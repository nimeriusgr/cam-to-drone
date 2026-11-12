-- Cam to Drone v1.1: shifts, no-fly zones, push, live view, clips, edge updates
CREATE TABLE IF NOT EXISTS shifts(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, start_ts REAL NOT NULL, end_ts REAL, handover TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS nofly(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, radius_m REAL NOT NULL DEFAULT 1000,
  level TEXT DEFAULT 'block', notes TEXT DEFAULT '', enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS push_subs(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, endpoint TEXT UNIQUE NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL, ts REAL);
CREATE TABLE IF NOT EXISTS live_req(camera_id INTEGER PRIMARY KEY, until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS live_frames(camera_id INTEGER PRIMARY KEY, ts REAL NOT NULL, ctype TEXT NOT NULL, data TEXT NOT NULL);
ALTER TABLE missions ADD COLUMN onsite_ts REAL;
ALTER TABLE events ADD COLUMN clip TEXT;
ALTER TABLE edges ADD COLUMN update_requested INTEGER DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_inc_first ON incidents(first_ts);
CREATE INDEX IF NOT EXISTS ix_ev_cam ON events(camera_id, ts);
CREATE INDEX IF NOT EXISTS ix_shift_open ON shifts(end_ts);
INSERT INTO nofly(name,lat,lon,radius_m,level,notes) VALUES
 ('Αεροδρόμιο Ελ. Βενιζέλος (παράδειγμα)',37.9364,23.9445,8000,'block','Παράδειγμα. Ελέγξτε τις πραγματικές ζώνες στον χάρτη της ΥΠΑ πριν από κάθε χρήση.'),
 ('Αεροδρόμιο Τατοΐου (παράδειγμα)',38.1089,23.7838,4000,'block','Παράδειγμα. Ελέγξτε τις πραγματικές ζώνες στον χάρτη της ΥΠΑ πριν από κάθε χρήση.'),
 ('Αεροδρόμιο Ελευσίνας (παράδειγμα)',38.0638,23.5559,3000,'block','Παράδειγμα. Ελέγξτε τις πραγματικές ζώνες στον χάρτη της ΥΠΑ πριν από κάθε χρήση.');
