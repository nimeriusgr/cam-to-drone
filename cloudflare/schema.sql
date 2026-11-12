-- Cam to Drone · Cloudflare D1 schema (same model as the Python server, plus a media table)
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
  last_seen REAL, version TEXT DEFAULT '', ip TEXT DEFAULT '', update_requested INTEGER DEFAULT 0, offline_since REAL);
CREATE TABLE IF NOT EXISTS cameras(
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, edge_id INTEGER, ext_key TEXT NOT NULL,
  name TEXT NOT NULL, lat REAL, lon REAL, enabled INTEGER DEFAULT 1, snooze_until REAL,
  last_seen REAL, online INTEGER DEFAULT 1, arming_mode TEXT DEFAULT 'inherit', caps TEXT, offline_since REAL, UNIQUE(org_id, ext_key));
CREATE TABLE IF NOT EXISTS docks(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  radius_km REAL DEFAULT 5, adapter TEXT DEFAULT 'sim', serial TEXT DEFAULT '',
  has_speaker INTEGER DEFAULT 1, has_light INTEGER DEFAULT 1, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
  org_id INTEGER, pw_hash TEXT NOT NULL, api_token TEXT UNIQUE, active INTEGER DEFAULT 1,
  totp_secret TEXT, totp_on INTEGER DEFAULT 0, failed_logins INTEGER DEFAULT 0, locked_until REAL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL, camera_id INTEGER, status TEXT NOT NULL DEFAULT 'new',
  priority INTEGER NOT NULL DEFAULT 3, type TEXT NOT NULL, first_ts REAL NOT NULL, last_ts REAL NOT NULL,
  count INTEGER DEFAULT 1, cameras TEXT DEFAULT '[]', assigned_user INTEGER, acked_ts REAL,
  escalated INTEGER DEFAULT 0, outcome TEXT, note TEXT DEFAULT '', closed_ts REAL, sop_done TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, org_id INTEGER, camera_id INTEGER, incident_id INTEGER,
  type TEXT, raw_type TEXT, target TEXT, state TEXT, image TEXT, raw TEXT, source TEXT, suppressed TEXT, clip TEXT);
CREATE TABLE IF NOT EXISTS timeline(
  id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL, ts REAL NOT NULL, who TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS missions(
  id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL, dock_id INTEGER NOT NULL, start_ts REAL NOT NULL,
  tlat REAL, tlon REAL, distance_m REAL, return_ts REAL, p_ret REAL DEFAULT 0, landed_ts REAL,
  last_phase TEXT DEFAULT 'takeoff', light INTEGER DEFAULT 0, camera_mode TEXT DEFAULT 'wide', onsite_ts REAL);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, incident_id INTEGER, ts REAL NOT NULL, channel TEXT, target TEXT,
  message TEXT, status TEXT, error TEXT, who TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL NOT NULL, who TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS media(name TEXT PRIMARY KEY, ctype TEXT NOT NULL, data TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, start_ts REAL NOT NULL, end_ts REAL, handover TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS nofly(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 1000,
  level TEXT DEFAULT 'block', notes TEXT DEFAULT '', enabled INTEGER DEFAULT 1, geom TEXT, source TEXT DEFAULT '', ext_id TEXT);
CREATE TABLE IF NOT EXISTS push_subs(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, endpoint TEXT UNIQUE NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL, ts REAL);
CREATE TABLE IF NOT EXISTS live_req(camera_id INTEGER PRIMARY KEY, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS live_frames(camera_id INTEGER PRIMARY KEY, ts REAL NOT NULL, ctype TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cam_commands(
  id INTEGER PRIMARY KEY, camera_id INTEGER NOT NULL, edge_id INTEGER NOT NULL, cmd TEXT NOT NULL, args TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', result TEXT, who TEXT, created REAL NOT NULL, sent_ts REAL, done_ts REAL);
CREATE INDEX IF NOT EXISTS ix_cmd_edge ON cam_commands(edge_id, status);
CREATE TABLE IF NOT EXISTS login_fail(ip TEXT PRIMARY KEY, n INTEGER NOT NULL, first REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_inc_first ON incidents(first_ts);
CREATE INDEX IF NOT EXISTS ix_ev_cam ON events(camera_id, ts);
CREATE INDEX IF NOT EXISTS ix_shift_open ON shifts(end_ts);
CREATE INDEX IF NOT EXISTS ix_inc_status ON incidents(status, last_ts);
CREATE INDEX IF NOT EXISTS ix_inc_closed ON incidents(closed_ts);
CREATE INDEX IF NOT EXISTS ix_ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_ev_inc ON events(incident_id);
CREATE INDEX IF NOT EXISTS ix_tl_inc ON timeline(incident_id);
CREATE INDEX IF NOT EXISTS ix_mis_inc ON missions(incident_id);
CREATE INDEX IF NOT EXISTS ix_cam_org ON cameras(org_id);
