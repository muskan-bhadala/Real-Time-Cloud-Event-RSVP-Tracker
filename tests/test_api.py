"""Automated tests. Run from project root: pytest -v"""
import os, sys, tempfile, threading, time
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import pytest
from fastapi.testclient import TestClient
import app as backend

client = TestClient(backend.app)

def reg_login(email, role="attendee"):
    client.post("/api/register", json={"name": email, "email": email, "password": "secret1", "role": role})
    d = client.post("/api/login", json={"email": email, "password": "secret1"}).json()
    return {"Authorization": "Bearer " + d["token"]}, d["user"]["id"]

def new_event(h, cap=2, **kw):
    body = {"event_name": "Cloud Workshop", "event_date": "2099-01-01", "start_time": "10:00", "end_time": "12:00", "capacity": cap, **kw}
    r = client.post("/api/events", json=body, headers=h); assert r.status_code == 201, r.text
    return r.json()["id"]

org, _ = reg_login("org@t.com", "organizer")
other_org, _ = reg_login("org2@t.com", "organizer")

def test_duplicate_registration():
    r = client.post("/api/register", json={"name": "x", "email": "org@t.com", "password": "secret1"})
    assert r.status_code == 409

def test_bad_login():
    assert client.post("/api/login", json={"email": "org@t.com", "password": "nope123"}).status_code == 401

def test_attendee_cannot_create_event():
    a, _ = reg_login("a0@t.com")
    r = client.post("/api/events", json={"event_name": "x", "event_date": "2099-01-01", "start_time": "10:00", "end_time": "11:00", "capacity": 5}, headers=a)
    assert r.status_code == 403

def test_event_validation():
    r = client.post("/api/events", json={"event_name": "x", "event_date": "2099-01-01", "start_time": "12:00", "end_time": "10:00", "capacity": 5}, headers=org)
    assert r.status_code == 422

def test_rsvp_update_and_duplicate_prevention():
    a, _ = reg_login("a1@t.com"); eid = new_event(org, 5)
    for s in ["GOING", "GOING", "MAYBE", "GOING"]:
        assert client.post(f"/api/events/{eid}/rsvp", json={"status": s}, headers=a).status_code == 200
    ev = client.get(f"/api/events/{eid}", headers=a).json()
    assert ev["counts"]["GOING"] == 1 and sum(ev["counts"].values()) == 1      # still ONE row

def test_capacity_waitlist_and_promotion():
    a, _ = reg_login("a2@t.com"); b, _ = reg_login("b2@t.com"); c, _ = reg_login("c2@t.com")
    eid = new_event(org, 1)
    client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=a)
    assert client.get(f"/api/events/{eid}", headers=a).json()["status"] == "FULL"
    r = client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=b).json()
    assert r["status"] == "WAITLISTED"
    client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=c)      # 2nd waitlisted
    client.delete(f"/api/events/{eid}/rsvp", headers=a)                              # seat frees
    mine = client.get("/api/rsvps/me", headers=b).json()
    assert mine[-1]["status"] == "GOING"                                             # FIFO: b promoted first
    assert any(n["type"] == "WAITLIST_PROMOTION" for n in client.get("/api/notifications", headers=b).json())

def test_race_condition_last_seat():
    eid = new_event(org, 1); uids = []
    for i in range(10): uids.append(reg_login(f"r{i}@t.com")[1])
    res = []
    ts = [threading.Thread(target=lambda u=u: res.append(backend.set_rsvp(eid, u, "GOING"))) for u in uids]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert res.count("GOING") == 1 and res.count("WAITLISTED") == 9

def test_unauthorized_event_modification():
    eid = new_event(org, 5)
    body = {"event_name": "hack", "event_date": "2099-01-01", "start_time": "10:00", "end_time": "11:00", "capacity": 5}
    assert client.put(f"/api/events/{eid}", json=body, headers=other_org).status_code == 403
    assert client.delete(f"/api/events/{eid}", headers=other_org).status_code == 403

def test_unauthenticated_and_expired_token():
    assert client.get("/api/events").status_code == 401
    assert client.get("/api/events", headers={"Authorization": "Bearer junk.token"}).status_code == 401
    old = backend.TOKEN_TTL; backend.TOKEN_TTL = -10
    t = backend.make_token(1, "attendee"); backend.TOKEN_TTL = old
    assert client.get("/api/events", headers={"Authorization": "Bearer " + t}).status_code == 401

def test_deadline_enforced():
    a, _ = reg_login("a3@t.com"); eid = new_event(org, 5, registration_deadline="2000-01-01")
    body = {"event_name": "x", "event_date": "2099-01-01", "start_time": "10:00", "end_time": "11:00", "capacity": 5, "registration_deadline": "2000-01-01"}
    assert client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=a).status_code == 409

def test_announcement_notification_and_cancel():
    a, _ = reg_login("a4@t.com"); eid = new_event(org, 5)
    client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=a)
    assert client.post(f"/api/events/{eid}/announcements", json={"title": "Venue", "message": "Room 2"}, headers=org).status_code == 201
    assert client.get(f"/api/events/{eid}/announcements", headers=a).json()[0]["title"] == "Venue"
    client.delete(f"/api/events/{eid}", headers=org)
    types = [n["type"] for n in client.get("/api/notifications", headers=a).json()]
    assert "ANNOUNCEMENT" in types and "EVENT_CANCELLED" in types
    assert client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=a).status_code == 409

def test_analytics():
    a, _ = reg_login("a5@t.com"); eid = new_event(org, 4)
    client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=a)
    d = client.get(f"/api/events/{eid}/analytics", headers=org).json()
    assert d["capacity_utilization"] == 25.0 and d["available_seats"] == 3
    assert client.get(f"/api/events/{eid}/analytics", headers=a).status_code == 403

def test_sse_snapshot_reflects_change():
    a, _ = reg_login("a6@t.com"); eid = new_event(org, 4)
    before = backend.versions[eid]
    client.post(f"/api/events/{eid}/rsvp", json={"status": "GOING"}, headers=a)
    assert backend.versions[eid] == before + 1 and backend.snapshot(eid)["counts"]["GOING"] == 1
