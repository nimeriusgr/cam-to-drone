-- SKYWARDEN v1.4: offline alerts, 2FA and login lockout, polygon no-fly zones (ED-269 / GeoJSON / KML import)
ALTER TABLE edges ADD COLUMN offline_since REAL;
ALTER TABLE cameras ADD COLUMN offline_since REAL;
ALTER TABLE users ADD COLUMN totp_secret TEXT;
ALTER TABLE users ADD COLUMN totp_on INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN failed_logins INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until REAL;
ALTER TABLE nofly ADD COLUMN geom TEXT;
ALTER TABLE nofly ADD COLUMN source TEXT DEFAULT '';
ALTER TABLE nofly ADD COLUMN ext_id TEXT;
CREATE TABLE IF NOT EXISTS login_fail(ip TEXT PRIMARY KEY, n INTEGER NOT NULL, first REAL NOT NULL);
