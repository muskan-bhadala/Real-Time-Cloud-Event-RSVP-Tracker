"""Seed demo data (dummy users only). Server must be running: python sample_data/seed.py"""
import json, urllib.request
BASE = "http://127.0.0.1:8000/api"
def call(path, body=None, token=None, method=None):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, json.dumps(body).encode() if body else None, h, method=method)
    try: return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e: return json.load(e)
call("/register", {"name": "Olivia Organizer", "email": "organizer@demo.com", "password": "demo1234", "role": "organizer"})
for n in ("alice", "bob", "carol"):
    call("/register", {"name": n.title(), "email": f"{n}@demo.com", "password": "demo1234"})
t = call("/login", {"email": "organizer@demo.com", "password": "demo1234"})["token"]
print(call("/events", {"event_name": "Cloud Computing Workshop", "event_date": "2099-01-01", "start_time": "10:00",
    "end_time": "12:00", "venue": "Lab 3", "capacity": 100, "description": "Demo event"}, t))
print("Seeded. Logins: organizer@demo.com / alice@demo.com / bob@demo.com  (password demo1234)")
