"""Real-Time Event RSVP Tracker - FastAPI backend.
Auth (signed tokens) | RBAC | Event CRUD | Concurrency-safe RSVP + waitlist | SSE real-time | Analytics"""
import os, json, time, hmac, hashlib, base64, asyncio, secrets, sqlite3, logging
from contextlib import contextmanager
from datetime import datetime, date
from collections import defaultdict
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from db import get_conn, init_db, query

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rsvp")
SECRET = os.getenv("SECRET_KEY", "dev-secret-change-me").encode()   # env var, never hardcode in prod
TOKEN_TTL = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = FastAPI(title="Real-Time Event RSVP Tracker")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
init_db()
versions = defaultdict(int)          # per-event change counter -> drives SSE pushes
now = lambda: datetime.utcnow().isoformat()

# ---------------------------------------------------------------- security
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()

def check_pw(pw, stored):
    return hmac.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)

def make_token(uid, role):
    p = base64.urlsafe_b64encode(json.dumps({"uid": uid, "role": role, "exp": int(time.time()) + TOKEN_TTL}).encode()).decode()
    return p + "." + hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()

def read_token(t):
    try:
        p, sig = t.split(".")
        if not hmac.compare_digest(sig, hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()): raise ValueError
        d = json.loads(base64.urlsafe_b64decode(p))
        if d["exp"] < time.time(): raise ValueError
        return d
    except Exception:
        raise HTTPException(401, "Invalid or expired token")

def user_dep(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)):
    t = token or (authorization or "").replace("Bearer ", "")
    if not t: raise HTTPException(401, "Missing token")
    return read_token(t)

def organizer_dep(u=Depends(user_dep)):
    if u["role"] not in ("organizer", "admin"): raise HTTPException(403, "Organizer role required")
    return u

@contextmanager
def tx():
    """Write transaction. BEGIN IMMEDIATE takes the write lock up-front (serialises writers)."""
    c = get_conn()
    try:
        c.execute("BEGIN IMMEDIATE"); yield c; c.execute("COMMIT")
    except BaseException:
        try: c.execute("ROLLBACK")
        except sqlite3.Error: pass
        raise
    finally:
        c.close()

# ---------------------------------------------------------------- helpers
def counts(c, eid):
    r = {"GOING": 0, "MAYBE": 0, "NOT_GOING": 0, "WAITLISTED": 0}
    for row in c.execute("SELECT status, COUNT(*) n FROM rsvps WHERE event_id=? GROUP BY status", (eid,)):
        r[row["status"]] = row["n"]
    return r

def notify(c, uid, eid, typ, msg):
    c.execute("INSERT INTO notifications(user_id,event_id,type,message,created_at) VALUES(?,?,?,?,?)", (uid, eid, typ, msg, now()))

def audit(c, uid, action, detail=""):
    c.execute("INSERT INTO audit_logs(user_id,action,detail,created_at) VALUES(?,?,?,?)", (uid, action, detail, now()))

def refresh_status(c, eid):
    e = c.execute("SELECT status, capacity FROM events WHERE id=?", (eid,)).fetchone()
    if e["status"] in ("CANCELLED", "COMPLETED", "DRAFT"): return
    new = "FULL" if counts(c, eid)["GOING"] >= e["capacity"] else "PUBLISHED"
    c.execute("UPDATE events SET status=?, updated_at=? WHERE id=?", (new, now(), eid))

def fill_seats(c, eid):
    """FIFO waitlist: promote earliest waitlisted users while seats are free."""
    cap = c.execute("SELECT capacity FROM events WHERE id=?", (eid,)).fetchone()["capacity"]
    while counts(c, eid)["GOING"] < cap:
        w = c.execute("SELECT id,user_id FROM rsvps WHERE event_id=? AND status='WAITLISTED' ORDER BY updated_at,id LIMIT 1", (eid,)).fetchone()
        if not w: break
        c.execute("UPDATE rsvps SET status='GOING', updated_at=? WHERE id=?", (now(), w["id"]))
        notify(c, w["user_id"], eid, "WAITLIST_PROMOTION", "A seat opened up - you are now GOING!")
    refresh_status(c, eid)

def own_event(eid, u):
    e = query("SELECT * FROM events WHERE id=?", (eid,), one=True)
    if not e: raise HTTPException(404, "Event not found")
    if u["role"] != "admin" and e["organizer_id"] != u["uid"]:
        raise HTTPException(403, "You can only manage your own events")
    return e

def snapshot(eid):
    with get_conn() as c:
        e = c.execute("SELECT status, capacity FROM events WHERE id=?", (eid,)).fetchone()
        return {"counts": counts(c, eid), "status": e["status"] if e else "GONE", "capacity": e["capacity"] if e else 0}

# ---------------------------------------------------------------- models
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=6, max_length=100)
    role: str = "attendee"

class LoginIn(BaseModel):
    email: str
    password: str

class EventIn(BaseModel):
    event_name: str = Field(min_length=1, max_length=120)
    description: str = ""
    event_type: str = "general"
    event_date: str
    start_time: str
    end_time: str
    venue: str = ""
    online_link: str = ""
    capacity: int
    registration_deadline: Optional[str] = None
    status: str = "PUBLISHED"

class RsvpIn(BaseModel):
    status: str

class AnnouncementIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)

def validate_event(e: EventIn):
    try:
        d = date.fromisoformat(e.event_date)
        s = datetime.strptime(e.start_time, "%H:%M"); en = datetime.strptime(e.end_time, "%H:%M")
        dl = date.fromisoformat(e.registration_deadline) if e.registration_deadline else None
    except ValueError:
        raise HTTPException(422, "Invalid date/time (use YYYY-MM-DD and HH:MM)")
    if en <= s: raise HTTPException(422, "end_time must be after start_time")
    if not 1 <= e.capacity <= 100000: raise HTTPException(422, "capacity must be 1..100000")
    if dl and dl > d: raise HTTPException(422, "registration_deadline must be on/before event_date")
    if e.status not in ("DRAFT", "PUBLISHED"): raise HTTPException(422, "status must be DRAFT or PUBLISHED")

# ---------------------------------------------------------------- auth routes
@app.get("/api/health")
def health(): return {"ok": True}

@app.post("/api/register", status_code=201)
def register(b: RegisterIn):
    if b.role not in ("attendee", "organizer"): raise HTTPException(422, "role must be attendee or organizer")
    try:
        with tx() as c:
            cur = c.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                            (b.name, b.email.lower().strip(), hash_pw(b.password), b.role, now()))
            audit(c, cur.lastrowid, "REGISTER")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Email already registered")
    return {"message": "registered"}

@app.post("/api/login")
def login(b: LoginIn):
    u = query("SELECT * FROM users WHERE email=?", (b.email.lower().strip(),), one=True)
    if not u or not check_pw(b.password, u["password_hash"]): raise HTTPException(401, "Wrong email or password")
    return {"token": make_token(u["id"], u["role"]), "user": {"id": u["id"], "name": u["name"], "role": u["role"]}}

@app.post("/api/logout")
def logout(u=Depends(user_dep)): return {"message": "logged out (client discards token)"}

# ---------------------------------------------------------------- events
@app.post("/api/events", status_code=201)
def create_event(b: EventIn, u=Depends(organizer_dep)):
    validate_event(b)
    with tx() as c:
        cur = c.execute("""INSERT INTO events(organizer_id,event_name,description,event_type,event_date,start_time,end_time,
            venue,online_link,capacity,registration_deadline,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (u["uid"], b.event_name, b.description, b.event_type, b.event_date, b.start_time, b.end_time, b.venue,
             b.online_link, b.capacity, b.registration_deadline, b.status, now(), now()))
        audit(c, u["uid"], "CREATE_EVENT", str(cur.lastrowid))
    return {"id": cur.lastrowid}

@app.get("/api/events")
def list_events(u=Depends(user_dep), upcoming: bool = False):
    sql = """SELECT e.*, (SELECT status FROM rsvps WHERE event_id=e.id AND user_id=?) my_status
             FROM events e WHERE (e.status!='DRAFT' OR e.organizer_id=?)"""
    if upcoming: sql += " AND e.event_date >= date('now') AND e.status IN ('PUBLISHED','FULL')"
    rows = query(sql + " ORDER BY e.event_date, e.start_time", (u["uid"], u["uid"]))
    with get_conn() as c:
        for r in rows: r["counts"] = counts(c, r["id"])
    return rows

@app.get("/api/events/{eid}")
def get_event(eid: int, u=Depends(user_dep)):
    e = query("SELECT * FROM events WHERE id=?", (eid,), one=True)
    if not e or (e["status"] == "DRAFT" and e["organizer_id"] != u["uid"]): raise HTTPException(404, "Event not found")
    e.update(snapshot(eid)); return e

@app.put("/api/events/{eid}")
def update_event(eid: int, b: EventIn, u=Depends(organizer_dep)):
    old = own_event(eid, u); validate_event(b)
    if old["status"] == "CANCELLED": raise HTTPException(409, "Cancelled events cannot be edited")
    with tx() as c:
        c.execute("""UPDATE events SET event_name=?,description=?,event_type=?,event_date=?,start_time=?,end_time=?,venue=?,
            online_link=?,capacity=?,registration_deadline=?,status=?,updated_at=? WHERE id=?""",
            (b.event_name, b.description, b.event_type, b.event_date, b.start_time, b.end_time, b.venue, b.online_link,
             b.capacity, b.registration_deadline, b.status, now(), eid))
        users = [r["user_id"] for r in c.execute("SELECT user_id FROM rsvps WHERE event_id=?", (eid,))]
        if b.venue != old["venue"]:
            for x in users: notify(c, x, eid, "VENUE_CHANGE", f"Venue for '{b.event_name}' is now: {b.venue}")
        if (b.start_time, b.end_time, b.event_date) != (old["start_time"], old["end_time"], old["event_date"]):
            for x in users: notify(c, x, eid, "TIME_CHANGE", f"'{b.event_name}' is now {b.event_date} {b.start_time}-{b.end_time}")
        fill_seats(c, eid)
        audit(c, u["uid"], "UPDATE_EVENT", str(eid))
    versions[eid] += 1
    return {"message": "updated"}

@app.delete("/api/events/{eid}")
def cancel_event(eid: int, u=Depends(organizer_dep)):
    e = own_event(eid, u)
    with tx() as c:
        c.execute("UPDATE events SET status='CANCELLED', updated_at=? WHERE id=?", (now(), eid))
        for r in c.execute("SELECT user_id FROM rsvps WHERE event_id=?", (eid,)).fetchall():
            notify(c, r["user_id"], eid, "EVENT_CANCELLED", f"'{e['event_name']}' has been cancelled.")
        audit(c, u["uid"], "CANCEL_EVENT", str(eid))
    versions[eid] += 1
    return {"message": "cancelled"}

# ---------------------------------------------------------------- RSVP (concurrency-safe)
def set_rsvp(eid: int, uid: int, status: str):
    """ONE transaction: check capacity + write RSVP. BEGIN IMMEDIATE means only one writer at a time,
    so two users can never both read 'going=99' and both insert (the race condition)."""
    if status not in ("GOING", "MAYBE", "NOT_GOING"): raise HTTPException(422, "status must be GOING, MAYBE or NOT_GOING")
    with tx() as c:
        e = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not e: raise HTTPException(404, "Event not found")
        if e["status"] in ("CANCELLED", "COMPLETED", "DRAFT"): raise HTTPException(409, f"Event is {e['status']}")
        if e["registration_deadline"] and date.today().isoformat() > e["registration_deadline"]:
            raise HTTPException(409, "Registration deadline has passed")
        old = c.execute("SELECT * FROM rsvps WHERE event_id=? AND user_id=?", (eid, uid)).fetchone()
        final = status
        if status == "GOING" and not (old and old["status"] == "GOING"):
            if counts(c, eid)["GOING"] >= e["capacity"]: final = "WAITLISTED"
        if old and old["status"] == "WAITLISTED" and final == "WAITLISTED": pass   # keep FIFO position
        elif old:
            if old["status"] != final:      # same status again = idempotent no-op
                c.execute("UPDATE rsvps SET status=?, updated_at=? WHERE id=?", (final, now(), old["id"]))
        else:
            c.execute("INSERT INTO rsvps(event_id,user_id,status,responded_at,updated_at) VALUES(?,?,?,?,?)", (eid, uid, final, now(), now()))
        fill_seats(c, eid)      # frees seat -> promote waitlist; also refreshes FULL/PUBLISHED
        cur = c.execute("SELECT status FROM rsvps WHERE event_id=? AND user_id=?", (eid, uid)).fetchone()["status"]
        notify(c, uid, eid, "RSVP_CONFIRMATION", f"Your RSVP for '{e['event_name']}' is {cur}")
        audit(c, uid, "RSVP", f"{eid}:{cur}")
    versions[eid] += 1
    log.info("rsvp event=%s user=%s status=%s", eid, uid, cur)
    return cur

@app.post("/api/events/{eid}/rsvp")
@app.put("/api/events/{eid}/rsvp")
def rsvp(eid: int, b: RsvpIn, u=Depends(user_dep)):
    s = set_rsvp(eid, u["uid"], b.status)
    return {"status": s, "message": "Your RSVP has been recorded." if s != "WAITLISTED" else "Event is full - you are on the waitlist."}

@app.delete("/api/events/{eid}/rsvp")
def cancel_rsvp(eid: int, u=Depends(user_dep)):
    with tx() as c:
        c.execute("DELETE FROM rsvps WHERE event_id=? AND user_id=?", (eid, u["uid"]))   # idempotent
        fill_seats(c, eid)
    versions[eid] += 1
    return {"message": "RSVP cancelled"}

@app.get("/api/events/{eid}/rsvps")
def event_rsvps(eid: int, u=Depends(organizer_dep)):
    own_event(eid, u)
    return query("SELECT r.status, r.responded_at, r.updated_at, u.name, u.email FROM rsvps r JOIN users u ON u.id=r.user_id WHERE r.event_id=? ORDER BY r.updated_at", (eid,))

@app.get("/api/rsvps/me")
def my_rsvps(u=Depends(user_dep)):
    return query("SELECT r.*, e.event_name, e.event_date FROM rsvps r JOIN events e ON e.id=r.event_id WHERE r.user_id=? ORDER BY e.event_date", (u["uid"],))

# ---------------------------------------------------------------- analytics
@app.get("/api/events/{eid}/analytics")
def analytics(eid: int, u=Depends(organizer_dep)):
    e = own_event(eid, u)
    with get_conn() as c:
        cn = counts(c, eid)
        invited = c.execute("SELECT COUNT(*) n FROM users WHERE role='attendee'").fetchone()["n"]
        timeline = [dict(r) for r in c.execute("SELECT substr(responded_at,1,10) day, COUNT(*) n FROM rsvps WHERE event_id=? GROUP BY day ORDER BY day", (eid,))]
    responded = sum(cn.values())
    pct = lambda a, b: round(a / b * 100, 1) if b else 0.0
    return {"counts": cn, "total_rsvps": responded, "invited": invited,
            "response_rate": pct(responded, invited),
            "capacity_utilization": pct(cn["GOING"], e["capacity"]),
            "available_seats": max(0, e["capacity"] - cn["GOING"]),
            "waitlist_count": cn["WAITLISTED"], "rsvp_conversion_rate": pct(cn["GOING"], responded),
            "growth": timeline}

# ---------------------------------------------------------------- announcements & notifications
@app.post("/api/events/{eid}/announcements", status_code=201)
def post_announcement(eid: int, b: AnnouncementIn, u=Depends(organizer_dep)):
    own_event(eid, u)
    with tx() as c:
        c.execute("INSERT INTO announcements(event_id,title,message,created_at) VALUES(?,?,?,?)", (eid, b.title, b.message, now()))
        for r in c.execute("SELECT user_id FROM rsvps WHERE event_id=?", (eid,)).fetchall():
            notify(c, r["user_id"], eid, "ANNOUNCEMENT", f"{b.title}: {b.message}")
    return {"message": "announcement published"}

@app.get("/api/events/{eid}/announcements")
def get_announcements(eid: int, u=Depends(user_dep)):
    e = query("SELECT organizer_id FROM events WHERE id=?", (eid,), one=True)
    if not e: raise HTTPException(404, "Event not found")
    has = query("SELECT 1 x FROM rsvps WHERE event_id=? AND user_id=?", (eid, u["uid"]), one=True)
    if not (has or u["role"] == "admin" or e["organizer_id"] == u["uid"]): raise HTTPException(403, "RSVP to see announcements")
    return query("SELECT * FROM announcements WHERE event_id=? ORDER BY id DESC", (eid,))

@app.get("/api/notifications")
def notifications(u=Depends(user_dep)):
    return query("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["uid"],))

@app.put("/api/notifications/{nid}/read")
def read_notification(nid: int, u=Depends(user_dep)):
    with tx() as c: c.execute("UPDATE notifications SET read=1 WHERE id=? AND user_id=?", (nid, u["uid"]))
    return {"message": "ok"}

# ---------------------------------------------------------------- real-time (Server-Sent Events)
@app.get("/api/events/{eid}/stream")
async def stream(eid: int, u=Depends(user_dep)):
    async def gen():
        last = -1
        while True:
            if versions[eid] != last:            # something changed -> push fresh counts
                last = versions[eid]
                yield f"data: {json.dumps(snapshot(eid))}\n\n"
            else:
                yield ": ping\n\n"               # heartbeat keeps proxies from closing the stream
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
