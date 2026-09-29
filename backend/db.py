"""Database layer: SQLite connection + schema. Swap for Postgres/Firestore later."""
import os, sqlite3
from contextlib import closing

DB_PATH = os.getenv("DB_PATH", "rsvp.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('attendee','organizer','admin')),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  organizer_id INTEGER NOT NULL REFERENCES users(id),
  event_name TEXT NOT NULL, description TEXT, event_type TEXT,
  event_date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  venue TEXT, online_link TEXT,
  capacity INTEGER NOT NULL CHECK(capacity > 0),
  registration_deadline TEXT,
  status TEXT NOT NULL CHECK(status IN ('DRAFT','PUBLISHED','FULL','COMPLETED','CANCELLED')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rsvps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK(status IN ('GOING','MAYBE','NOT_GOING','WAITLISTED')),
  responded_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(event_id, user_id));
CREATE INDEX IF NOT EXISTS idx_rsvps_event_status ON rsvps(event_id, status);
CREATE TABLE IF NOT EXISTS announcements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  title TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  event_id INTEGER, type TEXT NOT NULL, message TEXT NOT NULL,
  read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, read);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
"""

def get_conn():
    # isolation_level=None -> autocommit; explicit transactions use BEGIN IMMEDIATE
    c = sqlite3.connect(os.getenv("DB_PATH", DB_PATH), timeout=10,
                        isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c

def init_db():
    with closing(get_conn()) as c:
        c.executescript(SCHEMA)

def query(sql, args=(), one=False):
    """Read query on a fresh connection; returns dict(s)."""
    with closing(get_conn()) as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows
