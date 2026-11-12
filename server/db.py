"""SQLite storage. One connection per thread, WAL mode, simple helpers."""
import json
import os
import sqlite3
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("C2D_DATA", os.path.join(os.path.dirname(BASE), "data"))
MEDIA = os.path.join(DATA, "media")
DB_PATH = os.path.join(DATA, "cam2drone.db")
os.makedirs(MEDIA, exist_ok=True)

_local = threading.local()
write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs(
  id INTEGER PRIMARY KEY, parent_id INTEGER, kind TEXT NOT NULL DEFAULT 'customer',
  name TEXT NOT NULL, address TEXT DEFAULT '', lat REAL, lon REAL, notes TEXT DEFAULT '',
  arming_mode TEXT DEFAULT 'schedule', schedule TEXT DEFAULT '[]', allowed_types TEXT DEFAULT '[]',
  sop TEXT DEFAULT '[]', ack_timeout INTEGER DEFAULT 60, webhook_token TEXT UNIQUE,
  speaker_presets TEXT DEFAULT '[]', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, name TEXT NOT NULL, role TEXT DEFAULT '',
  phone TEXT DEFAULT '', email TEXT DEFAULT '', priority INTEGER DEFAULT 1,
  notify_sms INTEGER DEFAULT 1, notify_email INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS edges(
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL,
  last_seen REAL, version TEXT DEFAULT '', ip TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS cameras(
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, edge_id INTEGER, ext_key TEXT NOT NULL,
  name TEXT NOT NULL, lat REAL, lon REAL, enabled INTEGER DEFAULT 1, snooze_until REAL,
  last_seen REAL, online INTEGER DEFAULT 1, UNIQUE(org_id, ext_key));
CREATE TABLE IF NOT EXISTS docks(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  radius_km REAL DEFAULT 5, adapter TEXT DEFAULT 'sim', serial TEXT DEFAULT '',
  has_speaker INTEGER DEFAULT 1, has_light INTEGER DEFAULT 1, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
  org_id INTEGER, pw_hash TEXT NOT NULL, api_token TEXT UNIQUE, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, camera_id INTEGER, status TEXT NOT NULL DEFAULT 'new',
  priority INTEGER NOT NULL DEFAULT 3, type TEXT NOT NULL, first_ts REAL NOT NULL, last_ts REAL NOT NULL,
  count INTEGER DEFAULT 1, cameras TEXT DEFAULT '[]', assigned_user INTEGER, acked_ts REAL,
  escalated INTEGER DEFAULT 0, outcome TEXT, note TEXT DEFAULT '', closed_ts REAL, sop_done TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, org_id INTEGER, camera_id INTEGER, incident_id INTEGER,
  type TEXT, raw_type TEXT, target TEXT, state TEXT, image TEXT, raw TEXT, source TEXT,
  suppressed TEXT);
CREATE TABLE IF NOT EXISTS timeline(
  id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL, ts REAL NOT NULL, who TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS missions(
  id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL, dock_id INTEGER NOT NULL, start_ts REAL NOT NULL,
  tlat REAL, tlon REAL, distance_m REAL, return_ts REAL, p_ret REAL DEFAULT 0, landed_ts REAL,
  last_phase TEXT DEFAULT 'takeoff', light INTEGER DEFAULT 0, camera_mode TEXT DEFAULT 'wide');
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, incident_id INTEGER, ts REAL NOT NULL, channel TEXT, target TEXT,
  message TEXT, status TEXT, error TEXT, who TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL NOT NULL, who TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, start_ts REAL NOT NULL, end_ts REAL, handover TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS nofly(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 1000,
  level TEXT DEFAULT 'block', notes TEXT DEFAULT '', enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS push_subs(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, endpoint TEXT UNIQUE NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL, ts REAL);
CREATE TABLE IF NOT EXISTS cam_commands(
  id INTEGER PRIMARY KEY, camera_id INTEGER NOT NULL, edge_id INTEGER NOT NULL, cmd TEXT NOT NULL, args TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', result TEXT, who TEXT, created REAL NOT NULL, sent_ts REAL, done_ts REAL);
CREATE INDEX IF NOT EXISTS ix_cmd_edge ON cam_commands(edge_id, status);
CREATE INDEX IF NOT EXISTS ix_inc_status ON incidents(status, last_ts);
CREATE INDEX IF NOT EXISTS ix_inc_first ON incidents(first_ts);
CREATE INDEX IF NOT EXISTS ix_ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_ev_cam ON events(camera_id, ts);
CREATE INDEX IF NOT EXISTS ix_tl_inc ON timeline(incident_id);
CREATE INDEX IF NOT EXISTS ix_shift_open ON shifts(end_ts);
"""

# columns added after v1.0 (existing databases get them with ALTER TABLE)
COLUMNS = [("missions", "onsite_ts", "REAL"), ("events", "clip", "TEXT"), ("edges", "update_requested", "INTEGER DEFAULT 0"),
           ("cameras", "arming_mode", "TEXT DEFAULT 'inherit'"), ("cameras", "caps", "TEXT")]

JSON_COLS = {"schedule", "allowed_types", "sop", "cameras", "sop_done", "speaker_presets", "args"}


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init():
    with write_lock:
        conn().executescript(SCHEMA)
        for table, col, ddl in COLUMNS:
            if col not in [r[1] for r in conn().execute(f"PRAGMA table_info({table})")]:
                conn().execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        conn().commit()


def row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else []
        except ValueError:
            d[k] = []
    return d


def q(sql, args=()):
    return [row(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    return row(conn().execute(sql, args).fetchone())


def ex(sql, args=()):
    with write_lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def enc(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v


def insert(table, data):
    keys = list(data)
    return ex(f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
              [enc(data[k]) for k in keys])


def update(table, id_, data):
    if not data:
        return
    keys = list(data)
    ex(f"UPDATE {table} SET {','.join(k + '=?' for k in keys)} WHERE id=?", [enc(data[k]) for k in keys] + [id_])


def setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, json.dumps(value, ensure_ascii=False)))
