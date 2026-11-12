-- SKYWARDEN v1.3: arming per camera, ONVIF capabilities reported by the box, queue of camera commands
ALTER TABLE cameras ADD COLUMN arming_mode TEXT DEFAULT 'inherit';
ALTER TABLE cameras ADD COLUMN caps TEXT;
CREATE TABLE IF NOT EXISTS cam_commands(
  id INTEGER PRIMARY KEY, camera_id INTEGER NOT NULL, edge_id INTEGER NOT NULL, cmd TEXT NOT NULL, args TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', result TEXT, who TEXT, created REAL NOT NULL, sent_ts REAL, done_ts REAL);
CREATE INDEX IF NOT EXISTS ix_cmd_edge ON cam_commands(edge_id, status);
