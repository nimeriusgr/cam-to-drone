// SKYWARDEN · mini camera test.
// A camera / NVR pushes its alarm to /hook/<TOKEN>; the page at /<TOKEN> shows it at once with sound.
// Storage: D1 (binding DB), table created on first use. Secret: HOOK_TOKEN.

const KEEP = 300;               // events kept
const MAX_IMG = 1_800_000;      // bytes per stored snapshot

const TYPES = {
  vmd: 'Ανίχνευση κίνησης', videomotion: 'Ανίχνευση κίνησης', motion: 'Ανίχνευση κίνησης',
  linedetection: 'Διάβαση γραμμής', crosslinedetection: 'Διάβαση γραμμής', crossline: 'Διάβαση γραμμής', tripwire: 'Διάβαση γραμμής',
  fielddetection: 'Εισβολή σε περιοχή', intrusion: 'Εισβολή σε περιοχή', crossregiondetection: 'Εισβολή σε περιοχή',
  regionentrance: 'Είσοδος σε περιοχή', regionexiting: 'Έξοδος από περιοχή', loitering: 'Περιφερόμενο άτομο',
  io: 'Είσοδος συναγερμού', alarmlocal: 'Είσοδος συναγερμού', alarminput: 'Είσοδος συναγερμού',
  tamperdetection: 'Παρέμβαση στην κάμερα', shelteralarm: 'Κάλυψη κάμερας', videoblind: 'Κάλυψη κάμερας',
  videoloss: 'Απώλεια εικόνας', scenechangedetection: 'Αλλαγή σκηνής', facedetection: 'Ανίχνευση προσώπου',
};
const typeLabel = t => TYPES[String(t || '').toLowerCase().replace(/[^a-z]/g, '')] || (t ? String(t) : 'Συναγερμός');

export async function onRequest({ request, env }) {
  const url = new URL(request.url), p = url.pathname, T = env.HOOK_TOKEN;
  if (!T) return new Response('HOOK_TOKEN missing', { status: 500 });
  try {
    if (p === `/hook/${T}` || p.startsWith(`/hook/${T}/`)) return await hook(request, env);
    if (p === `/${T}` || p === `/${T}/`) return new Response(page(T), { headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' } });
    if (p === `/api/${T}/events`) return await events(env, +url.searchParams.get('after') || 0);
    if (p === `/api/${T}/arm` && request.method === 'POST') return await setArm(request, env);
    if (p === `/api/${T}/camera`) return request.method === 'POST' ? await saveCam(request, env) : await getCam(env);
    if (p === `/api/${T}/camera/snapshot` && request.method === 'POST') return await snapshot(env);
    const im = p.match(new RegExp(`^/api/${T}/img/(\\d+)$`));
    if (im) return await image(env, +im[1]);
  } catch (e) {
    return new Response('error: ' + e.message, { status: 500 });
  }
  return new Response('Not found', { status: 404 });
}

let ready = false;
async function ensure(db) {
  if (ready) return;
  await db.exec('CREATE TABLE IF NOT EXISTS ev (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, type TEXT, state TEXT, channel TEXT, cam TEXT, ip TEXT, target TEXT, raw TEXT, img BLOB, img_type TEXT)');
  try { await db.exec('ALTER TABLE ev ADD COLUMN armed INTEGER DEFAULT 1'); } catch { }
  await db.exec('CREATE TABLE IF NOT EXISTS arm (id INTEGER PRIMARY KEY, mode TEXT, sfrom TEXT, sto TEXT, by TEXT, ts INTEGER)');
  ready = true;
}

// ------------------------------------------------------------------ arming
// mode: armed | disarmed | schedule (armed between sfrom and sto, Athens time, may cross midnight)
const athensHM = () => new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Athens', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date());
async function armState(db) {
  await ensure(db);
  const r = await db.prepare('SELECT * FROM arm WHERE id=1').first() || { mode: 'armed', sfrom: '20:00', sto: '07:00' };
  let armed = r.mode !== 'disarmed';
  if (r.mode === 'schedule') { const n = athensHM(), a = r.sfrom || '20:00', b = r.sto || '07:00'; armed = a <= b ? (n >= a && n < b) : (n >= a || n < b); }
  return { mode: r.mode, from: r.sfrom || '20:00', to: r.sto || '07:00', by: r.by || '', changed: r.ts || null, armed };
}
async function setArm(request, env) {
  const b = await request.json(), hm = v => /^d{2}:d{2}$/.test(v || '') ? v : null;
  const mode = ['armed', 'disarmed', 'schedule'].includes(b.mode) ? b.mode : 'armed';
  const cur = await armState(env.DB);
  await env.DB.prepare('INSERT OR REPLACE INTO arm (id,mode,sfrom,sto,by,ts) VALUES (1,?,?,?,?,?)')
    .bind(mode, hm(b.from) || cur.from, hm(b.to) || cur.to, String(b.by || '').trim().slice(0, 60), Date.now()).run();
  return Response.json(await armState(env.DB));
}

// ------------------------------------------------------------------ camera details
const CAM_FIELDS = ['brand', 'site', 'host', 'http_port', 'rtsp_port', 'username', 'channel', 'notes'];
async function camRow(db) {
  await db.exec('CREATE TABLE IF NOT EXISTS cam (id INTEGER PRIMARY KEY, brand TEXT, site TEXT, host TEXT, http_port TEXT, rtsp_port TEXT, username TEXT, password TEXT, channel TEXT, notes TEXT, updated INTEGER)');
  return await db.prepare('SELECT * FROM cam WHERE id=1').first() || {};
}
async function getCam(env) {
  const r = await camRow(env.DB), o = {};
  for (const f of CAM_FIELDS) o[f] = r[f] || '';
  o.has_password = !!r.password; o.updated = r.updated || null;
  return Response.json(o, { headers: { 'cache-control': 'no-store' } });
}
async function saveCam(request, env) {
  const old = await camRow(env.DB), b = await request.json();
  const v = f => String(b[f] ?? '').trim().slice(0, 300);
  const pw = b.password ? String(b.password).slice(0, 200) : (b.clear_password ? null : old.password || null);
  await env.DB.prepare(`INSERT OR REPLACE INTO cam (id,${CAM_FIELDS.join(',')},password,updated) VALUES (1,${CAM_FIELDS.map(() => '?').join(',')},?,?)`)
    .bind(...CAM_FIELDS.map(v), pw, Date.now()).run();
  return getCam(env);
}

// Pull one snapshot from the camera (Hikvision ISAPI / Dahua CGI) with Basic or Digest auth.
async function snapshot(env) {
  const c = await camRow(env.DB);
  if (!c.host) return Response.json({ error: 'Συμπληρώστε πρώτα τη διεύθυνση της κάμερας.' }, { status: 400 });
  const ch = (c.channel || '1').replace(/\D/g, '') || '1';
  const port = c.http_port ? ':' + c.http_port.replace(/\D/g, '') : '';
  const host = c.host.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
  const scheme = /^https:/.test(c.host) || c.http_port === '443' ? 'https' : 'http';
  const path = /dahua/i.test(c.brand) ? `/cgi-bin/snapshot.cgi?channel=${ch}` : `/ISAPI/Streaming/channels/${ch}01/picture`;
  const url = `${scheme}://${host}${port}${path}`;
  try {
    let r = await fetch(url, { headers: c.username ? { authorization: 'Basic ' + btoa(`${c.username}:${c.password || ''}`) } : {}, signal: AbortSignal.timeout(12000) });
    const ch401 = r.status === 401 && r.headers.get('www-authenticate');
    if (ch401 && /digest/i.test(ch401)) r = await fetch(url, { headers: { authorization: await digest(ch401, 'GET', path, c.username || '', c.password || '') }, signal: AbortSignal.timeout(12000) });
    const ct = r.headers.get('content-type') || '';
    if (r.ok && ct.startsWith('image/')) return new Response(r.body, { headers: { 'content-type': ct, 'cache-control': 'no-store' } });
    const msg = r.status === 401 ? 'Η κάμερα απάντησε, αλλά δεν δέχτηκε όνομα χρήστη / κωδικό.' : `Η κάμερα απάντησε με κωδικό ${r.status}.`;
    return Response.json({ error: msg, url: url.replace(/\/\/[^/]*@/, '//') }, { status: 502 });
  } catch (e) {
    return Response.json({ error: 'Δεν έγινε σύνδεση με την κάμερα: ' + (e.message || e) + '. Ελέγξτε IP / θύρα και ότι η θύρα είναι ανοιχτή στο router.', url }, { status: 502 });
  }
}
async function md5(s) { return [...new Uint8Array(await crypto.subtle.digest('MD5', new TextEncoder().encode(s)))].map(b => b.toString(16).padStart(2, '0')).join(''); }
async function digest(header, method, uri, user, pass) {
  const p = {}; for (const m of header.matchAll(/(\w+)="?([^",]*)"?/g)) p[m[1]] = m[2];
  const nc = '00000001', cnonce = crypto.randomUUID().replace(/-/g, '').slice(0, 16), qop = (p.qop || '').split(',')[0].trim();
  const ha1 = await md5(`${user}:${p.realm}:${pass}`), ha2 = await md5(`${method}:${uri}`);
  const resp = qop ? await md5(`${ha1}:${p.nonce}:${nc}:${cnonce}:${qop}:${ha2}`) : await md5(`${ha1}:${p.nonce}:${ha2}`);
  return `Digest username="${user}", realm="${p.realm}", nonce="${p.nonce}", uri="${uri}", response="${resp}"` +
    (qop ? `, qop=${qop}, nc=${nc}, cnonce="${cnonce}"` : '') + (p.opaque ? `, opaque="${p.opaque}"` : '');
}

async function hook(request, env) {
  const arm = await armState(env.DB);
  const buf = new Uint8Array(await request.arrayBuffer());
  const ctype = request.headers.get('content-type') || '';
  const url = new URL(request.url);
  let { summary, images, raw } = parseBody(ctype, buf);
  if (!Object.keys(summary).length && url.search) summary = summarizeJson(Object.fromEntries(url.searchParams)); // GET ?event=... style
  const st = String(summary.eventState || 'active').toLowerCase();
  const state = /inactive|stop|end|false|0/.test(st) ? 'inactive' : 'active';
  const img = images.find(i => i[0].length <= MAX_IMG);
  const ip = summary.ipAddress || request.headers.get('CF-Connecting-IP') || '';
  await env.DB.prepare('INSERT INTO ev (ts,type,state,channel,cam,ip,target,raw,img,img_type,armed) VALUES (?,?,?,?,?,?,?,?,?,?,?)')
    .bind(Date.now(), String(summary.eventType || ''), state, String(summary.channelID || summary.dynChannelID || ''),
      String(summary.channelName || ''), ip, String(summary.targetType || summary.detectionTarget || ''),
      (raw || `${request.method} ${url.pathname}${url.search}`).slice(0, 4000), img ? img[0] : null, img ? (img[1] === 'png' ? 'image/png' : 'image/jpeg') : null, arm.armed ? 1 : 0).run();
  await env.DB.prepare('DELETE FROM ev WHERE id <= (SELECT MAX(id) FROM ev) - ?').bind(KEEP).run();
  return new Response('OK', { headers: { 'content-type': 'text/plain' } });
}

async function events(env, after) {
  await ensure(env.DB);
  const { results } = await env.DB.prepare('SELECT id,ts,type,state,channel,cam,ip,target,raw,armed,img IS NOT NULL AS has_img FROM ev WHERE id > ? ORDER BY id DESC LIMIT 60').bind(after).run();
  for (const r of results) r.label = typeLabel(r.type);
  const last = await env.DB.prepare('SELECT MAX(ts) AS t FROM ev').first();
  return Response.json({ events: results, arm: await armState(env.DB), last_contact: last?.t || null, now: Date.now() }, { headers: { 'cache-control': 'no-store' } });
}

async function image(env, id) {
  const r = await env.DB.prepare('SELECT img, img_type FROM ev WHERE id=?').bind(id).first();
  if (!r?.img) return new Response('Not found', { status: 404 });
  return new Response(new Uint8Array(r.img), { headers: { 'content-type': r.img_type, 'cache-control': 'private, max-age=86400' } });
}

// ------------------------------------------------------------------ camera payload parsing (same as SKYWARDEN)
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

// ------------------------------------------------------------------ page
function page(T) {
  return `<!doctype html><html lang="el"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SKYWARDEN · Τεστ κάμερας</title>
<style>
:root{--bg:#0a0f15;--panel:#111821;--line:#1f2a36;--tx:#e6edf3;--mut:#8b9bb0;--acc:#38bdf8;--red:#ef4444;--ok:#22c55e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 "Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.brand{font-weight:800;letter-spacing:.22em;font-size:18px}.brand small{font-weight:500;letter-spacing:.05em;color:var(--mut);font-size:13px;margin-left:8px}
.pill{margin-left:auto;display:flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--line);border-radius:999px;font-size:13px;color:var(--mut)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut)}.dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
button{background:var(--acc);color:#04121c;border:0;border-radius:8px;padding:9px 14px;font-weight:700;cursor:pointer;font-size:14px}
button.ghost{background:transparent;color:var(--tx);border:1px solid var(--line)}
main{max-width:1100px;margin:0 auto;padding:20px;display:grid;grid-template-columns:minmax(0,1fr);gap:18px}
.alarm{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.alarm.hot{border-color:var(--red);animation:flash 1s ease-in-out 6}
@keyframes flash{50%{box-shadow:0 0 0 4px rgba(239,68,68,.45)}}
.alarm .top{display:flex;gap:12px;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.tag{background:var(--red);color:#fff;font-weight:800;font-size:12px;letter-spacing:.08em;padding:4px 8px;border-radius:5px}
.tag.idle{background:var(--line);color:var(--mut)}
.alarm h2{margin:0;font-size:20px}.alarm .when{margin-left:auto;color:var(--mut)}
.alarm .body{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr)}
.shot{background:#000;aspect-ratio:16/9;display:grid;place-items:center;color:var(--mut)}.shot img{width:100%;height:100%;object-fit:contain}
dl{margin:0;padding:16px 18px;display:grid;grid-template-columns:auto 1fr;gap:8px 14px;align-content:start}dt{color:var(--mut)}dd{margin:0;word-break:break-word}
.waiting{padding:60px 20px;text-align:center;color:var(--mut)}.waiting b{display:block;color:var(--tx);font-size:20px;margin-bottom:6px}
h3{margin:4px 0 10px;font-size:13px;letter-spacing:.12em;color:var(--mut);font-weight:700}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:14px}
td,th{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--mut);font-weight:600}tr.in td{color:var(--mut)}
tr{cursor:pointer}tr:hover td{background:#16202b}
details{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 16px}summary{cursor:pointer;font-weight:700}
code{background:#0b1219;border:1px solid var(--line);padding:2px 6px;border-radius:5px;word-break:break-all}
pre{background:#0b1219;border:1px solid var(--line);padding:10px;border-radius:8px;white-space:pre-wrap;word-break:break-all;max-height:220px;overflow:auto;font-size:12px;color:var(--mut)}
.arm{display:flex;gap:16px;align-items:center;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.arm .st{display:flex;align-items:center;gap:12px;min-width:240px}.arm .ic{width:46px;height:46px;border-radius:12px;display:grid;place-items:center;font-size:22px;background:var(--line)}
.arm.on .ic{background:rgba(239,68,68,.18);color:var(--red)}.arm.off .ic{background:rgba(34,197,94,.15);color:var(--ok)}
.arm .big{font-size:20px;font-weight:800;letter-spacing:.06em}.arm.on .big{color:var(--red)}.arm.off .big{color:var(--ok)}
.arm .who{color:var(--mut);font-size:13px}.seg{display:flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-left:auto}
.seg button{background:transparent;color:var(--tx);border-radius:0;border-right:1px solid var(--line);padding:10px 16px}.seg button:last-child{border-right:0}
.seg button.sel{background:var(--acc);color:#04121c}.sched{display:flex;gap:8px;align-items:center;color:var(--mut);font-size:13px}.sched select{width:auto;min-width:96px;padding:7px 10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.hint{color:var(--mut);font-size:13px;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.wide{grid-column:1/-1}
label{display:grid;gap:5px;font-size:13px;color:var(--mut)}
input,select{background:#0b1219;color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit;font-size:16px;width:100%}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row .hint{margin:0}
#snapout img{max-width:100%;border-radius:8px;margin-top:12px;border:1px solid var(--line)}#snapout .err{color:#fca5a5;margin-top:12px}
@media(max-width:760px){.alarm .body{grid-template-columns:1fr}.hide-m{display:none}.grid{grid-template-columns:1fr 1fr}
 main{padding:16px}header{padding:12px 16px}.pill{margin-left:0}.arm .st{min-width:0}.seg{margin-left:0;width:100%}.seg button{flex:1;padding:10px 6px}.sched{flex-wrap:wrap}}
</style></head><body>
<header><div class="brand">SKYWARDEN<small>Τεστ κάμερας · Adesco</small></div>
<div class="pill"><span class="dot" id="dot"></span><span id="contact">Αναμονή πρώτου σήματος από την κάμερα</span></div>
<button id="enable">Ενεργοποίηση ήχου &amp; ειδοποιήσεων</button></header>
<main>
<section class="arm" id="arm"><div class="st"><div class="ic" id="armic">●</div><div><div class="big" id="armtxt">…</div><div class="who" id="armwho"></div></div></div>
<div class="sched" id="sched" hidden>Οπλισμένο από <select id="sfrom"></select> έως <select id="sto"></select></div>
<div class="seg"><button data-m="armed">Όπλιση</button><button data-m="disarmed">Αφόπλιση</button><button data-m="schedule">Ωράριο</button></div></section>
<section class="alarm" id="alarm"><div class="waiting"><b>Σε αναμονή συναγερμού</b>Μόλις η κάμερα στείλει σήμα, εμφανίζεται εδώ με ήχο.</div></section>
<section><h3>ΙΣΤΟΡΙΚΟ ΣΗΜΑΤΩΝ</h3><table><thead><tr><th>Ώρα</th><th>Συμβάν</th><th class="hide-m">Κανάλι</th><th class="hide-m">IP</th><th>Κατάσταση</th></tr></thead><tbody id="rows"><tr><td colspan="5" style="color:var(--mut)">Κανένα σήμα ακόμα.</td></tr></tbody></table></section>
<section class="card"><h3>ΣΤΟΙΧΕΙΑ ΚΑΜΕΡΑΣ</h3>
<form id="camf" class="grid">
<label>Μάρκα<select name="brand"><option>Hikvision</option><option>Dahua</option><option>Άλλη</option></select></label>
<label>Χώρος / περιγραφή<input name="site" placeholder="π.χ. Αποθήκη, πύλη φόρτωσης"></label>
<label>Εξωτερική IP ή DDNS<input name="host" placeholder="π.χ. 85.72.10.4 ή adesco.ddns.net"></label>
<label>Θύρα HTTP<input name="http_port" inputmode="numeric" placeholder="80"></label>
<label>Θύρα RTSP<input name="rtsp_port" inputmode="numeric" placeholder="554"></label>
<label>Κανάλι<input name="channel" inputmode="numeric" placeholder="1"></label>
<label>Όνομα χρήστη<input name="username" autocomplete="off"></label>
<label>Κωδικός<input name="password" type="password" autocomplete="new-password" placeholder=""></label>
<div class="wide row"><button type="submit">Αποθήκευση</button><button type="button" class="ghost" id="snap">Δοκιμή σύνδεσης (λήψη εικόνας)</button><span id="camst" class="hint"></span></div>
</form><div id="snapout"></div></section>
<section class="card"><label>Διεύθυνση σήματος κάμερας<div class="row"><input id="hookurl" readonly onclick="this.select()" style="flex:1;min-width:0"><button type="button" class="ghost" id="copyhook">Αντιγραφή</button></div></label></section>
</main>
<script>
const T=${JSON.stringify(T)};let after=0,all=[],first=true,audio=null;
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>new Date(t).toLocaleString('el-GR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
$('hookurl').value=location.origin+'/hook/'+T;$('copyhook').onclick=async()=>{try{await navigator.clipboard.writeText($('hookurl').value);$('copyhook').textContent='Αντιγράφηκε ✓'}catch{$('hookurl').select()}};
$('enable').onclick=async()=>{audio=audio||new (window.AudioContext||window.webkitAudioContext)();await audio.resume();
 if('Notification'in window&&Notification.permission==='default')try{await Notification.requestPermission()}catch{}
 $('enable').textContent='Ήχος ενεργός ✓';$('enable').className='ghost';};
function siren(){if(!audio)return;const t=audio.currentTime;for(let i=0;i<6;i++){const o=audio.createOscillator(),g=audio.createGain();o.type='square';
 o.frequency.setValueAtTime(i%2?660:990,t+i*.28);g.gain.setValueAtTime(.12,t+i*.28);g.gain.setValueAtTime(0,t+i*.28+.24);o.connect(g).connect(audio.destination);o.start(t+i*.28);o.stop(t+i*.28+.26)}}
function show(e,hot){const a=$('alarm');a.className='alarm'+(hot?' hot':'');
 a.innerHTML='<div class="top"><span class="tag'+(e.state==='active'&&e.armed?'':' idle')+'">'+(e.state!=='active'?'ΤΕΛΟΣ ΣΥΜΒΑΝΤΟΣ':e.armed?'ΣΥΝΑΓΕΡΜΟΣ':'ΑΦΟΠΛΙΣΜΕΝΟ · ΜΟΝΟ ΚΑΤΑΓΡΑΦΗ')+'</span><h2>'+esc(e.label)+'</h2><span class="when">'+fmt(e.ts)+'</span></div>'+
 '<div class="body"><div class="shot">'+(e.has_img?'<img src="/api/'+T+'/img/'+e.id+'" alt="">':'Η κάμερα δεν έστειλε φωτογραφία')+'</div><dl>'+
 '<dt>Κάμερα</dt><dd>'+esc(e.cam||'—')+'</dd><dt>Κανάλι</dt><dd>'+esc(e.channel||'—')+'</dd><dt>IP</dt><dd>'+esc(e.ip||'—')+'</dd>'+
 (e.target?'<dt>Στόχος</dt><dd>'+esc(e.target)+'</dd>':'')+'<dt>Κωδικός</dt><dd>'+esc(e.type||'—')+'</dd>'+
 '<dt style="grid-column:1/-1">Τι έστειλε η κάμερα</dt><dd style="grid-column:1/-1"><pre>'+esc(e.raw)+'</pre></dd></dl></div>';}
function table(){$('rows').innerHTML=all.length?all.slice(0,60).map(e=>'<tr data-id="'+e.id+'" class="'+(e.state==='active'&&e.armed?'':'in')+'"><td>'+fmt(e.ts)+'</td><td>'+esc(e.label)+'</td><td class="hide-m">'+esc(e.channel||'—')+'</td><td class="hide-m">'+esc(e.ip||'—')+'</td><td>'+(e.state!=='active'?'Τέλος / παλμός':e.armed?'Συναγερμός':'Καταγραφή (αφοπλισμένο)')+'</td></tr>').join(''):'<tr><td colspan="5" style="color:var(--mut)">Κανένα σήμα ακόμα.</td></tr>';
 for(const r of $('rows').children)r.onclick=()=>{const e=all.find(x=>x.id==r.dataset.id);if(e){show(e,false);scrollTo({top:0,behavior:'smooth'})}};}
async function poll(){try{const r=await fetch('/api/'+T+'/events?after='+after,{cache:'no-store'});const d=await r.json();
 if(d.last_contact){const ago=Math.round((d.now-d.last_contact)/1000);$('dot').className='dot'+(ago<600?' on':'');$('contact').textContent='Τελευταία επαφή κάμερας: '+fmt(d.last_contact);}
 if(d.events.length){after=d.events[0].id;all=d.events.concat(all).slice(0,300);table();
  const act=d.events.find(e=>e.state==='active'&&e.armed);const quiet=d.events.find(e=>e.state==='active'&&!e.armed);if(!act&&quiet&&!first)show(quiet,false);
  if(act&&!first){show(act,true);siren();if(window.Notification&&Notification.permission==='granted'){try{new Notification('SKYWARDEN · '+act.label,{body:(act.cam||'Κάμερα')+' · '+fmt(act.ts),tag:'skw'})}catch{}}document.title='🔴 '+act.label;setTimeout(()=>document.title='SKYWARDEN · Τεστ κάμερας',15000);}
  else if(first){const l=all.find(e=>e.state==='active');if(l)show(l,false);}}
 if(d.arm)armUI(d.arm);
 first=false;}catch{}setTimeout(poll,2000);}
for(const id of ['sfrom','sto']){const sel=$(id);for(let m=0;m<1440;m+=30){const v=String(m/60|0).padStart(2,'0')+':'+String(m%60).padStart(2,'0');sel.add(new Option(v,v))}}
const MODES={armed:'ΟΠΛΙΣΜΕΝΟ',disarmed:'ΑΦΟΠΛΙΣΜΕΝΟ'};let editing=false;
function armUI(a){$('arm').className='arm '+(a.armed?'on':'off');$('armic').textContent=a.armed?'⛨':'○';
 $('armtxt').textContent=MODES[a.armed?'armed':'disarmed']+(a.mode==='schedule'?' · ΩΡΑΡΙΟ':'');
 $('armwho').textContent=(a.mode==='schedule'?'Οπλισμένο '+a.from+'–'+a.to+'. ':'')+(a.changed?'Αλλαγή'+(a.by?' από '+a.by:'')+', '+fmt(a.changed):'');
 for(const b of document.querySelectorAll('.seg button'))b.className=b.dataset.m===a.mode?'sel':'';
 $('sched').hidden=a.mode!=='schedule';if(!editing){for(const [id,v] of [['sfrom',a.from],['sto',a.to]]){const sel=$(id);if(![...sel.options].some(o=>o.value===v))sel.add(new Option(v,v));sel.value=v;}}}
function who(){let n='';try{n=localStorage.getItem('skw_name')||''}catch{}if(!n){n=(prompt('Το όνομά σας (για το ιστορικό αλλαγών):')||'').trim();try{if(n)localStorage.setItem('skw_name',n)}catch{}}return n;}
async function setArm(mode){const r=await fetch('/api/'+T+'/arm',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({mode,from:$('sfrom').value,to:$('sto').value,by:who()})});if(r.ok)armUI(await r.json());}
for(const b of document.querySelectorAll('.seg button'))b.onclick=()=>setArm(b.dataset.m);
for(const i of [$('sfrom'),$('sto')]){i.onfocus=()=>editing=true;i.onblur=()=>editing=false;i.onchange=()=>{editing=false;setArm('schedule')};}
poll();
const F=$('camf');
async function loadCam(){const c=await (await fetch('/api/'+T+'/camera',{cache:'no-store'})).json();
 for(const k of ['brand','site','host','http_port','rtsp_port','channel','username'])if(F[k])F[k].value=c[k]||(k==='brand'?'Hikvision':'');
 F.password.value='';F.password.placeholder=c.has_password?'αποθηκευμένος (αφήστε κενό)':'';
 $('camst').textContent=c.updated?'Αποθηκεύτηκε '+fmt(c.updated):'';}
F.onsubmit=async e=>{e.preventDefault();const b=Object.fromEntries(new FormData(F));
 const r=await fetch('/api/'+T+'/camera',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)});
 $('camst').textContent=r.ok?'Αποθηκεύτηκε ✓':'Σφάλμα αποθήκευσης';if(r.ok)loadCam();};
$('snap').onclick=async()=>{const o=$('snapout');o.innerHTML='<p class="hint" style="margin-top:12px">Σύνδεση με την κάμερα…</p>';
 const r=await fetch('/api/'+T+'/camera/snapshot',{method:'POST'});
 if(r.ok&&(r.headers.get('content-type')||'').startsWith('image/')){o.innerHTML='<img src="'+URL.createObjectURL(await r.blob())+'" alt=""><p class="hint">Η κάμερα απαντά ✓ '+fmt(Date.now())+'</p>';}
 else{let d={};try{d=await r.json()}catch{}o.innerHTML='<p class="err">'+esc(d.error||('Σφάλμα '+r.status))+'</p>'+(d.url?'<p class="hint">'+esc(d.url)+'</p>':'');}};
loadCam();
</script></body></html>`;
}
