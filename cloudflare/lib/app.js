// Cam to Drone · Cloudflare Pages Functions version of the central server (D1 database).
// Same API as server/app.py, so the console UI and the edge agent work unchanged.

const VERSION = '1.0.0-cf';
const TZ = 'Europe/Athens';
const MERGE_SECONDS = 120;
const CAMERA_OFFLINE_AFTER = 120;
const CRUISE_MS = 15, TAKEOFF_S = 20;
const MAX_MEDIA = 1_400_000;
const JSON_COLS = new Set(['schedule', 'allowed_types', 'sop', 'cameras', 'sop_done', 'speaker_presets']);

const TYPES = {
  human: ['Άτομο', 1], vehicle: ['Όχημα', 1], input: ['Είσοδος συναγερμού', 1], zone: ['Είσοδος σε ζώνη', 2],
  line: ['Διέλευση γραμμής', 2], tamper: ['Παραποίηση κάμερας', 2], face: ['Πρόσωπο', 2], motion: ['Κίνηση', 3],
  other: ['Συναγερμός', 3], videoloss: ['Απώλεια εικόνας', 4], offline: ['Εκτός σύνδεσης', 4],
};
const TECHNICAL = new Set(['videoloss', 'offline']);
const RAW_MAP = {
  vmd: 'motion', motiondetection: 'motion', linedetection: 'line', fielddetection: 'zone', regionentrance: 'zone',
  regionexiting: 'zone', shelteralarm: 'tamper', tamperdetection: 'tamper', videoloss: 'videoloss', io: 'input',
  facedetection: 'face', pir: 'motion', videomotion: 'motion', crosslinedetection: 'line', crossregiondetection: 'zone',
  smartmotionhuman: 'human', smartmotionvehicle: 'vehicle', videoblind: 'tamper', alarmlocal: 'input', intrusion: 'zone',
  tripwire: 'line', cellmotiondetector: 'motion', motion: 'motion', linedetector: 'line', fielddetector: 'zone',
  peopledetect: 'human', humandetect: 'human', vehicledetect: 'vehicle', tampering: 'tamper', digitalinput: 'input',
  human: 'human', person: 'human', vehicle: 'vehicle', car: 'vehicle', line: 'line', zone: 'zone', tamper: 'tamper', input: 'input',
};
const ADMIN = {
  orgs: ['parent_id', 'kind', 'name', 'address', 'lat', 'lon', 'notes', 'arming_mode', 'schedule', 'allowed_types', 'sop', 'ack_timeout', 'speaker_presets'],
  contacts: ['org_id', 'name', 'role', 'phone', 'email', 'priority', 'notify_sms', 'notify_email'],
  edges: ['org_id', 'name'],
  cameras: ['org_id', 'edge_id', 'ext_key', 'name', 'lat', 'lon', 'enabled', 'snooze_until'],
  docks: ['name', 'lat', 'lon', 'radius_km', 'adapter', 'serial', 'has_speaker', 'has_light', 'enabled'],
  users: ['username', 'name', 'role', 'org_id', 'active'],
};
const ROLES = new Set(['admin', 'supervisor', 'operator', 'customer']);
const PHASES = { enroute: 'Το drone απογειώθηκε, σε πορεία', onsite: 'Το drone έφτασε στο σημείο', landed: 'Το drone προσγειώθηκε στο Dock' };
const COMMANDS = { ack: 'ack', dispatch_drone: 'dispatch', return_home: 'return_home', false_alarm: 'false_alarm', close: 'close',
  speaker: 'speaker_tts', light_on: 'light', light_off: 'light', note: 'note' };

class ApiError extends Error { constructor(code, msg) { super(msg); this.code = code; } }
const need = (c, code, msg) => { if (!c) throw new ApiError(code, msg); };
const now = () => Date.now() / 1000;
const json = (obj, code = 200, headers = {}) => new Response(JSON.stringify(obj), { status: code, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers } });
const rnd = (n = 24) => { const b = crypto.getRandomValues(new Uint8Array(n)); return btoa(String.fromCharCode(...b)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); };
const hex = buf => [...new Uint8Array(buf)].map(x => x.toString(16).padStart(2, '0')).join('');
const typeLabel = t => (TYPES[t] || TYPES.other)[0];

// ------------------------------------------------------------------ db helpers
function parseRow(r) {
  if (!r) return null;
  const d = { ...r };
  for (const k of Object.keys(d)) if (JSON_COLS.has(k)) { try { d[k] = d[k] ? JSON.parse(d[k]) : []; } catch { d[k] = []; } }
  return d;
}
const enc = v => (v !== null && typeof v === 'object') ? JSON.stringify(v) : (v === undefined ? null : v);
class Db {
  constructor(d1) { this.d1 = d1; }
  async q(sql, args = []) { const r = await this.d1.prepare(sql).bind(...args).all(); return (r.results || []).map(parseRow); }
  async q1(sql, args = []) { return parseRow(await this.d1.prepare(sql).bind(...args).first()); }
  async ex(sql, args = []) { const r = await this.d1.prepare(sql).bind(...args).run(); return r.meta && r.meta.last_row_id; }
  async insert(table, data) { const k = Object.keys(data); return this.ex(`INSERT INTO ${table}(${k.join(',')}) VALUES(${k.map(() => '?').join(',')})`, k.map(x => enc(data[x]))); }
  async update(table, id, data) { const k = Object.keys(data); if (!k.length) return; await this.ex(`UPDATE ${table} SET ${k.map(x => x + '=?').join(',')} WHERE id=?`, [...k.map(x => enc(data[x])), id]); }
  async setting(key, def = null) { const r = await this.q1('SELECT value FROM settings WHERE key=?', [key]); return r ? JSON.parse(r.value) : def; }
  async setSetting(key, v) { await this.ex('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', [key, JSON.stringify(v)]); }
}

// ------------------------------------------------------------------ auth (PBKDF2-SHA256, 100k iterations: same format as the Python server)
async function pbkdf2(pw, salt, iters) {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(pw), 'PBKDF2', false, ['deriveBits']);
  return hex(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: new TextEncoder().encode(salt), iterations: iters }, key, 256));
}
async function hashPw(pw) { const salt = hex(crypto.getRandomValues(new Uint8Array(16))); return `pbkdf2$${salt}$${await pbkdf2(pw, salt, 100000)}$100000`; }
async function checkPw(pw, stored) {
  const p = (stored || '').split('$'); if (p.length < 3) return false;
  const iters = +(p[3] || 100000); if (iters > 100000) return false;
  const h = await pbkdf2(pw, p[1], iters);
  let diff = h.length ^ p[2].length; for (let i = 0; i < h.length; i++) diff |= h.charCodeAt(i) ^ (p[2].charCodeAt(i) || 0);
  return diff === 0;
}

// ------------------------------------------------------------------ helpers
function normType(raw, target) {
  const s = String(raw ?? '').toLowerCase();
  let t = RAW_MAP[s.split('/').pop().replace(/[^a-z]/g, '')];
  if (!t) { const flat = s.replace(/[_/]/g, ''); t = Object.keys(RAW_MAP).find(k => flat.includes(k)); t = t ? RAW_MAP[t] : 'other'; }
  const tg = String(target ?? '').toLowerCase();
  if (['line', 'zone', 'motion', 'other'].includes(t)) { if (['human', 'person', 'people'].includes(tg)) t = 'human'; else if (['vehicle', 'car'].includes(tg)) t = 'vehicle'; }
  return t;
}
function localNow(at) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', { timeZone: TZ, weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(at || new Date()).map(p => [p.type, p.value]));
  const wd = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }[parts.weekday];
  return { day: wd, hm: (+parts.hour % 24) * 60 + +parts.minute };
}
function isArmed(org) {
  const mode = org.arming_mode || 'schedule';
  if (mode === 'armed') return true; if (mode === 'disarmed') return false;
  const sched = org.schedule || []; if (!sched.length) return true;
  const { day, hm } = localNow(); const prev = day === 1 ? 7 : day - 1;
  return sched.some(s => {
    const f = +s.from.slice(0, 2) * 60 + +s.from.slice(3, 5), t = +s.to.slice(0, 2) * 60 + +s.to.slice(3, 5), days = s.days || [1, 2, 3, 4, 5, 6, 7];
    return f <= t ? days.includes(day) && hm >= f && hm < t : (days.includes(day) && hm >= f) || (days.includes(prev) && hm < t);
  });
}
const fmtTime = (t, opt) => new Intl.DateTimeFormat('el-GR', { timeZone: TZ, hour12: false, ...opt }).format(new Date(t * 1000));
const haversine = (a1, o1, a2, o2) => { const r = 6371000, p1 = a1 * Math.PI / 180, p2 = a2 * Math.PI / 180, dp = p2 - p1, dl = (o2 - o1) * Math.PI / 180; const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2; return 2 * r * Math.asin(Math.sqrt(a)); };

// ------------------------------------------------------------------ core service bound to a request
class App {
  constructor(env, request) { this.env = env; this.db = new Db(env.DB); this.req = request; this.url = new URL(request.url); this.simSpeed = +(env.SIM_SPEED || 1); }

  async audit(who, action, detail = '') { await this.db.insert('audit', { ts: now(), who, action, detail }); }
  async tl(iid, text, who = 'σύστημα', kind = 'info') { await this.db.insert('timeline', { incident_id: iid, ts: now(), who, kind, text }); }

  async saveMedia(data, ext) {
    if (!data) return null;
    const bytes = typeof data === 'string' ? Uint8Array.from(atob(data), c => c.charCodeAt(0)) : new Uint8Array(data);
    if (!bytes.length || bytes.length > MAX_MEDIA) return null;
    let bin = ''; for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const name = `${Date.now()}_${rnd(4)}.${ext}`;
    const ctype = { svg: 'image/svg+xml', png: 'image/png', webm: 'audio/webm' }[ext] || 'image/jpeg';
    await this.db.ex('INSERT INTO media(name,ctype,data,ts) VALUES(?,?,?,?)', [name, ctype, btoa(bin), now()]);
    return name;
  }

  async camera(orgId, key, name, edgeId) {
    let c = await this.db.q1('SELECT * FROM cameras WHERE org_id=? AND ext_key=?', [orgId, key]);
    if (c) return c;
    const id = await this.db.insert('cameras', { org_id: orgId, edge_id: edgeId ?? null, ext_key: key, name: name || key, last_seen: now() });
    return this.db.q1('SELECT * FROM cameras WHERE id=?', [id]);
  }

  async ingest({ orgId, key, rawType, target, state = 'active', name, image, imageExt = 'jpg', raw = '', source = 'webhook', edgeId = null, ts = null, ignoreArming = false }) {
    const org = await this.db.q1('SELECT * FROM orgs WHERE id=?', [orgId]);
    if (!org) return { ok: false, error: 'unknown organisation' };
    const cam = await this.camera(orgId, key, name, edgeId);
    const t = ts || now();
    await this.db.update('cameras', cam.id, { last_seen: t, online: 1 });
    const etype = normType(rawType, target), st = String(state || 'active').toLowerCase();
    const img = image ? await this.saveMedia(image, imageExt) : null;
    const ev = { ts: t, org_id: orgId, camera_id: cam.id, type: etype, raw_type: String(rawType ?? '').slice(0, 80), target: target ?? null, state: st, image: img, raw: String(raw).slice(0, 4000), source };
    let reason = null;
    if (['inactive', 'stop', 'false', '0'].includes(st)) reason = 'τέλος συμβάντος';
    else if (!cam.enabled) reason = 'κάμερα απενεργοποιημένη';
    else if (cam.snooze_until && cam.snooze_until > t) reason = 'κάμερα σε παύση';
    else if (!TECHNICAL.has(etype) && etype !== 'tamper' && !ignoreArming && !isArmed(org)) reason = 'αφόπλιστο (εκτός ωραρίου)';
    else if (org.allowed_types.length && !org.allowed_types.includes(etype) && !TECHNICAL.has(etype)) reason = 'τύπος φιλτραρισμένος';
    if (reason) { await this.db.insert('events', { ...ev, suppressed: reason }); return { ok: true, suppressed: reason }; }

    const [label, prio] = TYPES[etype] || TYPES.other;
    const inc = await this.db.q1("SELECT * FROM incidents WHERE org_id=? AND status!='closed' AND last_ts>? ORDER BY last_ts DESC LIMIT 1", [orgId, t - MERGE_SECONDS]);
    if (inc && TECHNICAL.has(etype) === TECHNICAL.has(inc.type)) {
      const cams = inc.cameras; if (!cams.includes(cam.id)) cams.push(cam.id);
      const upd = { last_ts: t, count: inc.count + 1, cameras: cams };
      if (prio < inc.priority) Object.assign(upd, { priority: prio, type: etype });
      await this.db.update('incidents', inc.id, upd);
      await this.db.insert('events', { ...ev, incident_id: inc.id });
      await this.tl(inc.id, `Νέο σήμα: ${label} · ${cam.name}`, 'σύστημα', 'signal');
      return { ok: true, incident_id: inc.id, merged: true };
    }
    const iid = await this.db.insert('incidents', { org_id: orgId, camera_id: cam.id, status: 'new', priority: prio, type: etype, first_ts: t, last_ts: t, cameras: [cam.id] });
    await this.db.insert('events', { ...ev, incident_id: iid });
    await this.tl(iid, `Συναγερμός: ${label} · ${cam.name}`, 'σύστημα', 'alarm');
    return { ok: true, incident_id: iid };
  }

  // ---------------- drones (simulation; DJI Cloud API connection is not available on Pages)
  async missionView(m) {
    if (!m) return null;
    const dock = await this.db.q1('SELECT * FROM docks WHERE id=?', [m.dock_id]);
    const t = now(), sp = this.simSpeed, el = (t - m.start_ts) * sp, flight = Math.max(10, (m.distance_m || 0) / CRUISE_MS);
    let phase, p, eta = 0;
    if (m.return_ts == null) {
      if (el < TAKEOFF_S) { phase = 'takeoff'; p = 0; } else if (el < TAKEOFF_S + flight) { phase = 'enroute'; p = (el - TAKEOFF_S) / flight; } else { phase = 'onsite'; p = 1; }
      eta = Math.max(0, Math.floor((TAKEOFF_S + flight - el) / sp));
    } else {
      const tr = (t - m.return_ts) * sp, dur = Math.max(10, flight * m.p_ret);
      if (tr < dur) { phase = 'returning'; p = m.p_ret * (1 - tr / dur); eta = Math.max(0, Math.floor((dur - tr) / sp)); } else { phase = 'landed'; p = 0; }
    }
    if (m.landed_ts) { phase = 'landed'; p = 0; }
    const end = m.landed_ts || t;
    return { ...m, phase, progress: Math.round(p * 1e4) / 1e4, lat: dock.lat + (m.tlat - dock.lat) * p, lon: dock.lon + (m.tlon - dock.lon) * p,
      alt: phase === 'landed' ? 0 : phase === 'takeoff' ? Math.floor(40 * Math.min(1, el / TAKEOFF_S)) : 40,
      battery: Math.max(15, Math.floor(100 - (end - m.start_ts) * sp * 0.08)), eta, elapsed: Math.floor(end - m.start_ts),
      dock: { id: dock.id, name: dock.name, lat: dock.lat, lon: dock.lon, adapter: dock.adapter, has_speaker: dock.has_speaker, has_light: dock.has_light },
      simulated: dock.adapter === 'sim' };
  }
  async busyDocks() { return new Set((await this.db.q('SELECT dock_id FROM missions WHERE landed_ts IS NULL')).map(r => r.dock_id)); }
  async dockOptions(lat, lon) {
    const busy = await this.busyDocks();
    const out = (await this.db.q('SELECT * FROM docks WHERE enabled=1')).map(d => {
      const dist = haversine(d.lat, d.lon, lat, lon);
      return { ...d, distance_m: Math.floor(dist), in_range: dist <= d.radius_km * 1000, available: !busy.has(d.id), eta_s: Math.floor((TAKEOFF_S + dist / CRUISE_MS) / this.simSpeed) };
    });
    return out.sort((a, b) => (!(a.available && a.in_range)) - (!(b.available && b.in_range)) || a.distance_m - b.distance_m);
  }
  async target(inc) {
    const cam = inc.camera_id ? await this.db.q1('SELECT lat,lon FROM cameras WHERE id=?', [inc.camera_id]) : null;
    if (cam && cam.lat != null) return [cam.lat, cam.lon];
    const org = await this.db.q1('SELECT lat,lon FROM orgs WHERE id=?', [inc.org_id]);
    return org && org.lat != null ? [org.lat, org.lon] : null;
  }

  // ---------------- background work, done lazily on requests (Pages has no background threads)
  async tick() {
    const t = now();
    for (const m of await this.db.q('SELECT * FROM missions WHERE landed_ts IS NULL')) {
      const v = await this.missionView(m);
      if (v.phase !== m.last_phase) {
        const upd = { last_phase: v.phase }; if (v.phase === 'landed') upd.landed_ts = t;
        await this.db.update('missions', m.id, upd);
        if (PHASES[v.phase]) await this.tl(m.incident_id, PHASES[v.phase], 'σύστημα', 'drone');
      }
    }
    for (const inc of await this.db.q("SELECT i.id,i.first_ts,i.priority,o.ack_timeout FROM incidents i JOIN orgs o ON o.id=i.org_id WHERE i.status='new' AND i.escalated=0")) {
      if (t - inc.first_ts > (inc.ack_timeout || 60)) {
        await this.db.update('incidents', inc.id, { escalated: 1, priority: Math.max(1, inc.priority - 1) });
        await this.tl(inc.id, `Κλιμάκωση: χωρίς ανάληψη για ${inc.ack_timeout}″, ειδοποίηση επόπτη`, 'σύστημα', 'escalate');
      }
    }
    await this.db.ex('UPDATE cameras SET online=0 WHERE edge_id IS NOT NULL AND online=1 AND last_seen<?', [t - CAMERA_OFFLINE_AFTER]);
  }

  // ---------------- views
  async incidentView(iid) {
    const inc = await this.db.q1('SELECT * FROM incidents WHERE id=?', [iid]);
    if (!inc) return null;
    const org = await this.db.q1('SELECT * FROM orgs WHERE id=?', [inc.org_id]);
    const cam = inc.camera_id ? await this.db.q1('SELECT id,name,lat,lon,ext_key FROM cameras WHERE id=?', [inc.camera_id]) : null;
    const camList = inc.cameras.length ? await this.db.q(`SELECT id,name,lat,lon,online FROM cameras WHERE id IN (${inc.cameras.map(() => '?').join(',')})`, inc.cameras) : [];
    const images = (await this.db.q('SELECT image FROM events WHERE incident_id=? AND image IS NOT NULL ORDER BY ts', [iid])).map(r => r.image);
    const user = inc.assigned_user ? await this.db.q1('SELECT name FROM users WHERE id=?', [inc.assigned_user]) : null;
    const m = await this.db.q1('SELECT * FROM missions WHERE incident_id=? ORDER BY id DESC LIMIT 1', [iid]);
    return { ...inc, type_label: typeLabel(inc.type),
      org: org && { id: org.id, name: org.name, address: org.address, lat: org.lat, lon: org.lon, sop: org.sop, speaker_presets: org.speaker_presets, notes: org.notes, arming_mode: org.arming_mode },
      camera: cam, camera_list: camList, images: images.slice(-12), assigned_name: user ? user.name : null,
      contacts: await this.db.q('SELECT * FROM contacts WHERE org_id=? ORDER BY priority', [inc.org_id]),
      timeline: await this.db.q('SELECT ts,who,kind,text FROM timeline WHERE incident_id=? ORDER BY ts,id', [iid]),
      notifications: await this.db.q('SELECT * FROM notifications WHERE incident_id=? ORDER BY ts', [iid]),
      mission: m ? await this.missionView(m) : null, age: Math.floor(now() - inc.first_ts) };
  }
  async incidentList(vis) {
    const base = `SELECT i.*, o.name AS org_name, c.name AS camera_name, u.name AS assigned_name,
      (SELECT image FROM events e WHERE e.incident_id=i.id AND e.image IS NOT NULL ORDER BY e.ts DESC LIMIT 1) AS thumb,
      EXISTS(SELECT 1 FROM missions m WHERE m.incident_id=i.id AND m.landed_ts IS NULL) AS flying
      FROM incidents i JOIN orgs o ON o.id=i.org_id LEFT JOIN cameras c ON c.id=i.camera_id LEFT JOIN users u ON u.id=i.assigned_user`;
    const open = await this.db.q(`${base} WHERE i.status!='closed' ORDER BY i.priority, i.first_ts`);
    const closed = await this.db.q(`${base} WHERE i.status='closed' ORDER BY i.closed_ts DESC LIMIT 25`);
    return [...open, ...closed].filter(r => !vis || vis.has(r.org_id)).map(r => ({ ...r, type_label: typeLabel(r.type), flying: !!r.flying }));
  }
  policeSummary(inc) {
    const org = inc.org || {}, cam = inc.camera || {}, lat = cam.lat ?? org.lat, lon = cam.lon ?? org.lon;
    const l = [`Συναγερμός σε επιχείρηση: ${org.name || ''}`, `Διεύθυνση: ${org.address || '—'}`,
      lat != null ? `Συντεταγμένες: ${(+lat).toFixed(5)}, ${(+lon).toFixed(5)}` : 'Συντεταγμένες: —',
      `Συμβάν: ${inc.type_label} από κάμερα «${cam.name || '—'}» στις ${fmtTime(inc.first_ts, { hour: '2-digit', minute: '2-digit' })}`];
    const m = inc.mission;
    if (m && ['onsite', 'enroute'].includes(m.phase)) l.push(m.phase === 'onsite' ? 'Drone επιτήρησης βρίσκεται στο σημείο με ζωντανή εικόνα.' : 'Drone επιτήρησης κατευθύνεται στο σημείο.');
    l.push(`Αριθμός περιστατικού: #${inc.id}`);
    return l.join('\n');
  }

  // ---------------- notifications over HTTP APIs (SMS and email providers)
  async httpProvider(cfgKey, vars) {
    const cfg = await this.db.setting(cfgKey, {}) || {};
    if (!cfg.url) return ['simulated', 'Δεν έχει ρυθμιστεί πάροχος'];
    let body = JSON.stringify(cfg.body || {});
    for (const [k, v] of Object.entries({ ...vars, sender: cfg.sender || '' })) body = body.split(`{${k}}`).join(JSON.stringify(String(v)).slice(1, -1));
    try {
      const r = await fetch(cfg.url, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(cfg.headers || {}) }, body });
      return r.ok ? ['sent', null] : ['failed', `HTTP ${r.status}`];
    } catch (e) { return ['failed', String(e).slice(0, 200)]; }
  }
  async notify(iid, channel, target, message, who) {
    let st = 'logged', err = null;
    if (channel === 'sms') [st, err] = await this.httpProvider('sms', { to: target, text: message });
    else if (channel === 'email') [st, err] = await this.httpProvider('email', { to: target, subject: `Συναγερμός #${iid}`, text: message });
    await this.db.insert('notifications', { incident_id: iid, ts: now(), channel, target, message, status: st, error: err, who });
    const label = { sms: 'SMS', email: 'Email', voice: 'Κλήση', police: 'Αστυνομία' }[channel] || channel;
    const extra = { simulated: ' (προσομοίωση: δεν έχει ρυθμιστεί πάροχος)', failed: ` (απέτυχε: ${err})` }[st] || '';
    await this.tl(iid, `${label} → ${target}${extra}`, who, 'notify');
    return [st, err];
  }

  // ---------------- auth
  cookie(name) { const m = (this.req.headers.get('Cookie') || '').match(new RegExp('(?:^|;\\s*)' + name + '=([^;]+)')); return m ? m[1] : null; }
  bearer() { const h = this.req.headers.get('Authorization') || ''; return h.toLowerCase().startsWith('bearer ') ? h.slice(7).trim() : (this.req.headers.get('X-Token') || null); }
  async user(required = true, roles = null) {
    let u = null; const c = this.cookie('c2d');
    if (c) { const s = await this.db.q1('SELECT user_id FROM sessions WHERE token=? AND expires>?', [c, now()]); if (s) u = await this.db.q1('SELECT * FROM users WHERE id=? AND active=1', [s.user_id]); }
    if (!u && this.bearer()) u = await this.db.q1('SELECT * FROM users WHERE api_token=? AND active=1', [this.bearer()]);
    if (required) need(u, 401, 'Απαιτείται σύνδεση');
    if (roles && u) need(roles.includes(u.role), 403, 'Δεν έχετε δικαίωμα');
    return u;
  }
  async visible(u) {
    if (u.role !== 'customer') return null;
    const ids = new Set([u.org_id]); for (const o of await this.db.q('SELECT id FROM orgs WHERE parent_id=?', [u.org_id])) ids.add(o.id); return ids;
  }
  async body() { try { return await this.req.json(); } catch { throw new ApiError(400, 'Το σώμα πρέπει να είναι JSON'); } }

  // ---------------- incident actions
  async action(iid, u, b) {
    const who = u.name, a = b.action;
    const inc = await this.db.q1('SELECT * FROM incidents WHERE id=?', [iid]);
    need(inc, 404, `Δεν βρέθηκε το περιστατικό #${iid}`);
    need(u.role !== 'customer', 403, 'Ο ρόλος πελάτη έχει μόνο προβολή');
    const m = await this.db.q1('SELECT * FROM missions WHERE incident_id=? AND landed_ts IS NULL', [iid]);
    if (a !== 'note') need(inc.status !== 'closed', 409, 'Το περιστατικό είναι κλειστό');
    const t = now();
    switch (a) {
      case 'ack':
        await this.db.update('incidents', iid, { assigned_user: u.id, acked_ts: inc.acked_ts || t, status: inc.status === 'new' ? 'ack' : inc.status });
        await this.tl(iid, 'Ανάληψη περιστατικού', who, 'ack'); break;
      case 'sop': {
        const org = await this.db.q1('SELECT sop FROM orgs WHERE id=?', [inc.org_id]); const i = +b.step;
        need(i >= 0 && i < org.sop.length, 400, 'Άγνωστο βήμα');
        const done = new Set(inc.sop_done);
        if (b.done !== false) { done.add(i); await this.tl(iid, `✓ ${org.sop[i]}`, who, 'sop'); } else done.delete(i);
        await this.db.update('incidents', iid, { sop_done: [...done].sort((x, y) => x - y) }); break;
      }
      case 'note': need(b.text, 400, 'Κενή σημείωση'); await this.tl(iid, `Σημείωση: ${b.text}`, who, 'note'); break;
      case 'false_alarm':
        need(!m, 409, 'Το drone είναι σε αποστολή. Δώστε πρώτα επιστροφή.');
        await this.db.update('incidents', iid, { status: 'closed', outcome: 'Ψευδής συναγερμός', note: b.note || '', closed_ts: t, assigned_user: inc.assigned_user || u.id });
        await this.tl(iid, 'Κλείσιμο ως ψευδής συναγερμός' + (b.note ? `: ${b.note}` : ''), who, 'close'); break;
      case 'snooze_camera': {
        const cid = +(b.camera_id || inc.camera_id), mins = +(b.minutes || 60);
        await this.db.update('cameras', cid, { snooze_until: t + mins * 60 });
        const cam = await this.db.q1('SELECT name FROM cameras WHERE id=?', [cid]);
        await this.tl(iid, `Παύση κάμερας «${cam.name}» για ${mins} λεπτά`, who, 'camera'); break;
      }
      case 'dispatch': {
        need(!m, 409, 'Υπάρχει ήδη drone σε αποστολή για αυτό το περιστατικό');
        const tg = await this.target(inc); need(tg, 400, 'Δεν υπάρχουν συντεταγμένες για την κάμερα ή την επιχείρηση');
        const opts = await this.dockOptions(...tg);
        const dock = b.dock_id ? opts.find(d => d.id === +b.dock_id) : opts.find(d => d.available && d.in_range);
        need(dock, 409, 'Δεν υπάρχει διαθέσιμο drone εντός εμβέλειας');
        need(dock.available, 409, `Το ${dock.name} είναι ήδη σε αποστολή`);
        need(dock.adapter === 'sim', 502, 'Η σύνδεση με πραγματικό DJI Dock δεν έχει ενεργοποιηθεί ακόμα (DJI Cloud API).');
        await this.db.insert('missions', { incident_id: iid, dock_id: dock.id, start_ts: t, tlat: tg[0], tlon: tg[1], distance_m: dock.distance_m });
        await this.db.update('incidents', iid, { status: 'dispatched', assigned_user: inc.assigned_user || u.id, acked_ts: inc.acked_ts || t });
        await this.tl(iid, `Αποστολή drone από ${dock.name} · ${(dock.distance_m / 1000).toFixed(1)} km`, who, 'drone'); break;
      }
      case 'return_home': case 'light': case 'camera_mode': case 'speaker_tts': {
        need(m, 409, 'Δεν υπάρχει drone σε πτήση για αυτό το περιστατικό');
        const dock = await this.db.q1('SELECT * FROM docks WHERE id=?', [m.dock_id]);
        if (a === 'return_home') {
          need(m.return_ts == null, 409, 'Το drone επιστρέφει ήδη');
          const v = await this.missionView(m);
          await this.db.update('missions', m.id, { return_ts: t, p_ret: v.progress });
          await this.tl(iid, 'Εντολή επιστροφής στο Dock', who, 'drone');
        } else if (a === 'light') {
          need(dock.has_light, 400, 'Το drone δεν έχει προβολέα');
          await this.db.update('missions', m.id, { light: b.on ? 1 : 0 });
          await this.tl(iid, 'Προβολέας ' + (b.on ? 'ανοιχτός' : 'κλειστός'), who, 'drone');
        } else if (a === 'camera_mode') {
          need(['wide', 'zoom', 'ir'].includes(b.mode), 400, 'Άγνωστη κάμερα');
          await this.db.update('missions', m.id, { camera_mode: b.mode });
        } else {
          need(dock.has_speaker, 400, 'Το drone δεν έχει μεγάφωνο');
          const text = String(b.text || '').trim(); need(text, 400, 'Κενό μήνυμα');
          await this.tl(iid, `Μεγάφωνο: «${text}»`, who, 'speaker');
        }
        break;
      }
      case 'notify': {
        need(['sms', 'email'].includes(b.channel), 400, 'Κανάλι sms ή email');
        const msg = String(b.message || '').trim(); need(msg, 400, 'Κενό μήνυμα');
        need((b.contact_ids || []).length, 400, 'Επιλέξτε τουλάχιστον μία επαφή');
        const results = [];
        for (const cid of b.contact_ids) {
          const c = await this.db.q1('SELECT * FROM contacts WHERE id=? AND org_id=?', [cid, inc.org_id]); if (!c) continue;
          const target = b.channel === 'sms' ? c.phone : c.email;
          if (!target) { results.push({ contact: c.name, status: 'skipped' }); continue; }
          const [st, err] = await this.notify(iid, b.channel, target, msg, who);
          results.push({ contact: c.name, status: st, error: err });
        }
        await this.audit(who, 'incident.notify', `#${iid}`);
        return { ok: true, results };
      }
      case 'call_log': need(b.target, 400, 'Με ποιον μιλήσατε;'); await this.notify(iid, 'voice', b.target + (b.result ? ` · ${b.result}` : ''), b.result || '', who); break;
      case 'police': await this.notify(iid, 'police', String(b.number || '100') + (b.result ? ` · ${b.result}` : ''), b.summary || '', who); break;
      case 'close':
        need(!m, 409, 'Το drone δεν έχει προσγειωθεί ακόμα');
        await this.db.update('incidents', iid, { status: 'closed', outcome: b.outcome || 'Άλλο', note: b.note || '', closed_ts: t, assigned_user: inc.assigned_user || u.id });
        await this.tl(iid, `Κλείσιμο: ${b.outcome || 'Άλλο'}` + (b.note ? ` · ${b.note}` : ''), who, 'close'); break;
      default: throw new ApiError(400, 'Άγνωστη ενέργεια');
    }
    await this.audit(who, `incident.${a}`, `#${iid}`);
    return { ok: true, incident: await this.incidentView(iid) };
  }

  async demoAlarm(orgId) {
    const orgs = await this.db.q("SELECT * FROM orgs WHERE kind='customer'" + (orgId ? ' AND id=?' : ''), orgId ? [orgId] : []);
    need(orgs.length, 404, 'Δεν υπάρχει οργανισμός');
    const org = orgs[Math.floor(Math.random() * orgs.length)];
    const cams = await this.db.q('SELECT * FROM cameras WHERE org_id=?', [org.id]); need(cams.length, 404, 'Ο οργανισμός δεν έχει κάμερες');
    const cam = cams[Math.floor(Math.random() * cams.length)];
    const [raw, target] = [['linedetection', 'human'], ['fielddetection', 'human'], ['fielddetection', 'vehicle'], ['VMD', null], ['linedetection', null]][Math.floor(Math.random() * 5)];
    const svg = demoSnapshot(org.name, cam.name, target);
    return this.ingest({ orgId: org.id, key: cam.ext_key, rawType: raw, target, name: cam.name, image: new TextEncoder().encode(svg), imageExt: 'svg', raw: `demo ${raw} ${target || ''}`, source: 'demo', edgeId: cam.edge_id, ignoreArming: true });
  }

  // ---------------- router
  async handle() {
    const p = this.url.pathname, method = this.req.method;
    if (p.startsWith('/webhook/')) return this.webhook(p);
    if (p.startsWith('/media/')) {
      await this.user();
      const r = await this.db.q1('SELECT ctype,data FROM media WHERE name=?', [decodeURIComponent(p.slice(7))]);
      need(r, 404, 'Δεν βρέθηκε');
      return new Response(Uint8Array.from(atob(r.data), c => c.charCodeAt(0)), { headers: { 'Content-Type': r.ctype, 'Cache-Control': 'private, max-age=86400' } });
    }
    let m;
    if ((m = p.match(/^\/report\/(\d+)$/))) {
      const u = await this.user(); const inc = await this.incidentView(+m[1]); need(inc, 404, 'Δεν βρέθηκε');
      const vis = await this.visible(u); need(!vis || vis.has(inc.org_id), 404, 'Δεν βρέθηκε');
      return new Response(reportHtml(inc), { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    }
    if (p.startsWith('/api/edge/')) return this.edge(p);
    if (p === '/api/login' && method === 'POST') {
      const b = await this.body();
      const u = await this.db.q1('SELECT * FROM users WHERE username=? AND active=1', [b.username || '']);
      if (!u || !(await checkPw(b.password || '', u.pw_hash))) { await new Promise(r => setTimeout(r, 600)); throw new ApiError(401, 'Λάθος όνομα χρήστη ή κωδικός'); }
      const tok = rnd(32); await this.db.ex('INSERT INTO sessions(token,user_id,expires) VALUES(?,?,?)', [tok, u.id, now() + 12 * 3600]);
      await this.db.ex('DELETE FROM sessions WHERE expires<?', [now()]);
      await this.audit(u.name, 'login');
      const secure = this.url.protocol === 'https:' ? ' Secure;' : '';
      return json({ ok: true }, 200, { 'Set-Cookie': `c2d=${tok}; HttpOnly;${secure} SameSite=Strict; Path=/; Max-Age=43200` });
    }
    if (p === '/api/logout' && method === 'POST') { await this.db.ex('DELETE FROM sessions WHERE token=?', [this.cookie('c2d')]); return json({ ok: true }, 200, { 'Set-Cookie': 'c2d=; Path=/; Max-Age=0' }); }
    if (p === '/api/me') { const u = await this.user(); return json({ id: u.id, username: u.username, name: u.name, role: u.role, org_id: u.org_id, version: VERSION, platform: 'cloudflare' }); }
    if (p === '/api/password' && method === 'POST') {
      const u = await this.user(), b = await this.body();
      need(await checkPw(b.old || '', u.pw_hash), 400, 'Λάθος τρέχων κωδικός');
      need((b.new || '').length >= 8, 400, 'Ο νέος κωδικός θέλει τουλάχιστον 8 χαρακτήρες');
      await this.db.update('users', u.id, { pw_hash: await hashPw(b.new) }); return json({ ok: true });
    }
    if (p === '/api/stream') return new Response('live stream not available on this platform', { status: 204 });
    if (p === '/api/overview') {
      const u = await this.user(); await this.tick(); const vis = await this.visible(u);
      const counts = Object.fromEntries((await this.db.q('SELECT org_id, COUNT(*) AS n, SUM(CASE WHEN online=1 AND enabled=1 THEN 1 ELSE 0 END) AS on_ FROM cameras GROUP BY org_id')).map(r => [r.org_id, r]));
      const orgs = (await this.db.q('SELECT id,name,kind,address,lat,lon,arming_mode,schedule,parent_id FROM orgs')).filter(o => !vis || vis.has(o.id))
        .map(o => ({ ...o, armed: o.kind === 'customer' ? isArmed(o) : null, cams_total: counts[o.id]?.n || 0, cams_online: counts[o.id]?.on_ || 0 }));
      const busy = await this.busyDocks();
      const docks = (await this.db.q('SELECT * FROM docks WHERE enabled=1')).map(d => ({ ...d, busy: busy.has(d.id) }));
      let missions = [];
      for (const mm of await this.db.q('SELECT m.*, i.org_id AS inc_org FROM missions m JOIN incidents i ON i.id=m.incident_id WHERE m.landed_ts IS NULL'))
        if (!vis || vis.has(mm.inc_org)) missions.push(await this.missionView(mm));
      return json({ orgs, docks, missions, incidents: await this.incidentList(vis), server_time: now(), types: Object.fromEntries(Object.entries(TYPES).map(([k, v]) => [k, v[0]])) });
    }
    if ((m = p.match(/^\/api\/incidents\/(\d+)$/)) && method === 'GET') {
      const u = await this.user(); await this.tick();
      const inc = await this.incidentView(+m[1]); need(inc, 404, 'Δεν βρέθηκε');
      const vis = await this.visible(u); need(!vis || vis.has(inc.org_id), 404, 'Δεν βρέθηκε');
      const tg = await this.target(inc);
      inc.dock_options = tg && u.role !== 'customer' ? await this.dockOptions(...tg) : [];
      inc.police_summary = this.policeSummary(inc);
      return json(inc);
    }
    if ((m = p.match(/^\/api\/incidents\/(\d+)\/action$/)) && method === 'POST') return json(await this.action(+m[1], await this.user(), await this.body()));
    if ((m = p.match(/^\/api\/incidents\/(\d+)\/speaker-audio$/)) && method === 'POST') {
      const u = await this.user(true, ['admin', 'supervisor', 'operator']); const iid = +m[1];
      const mm = await this.db.q1('SELECT * FROM missions WHERE incident_id=? AND landed_ts IS NULL', [iid]); need(mm, 409, 'Δεν υπάρχει drone σε πτήση');
      const data = await this.req.arrayBuffer(); need(data.byteLength > 200, 400, 'Κενή ηχογράφηση');
      const name = await this.saveMedia(data, 'webm'); need(name, 413, 'Πολύ μεγάλη ηχογράφηση');
      await this.tl(iid, `Μεγάφωνο: φωνητικό μήνυμα ελεγκτή (${this.url.searchParams.get('secs') || '?'}″)`, u.name, 'speaker');
      return json({ ok: true, file: name });
    }
    if (p === '/api/command' && method === 'POST') {
      const u = await this.user(true, ['admin', 'supervisor', 'operator']), b = await this.body(), cmd = b.command;
      if (cmd === 'ping') return json({ ok: true, pong: now() });
      if (cmd === 'demo_alarm') return json(await this.demoAlarm(b.org_id));
      need(COMMANDS[cmd], 400, `Άγνωστη εντολή. Δεκτές: ${[...Object.keys(COMMANDS), 'ping', 'demo_alarm'].sort().join(', ')}`);
      let iid = b.incident_id;
      if (!iid) { const r = await this.db.q1("SELECT id FROM incidents WHERE status!='closed' ORDER BY priority, first_ts LIMIT 1"); need(r, 404, 'Δεν υπάρχει ανοιχτό περιστατικό'); iid = r.id; }
      const body = { ...b, action: COMMANDS[cmd] }; if (cmd === 'light_on' || cmd === 'light_off') body.on = cmd === 'light_on';
      return json(await this.action(+iid, u, body));
    }
    if (p === '/api/demo/alarm' && method === 'POST') { await this.user(true, ['admin', 'supervisor', 'operator']); const b = await this.body().catch(() => ({})); return json(await this.demoAlarm(b.org_id)); }
    if ((m = p.match(/^\/api\/admin\/(\w+)(?:\/(\d+))?$/))) return this.admin(method, m[1], m[2]);
    throw new ApiError(404, 'Δεν βρέθηκε');
  }

  async webhook(p) {
    const m = p.match(/^\/webhook\/([\w-]+)/); need(m, 404, 'Άγνωστο webhook');
    const org = await this.db.q1('SELECT * FROM orgs WHERE webhook_token=?', [m[1]]); need(org, 404, 'Άγνωστο webhook');
    if (this.req.method === 'GET') return json({ ok: true, test: true });
    const ctype = this.req.headers.get('Content-Type') || '';
    const buf = new Uint8Array(await this.req.arrayBuffer());
    const { summary, images, raw } = parseBody(ctype, buf);
    if (!summary.eventType && !buf.length) return json({ ok: true, test: true });
    const ip = summary.ipAddress || this.req.headers.get('CF-Connecting-IP') || 'webhook';
    const ch = summary.channelID || summary.dynChannelID || '1';
    const img = images[images.length - 1] || [null, 'jpg'];
    return json(await this.ingest({ orgId: org.id, key: `${ip}:${ch}`, rawType: summary.eventType, target: summary.targetType || summary.detectionTarget,
      state: summary.eventState || 'active', name: summary.channelName, image: img[0], imageExt: img[1], raw, source: 'webhook' }));
  }

  async edge(p) {
    const edge = await this.db.q1('SELECT * FROM edges WHERE token=?', [this.bearer() || '']); need(edge, 401, 'Άγνωστο κουτί (token)');
    await this.db.update('edges', edge.id, { last_seen: now(), ip: this.req.headers.get('CF-Connecting-IP') || '' });
    const b = await this.body();
    if (p === '/api/edge/event') {
      need(b.camera_key, 400, 'Λείπει camera_key');
      return json(await this.ingest({ orgId: edge.org_id, key: b.camera_key, rawType: b.type || b.raw_type, target: b.target, state: b.state || 'active',
        name: b.camera_name, image: b.image_b64, imageExt: b.image_ext || 'jpg', raw: b.raw || '', source: `edge:${edge.name}`, edgeId: edge.id, ts: b.ts }));
    }
    if (p === '/api/edge/heartbeat') {
      await this.db.update('edges', edge.id, { version: b.version || '' });
      for (const c of b.cameras || []) {
        const cam = await this.camera(edge.org_id, c.key, c.name, edge.id);
        await this.db.update('cameras', cam.id, { last_seen: now(), online: c.online === false ? 0 : 1, edge_id: edge.id });
      }
      return json({ ok: true, server_time: now() });
    }
    throw new ApiError(404, 'Δεν βρέθηκε');
  }

  async admin(method, table, id) {
    const u = await this.user(true, method === 'GET' ? ['admin', 'supervisor'] : ['admin']);
    if (table === 'settings') {
      if (method === 'GET') { const em = await this.db.setting('email', {}) || {}; return json({ sms: await this.db.setting('sms', {}), email: em, platform: 'cloudflare' }); }
      const b = await this.body();
      if (b.sms) await this.db.setSetting('sms', b.sms);
      if (b.email) await this.db.setSetting('email', b.email);
      await this.audit(u.name, 'settings.update'); return json({ ok: true });
    }
    if (table === 'audit') return json(await this.db.q('SELECT * FROM audit ORDER BY ts DESC LIMIT 300'));
    if (table === 'events') return json(await this.db.q(`SELECT e.id,e.ts,e.type,e.raw_type,e.target,e.state,e.suppressed,e.source,e.incident_id,o.name AS org,c.name AS camera
      FROM events e LEFT JOIN orgs o ON o.id=e.org_id LEFT JOIN cameras c ON c.id=e.camera_id ORDER BY e.ts DESC LIMIT 300`));
    need(ADMIN[table], 404, 'Άγνωστος πίνακας');
    if (method === 'GET') {
      const rows = await this.db.q(`SELECT * FROM ${table} ORDER BY id`);
      for (const r of rows) {
        delete r.pw_hash;
        if (table === 'users') r.api_token = String(r.api_token || '').slice(0, 6) + '…';
        if (table === 'edges') { r.token = String(r.token || '').slice(0, 6) + '…'; r.cameras = (await this.db.q1('SELECT COUNT(*) AS n FROM cameras WHERE edge_id=?', [r.id])).n; }
        if (table === 'orgs') { r.webhook_url = r.kind === 'customer' ? `/webhook/${r.webhook_token}` : ''; delete r.webhook_token; }
      }
      return json(rows);
    }
    if (method === 'DELETE') {
      need(id, 400, 'Λείπει id'); if (table === 'users') need(+id !== u.id, 400, 'Δεν μπορείτε να διαγράψετε τον εαυτό σας');
      await this.db.ex(`DELETE FROM ${table} WHERE id=?`, [+id]); await this.audit(u.name, `${table}.delete`, id); return json({ ok: true });
    }
    const b = await this.body(), data = {};
    for (const k of ADMIN[table]) if (k in b) data[k] = (b[k] === '' && ['lat', 'lon', 'parent_id', 'org_id', 'edge_id', 'snooze_until'].includes(k)) ? null : b[k];
    const extra = {};
    if (table === 'users') {
      need(ROLES.has(data.role || 'operator'), 400, 'Άγνωστος ρόλος');
      if (b.password) { need(b.password.length >= 8, 400, 'Ο κωδικός θέλει τουλάχιστον 8 χαρακτήρες'); data.pw_hash = await hashPw(b.password); }
    }
    if (method === 'POST') {
      if (table === 'orgs') data.webhook_token = rnd(12);
      if (table === 'edges') extra.token = data.token = rnd(24);
      if (table === 'users') { need(data.pw_hash, 400, 'Ορίστε κωδικό'); extra.api_token = data.api_token = rnd(24); }
      const nid = await this.db.insert(table, data); await this.audit(u.name, `${table}.create`, String(nid));
      return json({ ok: true, id: nid, ...extra });
    }
    if (method === 'PUT') {
      need(id, 400, 'Λείπει id');
      if (table === 'edges' && b.regenerate_token) extra.token = data.token = rnd(24);
      await this.db.update(table, +id, data); await this.audit(u.name, `${table}.update`, id);
      return json({ ok: true, ...extra });
    }
    throw new ApiError(405, 'Μη επιτρεπτή μέθοδος');
  }
}

// ------------------------------------------------------------------ camera payload parsing
const XML_FIELDS = ['eventType', 'eventState', 'eventDescription', 'channelID', 'dynChannelID', 'channelName', 'dateTime', 'ipAddress', 'macAddress', 'activePostCount', 'targetType', 'detectionTarget'];
function parseXml(text) { const o = {}; for (const f of XML_FIELDS) { const m = text.match(new RegExp(`<(?:\\w+:)?${f}>([\\s\\S]*?)</(?:\\w+:)?${f}>`)); if (m) o[f] = m[1].trim(); } return o; }
function summarizeJson(j) {
  if (!j || typeof j !== 'object') return {};
  const pick = (...k) => { for (const x of k) if (j[x] != null && j[x] !== '') return j[x]; return undefined; };
  const o = { eventType: pick('eventType', 'event', 'type', 'Code', 'code', 'alarmType'), channelName: pick('camera', 'cameraName', 'channelName', 'deviceName'),
    channelID: pick('channel', 'channelID', 'index'), dateTime: pick('time', 'dateTime', 'timestamp'), eventState: pick('state', 'eventState', 'action', 'Action'),
    ipAddress: pick('ipAddress', 'ip'), targetType: pick('target', 'targetType', 'objectType', 'ObjectType') };
  for (const k of Object.keys(o)) if (o[k] === undefined) delete o[k];
  return o;
}
function dahuaLine(t) { const m = t.match(/Code=([^;\s]+);action=([^;\s]+);index=(\d+)/); return m ? { eventType: m[1], eventState: m[2].toLowerCase() === 'start' ? 'active' : 'inactive', channelID: String(+m[3] + 1) } : {}; }
function parseBody(ctype, buf) {
  const ct = ctype.toLowerCase(), images = []; let summary = {}, raw = '';
  if (!buf.length) return { summary, images, raw };
  if (ct.includes('multipart')) {
    const bm = ctype.match(/boundary="?([^";]+)"?/i); if (!bm) return { summary, images, raw };
    let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
    for (const part of bin.split('--' + bm[1])) {
      const i = part.indexOf('\r\n\r\n'); if (i < 0) continue;
      const head = part.slice(0, i).toLowerCase(); let body = part.slice(i + 4); if (body.endsWith('\r\n')) body = body.slice(0, -2);
      const ptype = (head.match(/content-type:\s*([^\r\n;]+)/) || [])[1] || '';
      if (ptype.startsWith('image/')) { images.push([Uint8Array.from(body, c => c.charCodeAt(0) & 255), ptype.includes('png') ? 'png' : 'jpg']); continue; }
      const text = new TextDecoder().decode(Uint8Array.from(body, c => c.charCodeAt(0) & 255)); raw += text.slice(0, 3000);
      if (text.includes('<') && text.includes('eventType')) Object.assign(summary, parseXml(text));
      else { try { Object.assign(summary, summarizeJson(JSON.parse(text))); } catch { Object.assign(summary, dahuaLine(text)); } }
    }
    return { summary, images, raw };
  }
  if (ct.startsWith('image/')) return { summary, images: [[buf, ct.includes('png') ? 'png' : 'jpg']], raw };
  const text = new TextDecoder().decode(buf); raw = text.slice(0, 4000);
  if (ct.includes('json') || /^\s*[{[]/.test(text)) { try { summary = summarizeJson(JSON.parse(text)); } catch { } }
  else if (ct.includes('xml') || /^\s*</.test(text)) summary = parseXml(text);
  else if (ct.includes('x-www-form-urlencoded')) { const f = Object.fromEntries(new URLSearchParams(text)); summary = Object.keys(summarizeJson(f)).length ? summarizeJson(f) : f; }
  else summary = dahuaLine(text);
  return { summary, images, raw };
}

// ------------------------------------------------------------------ html pieces
const escH = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function demoSnapshot(org, cam, target) {
  const ts = new Intl.DateTimeFormat('sv-SE', { timeZone: TZ, dateStyle: 'short', timeStyle: 'medium' }).format(new Date());
  const x = 260 + Math.floor(Math.random() * 640);
  const fig = target === 'vehicle'
    ? `<rect x="${x}" y="430" width="220" height="90" rx="18" fill="#3a4654"/><rect x="${x - 12}" y="418" width="244" height="114" fill="none" stroke="#facc15" stroke-width="4"/><text x="${x - 10}" y="408" fill="#facc15" font-size="22" font-family="Arial">vehicle 0.91</text>`
    : target === 'human'
      ? `<circle cx="${x}" cy="380" r="22" fill="#56606c"/><rect x="${x - 24}" y="404" width="48" height="110" rx="14" fill="#56606c"/><rect x="${x - 44}" y="346" width="88" height="180" fill="none" stroke="#facc15" stroke-width="4"/><text x="${x - 44}" y="336" fill="#facc15" font-size="22" font-family="Arial">human 0.94</text>`
      : `<circle cx="${x}" cy="400" r="18" fill="#56606c"/><rect x="${x - 20}" y="420" width="40" height="90" rx="12" fill="#56606c"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1b232c"/><stop offset="1" stop-color="#2b333b"/></linearGradient></defs>
<rect width="1280" height="720" fill="url(#g)"/><path d="M0 560 L1280 520 L1280 720 L0 720Z" fill="#232b33"/><path d="M0 600 L1280 470" stroke="#3b444e" stroke-width="6"/>
<rect x="80" y="260" width="300" height="250" fill="#262e37"/><rect x="900" y="230" width="320" height="290" fill="#262e37"/><rect x="960" y="300" width="70" height="90" fill="#36404a"/>
${fig}<text x="24" y="44" fill="#e6edf3" font-size="26" font-family="Arial">${escH(cam)}</text><text x="1256" y="44" fill="#e6edf3" font-size="24" font-family="Arial" text-anchor="end">${ts}</text>
<text x="24" y="700" fill="#8b9bb0" font-size="20" font-family="Arial">${escH(org)} · ΔΕΙΓΜΑ ΕΠΙΔΕΙΞΗΣ</text></svg>`;
}
function reportHtml(inc) {
  const f = t => t ? fmtTime(t, { dateStyle: 'short', timeStyle: 'medium' }) : '—', m = inc.mission;
  const imgs = inc.images.slice(-6).map(i => `<img src="/media/${encodeURIComponent(i)}" alt="">`).join('');
  const tl = inc.timeline.map(t => `<tr><td>${f(t.ts)}</td><td>${escH(t.text)}</td><td>${escH(t.who)}</td></tr>`).join('');
  return `<!doctype html><html lang="el"><head><meta charset="utf-8"><title>Αναφορά περιστατικού #${inc.id}</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;color:#1d232a;max-width:900px;margin:24px auto;padding:0 16px}h1{font-size:22px;margin:0 0 4px}.meta{color:#5b6672;margin-bottom:18px}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #dde3e9;padding:6px 8px;text-align:left;vertical-align:top}th{color:#5b6672;font-weight:600;width:200px}
.imgs{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}.imgs img{width:100%;border-radius:6px;border:1px solid #dde3e9}h2{font-size:16px;margin:22px 0 8px}@media print{button{display:none}}</style></head><body>
<button onclick="print()" style="float:right;padding:8px 14px">Εκτύπωση / PDF</button>
<h1>Αναφορά περιστατικού #${inc.id}</h1><div class="meta">${escH(inc.org.name)} · ${escH(inc.type_label)} · ${f(inc.first_ts)}</div>
<table><tr><th>Επιχείρηση</th><td>${escH(inc.org.name)}<br>${escH(inc.org.address)}</td></tr><tr><th>Κάμερες</th><td>${escH(inc.camera_list.map(c => c.name).join(', '))}</td></tr>
<tr><th>Σήματα</th><td>${inc.count}</td></tr><tr><th>Ελεγκτής</th><td>${escH(inc.assigned_name)}</td></tr>
<tr><th>Drone</th><td>${m ? escH(m.dock.name + ' · ' + (m.simulated ? 'προσομοίωση' : 'DJI Dock')) : 'Δεν στάλθηκε'}</td></tr>
<tr><th>Αποτέλεσμα</th><td>${escH(inc.outcome || 'Ανοιχτό')}</td></tr><tr><th>Σημείωση</th><td>${escH(inc.note)}</td></tr><tr><th>Κλείσιμο</th><td>${f(inc.closed_ts)}</td></tr></table>
<h2>Εικόνες</h2><div class="imgs">${imgs || '<p>Δεν υπάρχουν εικόνες.</p>'}</div><h2>Χρονολόγιο</h2><table>${tl}</table></body></html>`;
}

export async function handle(context) {
  const app = new App(context.env, context.request);
  try { return await app.handle(); }
  catch (e) {
    if (e instanceof ApiError) return json({ ok: false, error: e.message }, e.code);
    console.error(e);
    return json({ ok: false, error: `Σφάλμα server: ${e.message}` }, 500);
  }
}
