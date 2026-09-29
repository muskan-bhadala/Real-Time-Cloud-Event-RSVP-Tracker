# Real-Time Cloud-Based Event Planning & RSVP Tracker

Organizers create events; attendees RSVP (Going / Maybe / Not going); the organizer dashboard updates **live** (Server-Sent Events) with capacity enforcement, FIFO waitlist, announcements, notifications and analytics.

**Stack:** FastAPI · SQLite (swap for Postgres) · Server-Sent Events · vanilla JS · Render free tier

## Folder structure
```
Real-Time-Cloud-Event-RSVP-Tracker/
├── backend/app.py          # API, auth, RBAC, RSVP transactions, waitlist, SSE, analytics
├── backend/db.py           # schema (unique + indexes) and DB helpers
├── frontend/index.html     # UI (served by FastAPI at /)
├── tests/test_api.py       # automated tests incl. last-seat race condition
├── sample_data/seed.py     # dummy users + demo event
├── docs/ screenshots/ reports/
├── requirements.txt  .env.example  .gitignore  render.yaml
```

## Run locally (5 minutes)
```bash
cd Real-Time-Cloud-Event-RSVP-Tracker
python -m venv venv
venv\Scripts\activate            # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env             # optional; defaults work for local dev
cd backend
uvicorn app:app --reload         # open http://127.0.0.1:8000  (API docs: /docs)
```
Optional seed (new terminal, server running): `python sample_data/seed.py`
Tests: from the project root run `pytest -v`

## Multi-user real-time demo (your main proof)
Session data is per browser tab, so 3 tabs = 3 users.
1. Tab 1: register **Organizer** → login → create "Cloud Computing Workshop", capacity 100.
2. Tab 2: register/login **Attendee A**. Tab 3: **Attendee B**. (Put tabs side by side.)
3. A clicks **Going** → organizer tab shows Going = 1 with no refresh.
4. B clicks **Maybe** → Maybe = 1. B clicks **Going** → Going = 2, Maybe = 0.
5. Organizer sends an announcement → attendees see it under Notifications (≤5 s) and Updates.
6. Test capacity: create event with capacity 1; A = Going, B = Going → B is WAITLISTED, status FULL. A clicks Cancel RSVP → B auto-promoted.
7. Unauthorized test: log in as a second organizer and try `PUT /api/events/{id}` in `/docs` → 403.

## Concurrency (interview answer)
Naive: `SELECT count(going)` then `INSERT` are two steps; two requests both read 99 and both insert → 101/100.
Fix (`set_rsvp` in `backend/app.py`): count + write inside one `BEGIN IMMEDIATE` transaction so writers are serialised. Equivalents: Postgres `SELECT ... FOR UPDATE` on the event row, DynamoDB conditional write, Firestore transaction. `UNIQUE(event_id,user_id)` guarantees one RSVP per user at DB level. Test: `test_race_condition_last_seat` (10 threads, 1 seat → exactly 1 GOING).

## Real-time approaches
Polling (simple, wasteful) · **SSE (used here: one-way server push, auto-reconnect)** · WebSockets/Firestore listeners (two-way, more infra). On any RSVP change the server bumps an event version counter; each open SSE stream pushes fresh counts within ~1 s. The DB stays the source of truth: the browser never sends counts, only a status.

## Cloud concepts → where they appear
SaaS (the app) · PaaS (Render runs it) · REST API (`/api/*`, `/docs`) · Auth + RBAC (signed tokens, organizer/attendee/admin) · Managed real-time (SSE) · Event-driven (RSVP → notification + push) · Env vars/secrets (`.env`, Render env) · Logging (`log.info`, `audit_logs`) · CI/CD (Render auto-deploys on `git push`) · Scalability (see below).

## Deploy free (Render)
1. Push to GitHub (below). 2. render.com → New → **Blueprint** → select the repo (uses `render.yaml`). 3. Wait for deploy, open the URL, check `/api/health`.
Note: free instances have ephemeral disk, so SQLite resets on redeploy, which is fine for a demo. For persistence use Render/Supabase Postgres (change only `db.py` + SQL placeholders).
Enterprise mapping: CloudFront (CDN) → API Gateway → Lambda/ECS → RDS/DynamoDB; WebSocket API for push; Cognito auth; SNS/SES notifications; CloudWatch monitoring. Azure: Front Door / API Mgmt / Functions / Cosmos DB / SignalR. GCP: Cloud CDN / API Gateway / Cloud Run / Firestore.

## Scalability (100k RSVPs in 5 min)
Load balancer + autoscaled stateless API containers → managed Postgres with per-event row lock (hot spot) mitigated by a queue (SQS/Redis) that serialises RSVPs per event, cached counters (Redis) for reads, CDN for static UI, rate limiting at gateway, WebSocket fan-out via pub/sub (Redis/SNS) instead of in-memory versions.

## Security
PBKDF2 password hashing · HMAC-signed expiring tokens · role checks on every route · ownership checks · Pydantic validation · parameterised SQL (no injection) · `esc()` output escaping in UI (XSS) · CORS via env · HTTPS by Render · audit log. Add for production: rate limiting (slowapi), HTTPS-only cookies/refresh tokens.

## Failure handling
Same RSVP twice = idempotent no-op · transaction rollback on any error · 401 auto-logout on token expiry · SSE reconnects automatically (EventSource) · notification failure can't break RSVP (in-app, same transaction).

## Push to GitHub
```bash
git init
git add .
git commit -m "Initialize real-time cloud event tracker"
git branch -M main
git remote add origin https://github.com/<you>/Real-Time-Cloud-Event-RSVP-Tracker.git
git push -u origin main
```
Topics: cloud-computing event-management rsvp realtime python fastapi websocket cloud-database rest-api authentication

## Screenshot checklist → save into `screenshots/`
01_folder_structure · 02_register_login · 03_organizer_create_event · 04_three_tabs_realtime (Going 1→2, Maybe 1→0) · 05_capacity_full_waitlist · 06_announcement_notification · 07_analytics · 08_unauthorized_403 (in /docs) · 09_pytest_passing · 10_render_dashboard · 11_live_url · 12_github_commits

## API summary
Auth: POST `/api/register` `/api/login` `/api/logout` · Events: POST/GET `/api/events`, GET/PUT/DELETE `/api/events/{id}` · RSVP: POST/PUT/DELETE `/api/events/{id}/rsvp`, GET `/api/events/{id}/rsvps`, GET `/api/rsvps/me` · GET `/api/events/{id}/analytics` · POST/GET `/api/events/{id}/announcements` · GET `/api/notifications`, PUT `/api/notifications/{id}/read` · SSE `/api/events/{id}/stream`. Full interactive docs at `/docs`.

## Limitations / future
SQLite single-node; SSE state in memory (use Redis pub/sub to scale out); no email/SMS/QR yet (add via SendGrid/`qrcode`); no admin UI.

## Author
<your name> · <college> · Cloud Computing course project
