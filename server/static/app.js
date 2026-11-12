/* Cam to Drone · operator console */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hms = t => t ? new Date(t * 1000).toLocaleTimeString('el-GR', { hour12: false }) : '';
const hm = t => t ? new Date(t * 1000).toLocaleTimeString('el-GR', { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
const dur = s => { s = Math.max(0, Math.floor(s)); const m = Math.floor(s / 60); return m >= 60 ? `${Math.floor(m / 60)}ω ${m % 60}′` : `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };
const km = m => m >= 1000 ? (m / 1000).toFixed(1) + ' km' : Math.round(m) + ' m';
const store = { get: k => { try { return localStorage.getItem(k) } catch { return null } }, set: (k, v) => { try { localStorage.setItem(k, v) } catch { } } };

const STATUS = { new: 'ΝΕΟ', ack: 'σε χειρισμό', dispatched: 'drone σε αποστολή', closed: 'κλειστό' };
const PRIO = { 1: 'Υψηλή', 2: 'Μεσαία', 3: 'Χαμηλή', 4: 'Τεχνικό' };
const PHASE = { takeoff: 'Απογείωση', enroute: 'Σε πορεία', onsite: 'Στο σημείο', returning: 'Επιστροφή', landed: 'Προσγειώθηκε' };
const CAM_MODE = { wide: 'Κανονική', zoom: 'Ζουμ', ir: 'Θερμική' };
const OUTCOMES = ['Ψευδής συναγερμός', 'Εισβολή / κλοπή', 'Απόπειρα / απομακρύνθηκαν', 'Φωτιά', 'Τεχνικό πρόβλημα', 'Άλλο'];

const S = { me: null, ov: null, sel: null, inc: null, filter: 'open', org: '', seen: null, sound: false, actx: null, map: null, layers: {}, imgSel: null, rec: null, fitted: false };

async function api(path, body, method) {
  const opt = body !== undefined ? { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : { method: method || 'GET' };
  const r = await fetch(path, { ...opt, cache: 'no-store', credentials: 'same-origin' });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('Απαιτείται σύνδεση'); }
  if (!r.ok) throw new Error(j.error || `Σφάλμα ${r.status}`);
  return j;
}
function toast(msg, kind = '') {
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), kind === 'alarm' ? 7000 : 4000);
}

/* ------------------------------------------------------------ sound */
function beep(kind = 'alarm') {
  if (!S.sound || !S.actx) return;
  const seq = kind === 'alarm' ? [[880, 0], [660, .18], [880, .36], [660, .54]] : [[520, 0], [520, .3]];
  seq.forEach(([f, t]) => {
    const o = S.actx.createOscillator(), g = S.actx.createGain();
    o.type = 'square'; o.frequency.value = f; g.gain.value = .05;
    o.connect(g); g.connect(S.actx.destination); o.start(S.actx.currentTime + t); o.stop(S.actx.currentTime + t + .15);
  });
}
$('#soundBtn').onclick = () => {
  S.sound = !S.sound;
  if (S.sound && !S.actx) S.actx = new (window.AudioContext || window.webkitAudioContext)();
  $('#soundBtn').textContent = S.sound ? 'Ήχος: ανοιχτός' : 'Ήχος: κλειστός';
  $('#soundBtn').classList.toggle('on', S.sound);
  if (S.sound) beep('info');
};
$('#soundBtn').textContent = 'Ήχος: κλειστός';

/* ------------------------------------------------------------ auth */
function showLogin() { $('#app').hidden = true; $('#login').hidden = false; $('#lu').focus(); }
$('#loginForm').onsubmit = async e => {
  e.preventDefault(); $('#lerr').textContent = '';
  try { await api('/api/login', { username: $('#lu').value.trim(), password: $('#lp').value }); $('#lp').value = ''; boot(); }
  catch (er) { $('#lerr').textContent = er.message; }
};
$('#avatar').onclick = () => { $('#userMenu').hidden = !$('#userMenu').hidden; };
document.addEventListener('click', e => { if (!$('#userBox').contains(e.target)) $('#userMenu').hidden = true; });
$('#logoutBtn').onclick = async () => { await api('/api/logout', {}).catch(() => { }); location.reload(); };
$('#pwBtn').onclick = () => modal(`<h2>Αλλαγή κωδικού</h2>
  <label for="pw0">Τρέχων κωδικός</label><input id="pw0" type="password" autocomplete="current-password">
  <label for="pw1">Νέος κωδικός (τουλάχιστον 8 χαρακτήρες)</label><input id="pw1" type="password" autocomplete="new-password">`,
  'Αλλαγή', async () => { await api('/api/password', { old: $('#pw0').value, new: $('#pw1').value }); toast('Ο κωδικός άλλαξε'); });

async function boot() {
  try { S.me = await api('/api/me'); } catch { return; }
  $('#login').hidden = true; $('#app').hidden = false;
  $('#avatar').textContent = S.me.name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
  $('#userName').textContent = `${S.me.name} · ${({ admin: 'Διαχειριστής', supervisor: 'Επόπτης', operator: 'Ελεγκτής', customer: 'Πελάτης' })[S.me.role]}`;
  $('#navAdmin').hidden = !['admin', 'supervisor'].includes(S.me.role);
  $('#demoBtn').hidden = S.me.role === 'customer';
  document.body.classList.toggle('customer', S.me.role === 'customer');
  S.cloud = S.me.platform === 'cloudflare';
  initMap();
  await refresh();
  if (!S.cloud) connectStream();
}

/* ------------------------------------------------------------ navigation */
$$('#nav button').forEach(b => b.onclick = () => {
  $$('#nav button').forEach(x => x.classList.toggle('on', x === b));
  const v = b.dataset.view;
  $('#console').hidden = v !== 'console'; $('#admin').hidden = v !== 'admin';
  if (v === 'admin') window.Admin && Admin.open(); else setTimeout(() => S.map && S.map.invalidateSize(), 50);
});
$('#demoBtn').onclick = async () => { try { const r = await api('/api/demo/alarm', {}); if (r.suppressed) toast('Ο συναγερμός φιλτραρίστηκε: ' + r.suppressed); } catch (e) { toast(e.message, 'err'); } };

/* ------------------------------------------------------------ data refresh */
let refreshTimer = null;
function scheduleRefresh(delay = 200) { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, delay); }
async function refresh() {
  try { S.ov = await api('/api/overview'); } catch { return; }
  const ids = new Set(S.ov.incidents.map(i => i.id));
  const fresh = S.seen ? S.ov.incidents.filter(i => !S.seen.has(i.id) && i.status === 'new') : [];
  S.seen = ids;
  if (fresh.length) {
    beep('alarm');
    fresh.forEach(i => toast(`Νέος συναγερμός: ${i.org_name} · ${i.type_label} · ${i.camera_name || ''}`, 'alarm'));
    if (!S.sel) select(fresh[0].id);
  }
  renderStats(); renderOrgFilter(); renderQueue(fresh.map(i => i.id)); renderMap();
  if (S.sel) await loadIncident(); else renderIdle();
}
async function loadIncident() {
  if (!S.sel) return;
  try { S.inc = await api(`/api/incidents/${S.sel}`); }
  catch { S.sel = null; S.inc = null; renderIdle(); return; }
  renderIncident();
}
function select(id) {
  S.sel = id; S.imgSel = null; S.fitted = false;
  $$('.q-item').forEach(x => x.classList.toggle('sel', +x.dataset.id === id));
  loadIncident().then(renderMap);
}

/* ------------------------------------------------------------ live stream */
function connectStream() {
  const es = new EventSource('/api/stream');
  es.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.kind === 'escalation') { beep('info'); toast(`Κλιμάκωση: περιστατικό #${m.id} χωρίς ανάληψη`, 'err'); }
    if (m.kind === 'mission') { if (S.inc && S.inc.mission && m.incident_id === S.sel) loadIncident(); scheduleRefresh(800); return; }
    scheduleRefresh();
  };
  es.onerror = () => { $('#stats').dataset.offline = '1'; };
  es.onopen = () => { delete $('#stats').dataset.offline; };
}
// Python server pushes changes live; on Cloudflare Pages the console polls (and pauses while the tab is hidden).
let tickN = 0;
setInterval(() => {
  tickN++;
  $('#clock').textContent = new Date().toLocaleTimeString('el-GR', { hour12: false });
  $$('[data-age]').forEach(el => el.textContent = dur(Date.now() / 1000 - +el.dataset.age));
  if ($('#app').hidden || !S.me || document.hidden) return;
  const flying = S.inc && S.inc.mission && S.inc.mission.phase !== 'landed';
  if (S.cloud) {
    if (tickN % 3 === 0) refresh();
    else if (flying && tickN % 3 === 1) loadIncident();
  } else {
    if (tickN % 10 === 0) refresh();
    if (flying) loadIncident();
  }
}, 1000);

/* ------------------------------------------------------------ top stats */
function renderStats() {
  const o = S.ov, open = o.incidents.filter(i => i.status !== 'closed');
  const docksFree = o.docks.filter(d => !d.busy).length;
  const cams = o.orgs.reduce((a, x) => a + x.cams_total, 0), camsOn = o.orgs.reduce((a, x) => a + x.cams_online, 0);
  const cust = o.orgs.filter(x => x.kind === 'customer'), armed = cust.filter(x => x.armed).length;
  $('#stats').innerHTML = `
    <span class="stat"><span class="dot ${open.some(i => i.status === 'new') ? 'red' : 'grey'}"></span>Ανοιχτά <b>${open.length}</b></span>
    <span class="stat"><span class="dot ${docksFree ? '' : 'amber'}"></span>Drones <b>${docksFree}/${o.docks.length}</b></span>
    <span class="stat"><span class="dot ${camsOn < cams ? 'amber' : ''}"></span>Κάμερες <b>${camsOn}/${cams}</b></span>
    <span class="stat"><span class="dot ${armed ? '' : 'grey'}"></span>Οπλισμένες <b>${armed}/${cust.length}</b></span>`;
}

/* ------------------------------------------------------------ queue */
$$('#qFilters .chip').forEach(c => c.onclick = () => { S.filter = c.dataset.f; $$('#qFilters .chip').forEach(x => x.classList.toggle('on', x === c)); renderQueue([]); });
$('#orgFilter').onchange = e => { S.org = e.target.value; renderQueue([]); };
function renderOrgFilter() {
  const cur = $('#orgFilter').value, custs = S.ov.orgs.filter(o => o.kind === 'customer');
  const html = '<option value="">Όλες οι επιχειρήσεις</option>' + custs.map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join('');
  if ($('#orgFilter').dataset.h !== html) { $('#orgFilter').innerHTML = html; $('#orgFilter').dataset.h = html; $('#orgFilter').value = cur; }
}
function renderQueue(flash) {
  let list = S.ov.incidents;
  if (S.org) list = list.filter(i => i.org_id === +S.org);
  const counts = { open: list.filter(i => i.status !== 'closed').length, high: list.filter(i => i.status !== 'closed' && i.priority === 1).length, drone: list.filter(i => i.flying).length, closed: list.filter(i => i.status === 'closed').length };
  $$('#qFilters .chip').forEach(c => { c.innerHTML = c.textContent.replace(/\d+$/, '').trim() + `<span class="n">${counts[c.dataset.f]}</span>`; });
  list = list.filter(i => S.filter === 'closed' ? i.status === 'closed' : i.status !== 'closed' && (S.filter !== 'high' || i.priority === 1) && (S.filter !== 'drone' || i.flying));
  $('#queue').innerHTML = list.map(i => `
    <button class="q-item p${i.priority} ${i.status === 'new' ? 'new' : ''} ${i.id === S.sel ? 'sel' : ''} ${flash.includes(i.id) ? 'flash' : ''}" data-id="${i.id}">
      <span class="bar"></span>
      <span class="th" style="${i.thumb ? `background-image:url('/media/${encodeURIComponent(i.thumb)}')` : ''}"></span>
      <span style="min-width:0"><div class="q-t">${esc(i.org_name)}</div>
        <div class="q-s">${esc(i.type_label)} · ${esc(i.camera_name || '')}${i.count > 1 ? ` · ×${i.count}` : ''}</div>
        <div class="q-s">${i.assigned_name ? esc(i.assigned_name) : 'χωρίς ανάληψη'}${i.flying ? ' · drone στον αέρα' : ''}</div></span>
      <span class="q-r">${i.status === 'closed' ? hm(i.closed_ts) : `<span data-age="${i.first_ts}">${dur(S.ov.server_time - i.first_ts)}</span>`}
        <span class="pill ${i.escalated && i.status === 'new' ? 'esc' : i.status}">${i.escalated && i.status === 'new' ? 'ΚΛΙΜΑΚΩΣΗ' : STATUS[i.status]}</span></span>
    </button>`).join('') || `<div class="empty">${S.filter === 'closed' ? 'Κανένα κλειστό περιστατικό.' : 'Κανένα ανοιχτό περιστατικό. Οι νέοι συναγερμοί εμφανίζονται εδώ με ήχο.'}</div>`;
  $$('#queue .q-item').forEach(b => b.onclick = () => select(+b.dataset.id));
}

/* ------------------------------------------------------------ idle view (no incident selected) */
function renderIdle() {
  $('#camTitle').textContent = ''; $('#droneTitle').textContent = '';
  $('#camView').innerHTML = '<div class="empty">Επιλέξτε περιστατικό από τη λίστα</div>'; $('#camThumbs').innerHTML = '';
  const flying = S.ov ? S.ov.missions : [];
  $('#droneView').innerHTML = `<div class="empty">${flying.length ? `${flying.length} drone σε αποστολή` : 'Κανένα drone σε αποστολή'}</div>`; $('#droneBar').innerHTML = '';
  if (!S.ov) return;
  const custs = S.ov.orgs.filter(o => o.kind === 'customer');
  $('#side').innerHTML = `
    <div class="sec"><h3>Επιχειρήσεις <span class="r mut">${custs.length}</span></h3>
      ${custs.map(o => `<div class="org-row"><span class="dot ${o.armed ? (o.cams_online < o.cams_total ? 'amber' : '') : 'grey'}"></span>
        <span style="min-width:0"><div class="q-t">${esc(o.name)}</div><div class="q-s">${esc(o.address)}</div></span>
        <span class="mut" style="text-align:right">${o.armed ? 'Οπλισμένη' : 'Αφόπλιστη'}<br>${o.cams_online}/${o.cams_total} κάμερες</span></div>`).join('')}</div>
    <div class="sec"><h3>Σταθμοί drone</h3>
      ${S.ov.docks.map(d => `<div class="org-row"><span class="dot ${d.busy ? 'amber' : ''}"></span><span>${esc(d.name)}<div class="q-s">ακτίνα ${d.radius_km} km · ${d.adapter === 'sim' ? 'προσομοίωση' : 'DJI Dock'}</div></span><span class="mut">${d.busy ? 'σε αποστολή' : 'διαθέσιμο'}</span></div>`).join('')}</div>`;
}

/* ------------------------------------------------------------ incident view */
function renderIncident() {
  const i = S.inc, m = i.mission, flying = m && m.phase !== 'landed', closed = i.status === 'closed', cust = S.me.role === 'customer';
  // camera
  $('#camTitle').textContent = `${i.camera ? i.camera.name : ''} · ${hms(i.first_ts)}`;
  const img = S.imgSel && i.images.includes(S.imgSel) ? S.imgSel : i.images[i.images.length - 1];
  const camHtml = img ? `<img src="/media/${encodeURIComponent(img)}" alt="Εικόνα συναγερμού">` : '<div class="empty">Η κάμερα δεν έστειλε εικόνα</div>';
  if ($('#camView').dataset.img !== (img || '')) { $('#camView').innerHTML = camHtml; $('#camView').dataset.img = img || ''; }
  const th = i.images.length > 1 ? i.images.map(n => `<img src="/media/${encodeURIComponent(n)}" data-n="${esc(n)}" class="${n === img ? 'on' : ''}" alt="">`).join('') : `<span class="mut">${i.count} σήμα${i.count > 1 ? 'τα' : ''} · ${i.camera_list.map(c => esc(c.name)).join(', ')}</span>`;
  if ($('#camThumbs').dataset.h !== th) { $('#camThumbs').innerHTML = th; $('#camThumbs').dataset.h = th; $$('#camThumbs img').forEach(t => t.onclick = () => { S.imgSel = t.dataset.n; renderIncident(); }); }
  // drone
  renderDrone(i, m, flying, cust);
  // side
  $('#side').innerHTML = sideHtml(i, m, flying, closed, cust);
  bindSide(i, m);
  renderMap();
}

function renderDrone(i, m, flying, cust) {
  if (!m) {
    const d = (i.dock_options || []).find(x => x.available && x.in_range);
    $('#droneTitle').textContent = '';
    $('#droneView').innerHTML = `<div class="empty">${d ? `Πλησιέστερο διαθέσιμο: <b>${esc(d.name)}</b><br>${km(d.distance_m)} · άφιξη σε ~${dur(d.eta_s)}` : 'Δεν υπάρχει διαθέσιμο drone εντός εμβέλειας'}</div>`;
    $('#droneBar').innerHTML = cust || i.status === 'closed' ? '' : `<button class="btn pri" data-a="dispatch" ${d ? '' : 'disabled'}>Αποστολή drone</button>`;
    $$('#droneBar [data-a]').forEach(b => b.onclick = () => act(b.dataset.a));
    return;
  }
  $('#droneTitle').innerHTML = `${esc(m.dock.name)} ${m.simulated ? '<span class="tag">ΠΡΟΣΟΜΟΙΩΣΗ</span>' : ''}`;
  const dist = m.distance_m * (1 - m.progress);
  $('#droneView').innerHTML = `<div class="sim-bg ${m.camera_mode === 'ir' ? 'ir' : ''}"></div>
    <div class="hud"><div class="tl">${PHASE[m.phase]}${m.eta ? ` · άφιξη ${dur(m.eta)}` : ''}<br>ΥΨ ${m.alt} m · ${CAM_MODE[m.camera_mode]}${m.light ? ' · ΠΡΟΒΟΛΕΑΣ' : ''}</div>
      <div class="tr">ΜΠΑΤ ${m.battery}%<br>${dur(m.elapsed)}</div>
      <div class="bl">${m.lat.toFixed(5)}, ${m.lon.toFixed(5)}<br>απόσταση ${km(dist)}</div>
      ${m.simulated ? '' : '<div class="br">LIVE</div>'}
      ${flying ? '<div class="cross"></div>' : ''}</div>`;
  $('#droneBar').innerHTML = cust || !flying ? `<span class="mut">${flying ? '' : 'Το drone προσγειώθηκε στο Dock'}</span>` : `
    ${['wide', 'zoom', 'ir'].map(k => `<button class="btn sm ${m.camera_mode === k ? 'on' : ''}" data-cam="${k}">${CAM_MODE[k]}</button>`).join('')}
    ${m.dock.has_light ? `<button class="btn sm ${m.light ? 'on' : ''}" data-a="light">Προβολέας</button>` : ''}
    ${m.phase !== 'returning' ? '<button class="btn sm warn" data-a="return_home">Επιστροφή</button>' : '<span class="mut">Επιστρέφει…</span>'}
    ${m.simulated ? '<span class="mut" style="font-size:11px">Live εικόνα μόλις συνδεθεί DJI Dock</span>' : ''}`;
  $$('#droneBar [data-cam]').forEach(b => b.onclick = () => send({ action: 'camera_mode', mode: b.dataset.cam }));
  $$('#droneBar [data-a]').forEach(b => b.onclick = () => act(b.dataset.a));
}

function sideHtml(i, m, flying, closed, cust) {
  const org = i.org || {}, sopDone = new Set(i.sop_done);
  const head = `<div class="sec ih">
      <div class="t1">${esc(org.name)}</div>
      <div class="t2">${esc(i.type_label)} · ${esc(i.camera ? i.camera.name : '')}${i.count > 1 ? ` · ${i.count} σήματα` : ''}</div>
      <div class="row"><span class="pill ${i.status}">${STATUS[i.status]}</span><span class="pill">${PRIO[i.priority]} προτεραιότητα</span>
        ${i.escalated ? '<span class="pill esc">ΚΛΙΜΑΚΩΣΗ</span>' : ''}
        <span class="mut">${closed ? 'έκλεισε ' + hm(i.closed_ts) : `πριν <span class="timer" data-age="${i.first_ts}">${dur(i.age)}</span>`}</span></div>
      <div class="mut">#${i.id} · ${i.assigned_name ? 'Ελεγκτής: ' + esc(i.assigned_name) : 'Χωρίς ανάληψη'}</div>
    </div>`;
  const info = `<div class="sec"><h3>Στοιχεία</h3><dl class="kv">
      <dt>Διεύθυνση</dt><dd>${esc(org.address || '—')}</dd>
      <dt>Κάμερες</dt><dd>${i.camera_list.map(c => esc(c.name)).join(', ')}</dd>
      ${org.notes ? `<dt>Σημειώσεις</dt><dd>${esc(org.notes)}</dd>` : ''}
      ${closed ? `<dt>Αποτέλεσμα</dt><dd>${esc(i.outcome)}</dd>${i.note ? `<dt>Σημείωση</dt><dd>${esc(i.note)}</dd>` : ''}` : ''}
    </dl></div>`;
  const tl = `<div class="sec"><h3>Χρονολόγιο <a class="r" href="/report/${i.id}" target="_blank">Αναφορά</a></h3>
      <ul class="tl">${i.timeline.slice().reverse().map(t => `<li class="${t.kind}"><time>${hms(t.ts)}</time><span>${esc(t.text)} <small>· ${esc(t.who)}</small></span></li>`).join('')}</ul>
      ${cust ? '' : '<div class="tts" style="margin-top:8px"><input id="noteIn" placeholder="Σημείωση στο χρονολόγιο…" aria-label="Σημείωση"><button class="btn sm" id="noteBtn">Προσθήκη</button></div>'}</div>`;
  if (cust) return head + info + tl;

  const actions = closed ? '' : `<div class="sec"><div class="btns">
      ${i.status === 'new' ? '<button class="btn pri" data-a="ack">Ανάληψη</button>' : ''}
      ${!flying ? '<button class="btn" data-a="false_alarm">Ψευδής</button>' : ''}
      ${!m || m.phase === 'landed' ? '<button class="btn" data-a="dispatch">Αποστολή drone</button>' : ''}
      ${flying && m.phase !== 'returning' ? '<button class="btn warn" data-a="return_home">Επιστροφή drone</button>' : ''}
      <button class="btn" data-a="snooze">Παύση κάμερας</button>
      ${!flying ? '<button class="btn" data-a="close">Κλείσιμο</button>' : ''}
    </div></div>`;
  const sop = (org.sop || []).length ? `<div class="sec sop"><h3>Σχέδιο ενεργειών <span class="r mut">${sopDone.size}/${org.sop.length}</span></h3>
      ${org.sop.map((s, k) => `<label class="${sopDone.has(k) ? 'done' : ''}"><input type="checkbox" data-sop="${k}" ${sopDone.has(k) ? 'checked' : ''} ${closed ? 'disabled' : ''}><span>${esc(s)}</span></label>`).join('')}</div>` : '';
  const speaker = flying && m.dock.has_speaker ? `<div class="sec"><h3>Μεγάφωνο drone</h3>
      <div class="presets">${(org.speaker_presets || []).map((p, k) => `<button class="btn sm" data-preset="${k}">${esc(p)}</button>`).join('')}</div>
      <div class="tts"><input id="ttsIn" placeholder="Γράψτε μήνυμα για εκφώνηση…" aria-label="Μήνυμα μεγαφώνου"><button class="btn sm" id="ttsBtn">Εκφώνηση</button></div>
      <button class="btn wide" id="pttBtn" style="margin-top:6px">Πατήστε και κρατήστε για να μιλήσετε</button></div>` : '';
  const notif = `<div class="sec"><h3>Ειδοποιήσεις</h3><div class="contacts">
      ${i.contacts.map(c => `<div class="contact"><div class="n"><div>${esc(c.name)}</div><div>${esc(c.role)} · ${esc(c.phone)}</div></div>
        ${c.phone ? `<button class="btn sm" data-sms="${c.id}">SMS</button>` : ''}${c.email ? `<button class="btn sm" data-mail="${c.id}">Email</button>` : ''}
        <button class="btn sm" data-call="${c.id}">Κλήση</button></div>`).join('') || '<span class="mut">Δεν υπάρχουν επαφές για την επιχείρηση</span>'}
      </div>
      <div class="btns" style="margin-top:6px"><button class="btn red" data-a="police">Αστυνομία 100 / 112</button>
        ${i.contacts.length ? '<button class="btn" data-a="notify_all">Ειδοποίηση όλων</button>' : ''}</div>
      ${i.notifications.length ? `<div class="mut" style="margin-top:6px">${i.notifications.slice(-4).reverse().map(n => `${hm(n.ts)} ${({ sms: 'SMS', email: 'Email', voice: 'Κλήση', police: 'Αστυνομία' })[n.channel]} → ${esc(n.target)} · ${({ sent: 'στάλθηκε', simulated: 'προσομοίωση', failed: 'απέτυχε', logged: 'καταγράφηκε' })[n.status] || n.status}`).join('<br>')}</div>` : ''}
    </div>`;
  return head + actions + speaker + sop + notif + info + tl;
}

function bindSide(i, m) {
  $$('#side [data-a]').forEach(b => b.onclick = () => act(b.dataset.a));
  $$('#side [data-sop]').forEach(c => c.onchange = () => send({ action: 'sop', step: +c.dataset.sop, done: c.checked }));
  $$('#side [data-preset]').forEach(b => b.onclick = () => send({ action: 'speaker_tts', text: i.org.speaker_presets[+b.dataset.preset] }, 'Εκφωνήθηκε από το μεγάφωνο'));
  $$('#side [data-sms]').forEach(b => b.onclick = () => notifyModal('sms', [+b.dataset.sms]));
  $$('#side [data-mail]').forEach(b => b.onclick = () => notifyModal('email', [+b.dataset.mail]));
  $$('#side [data-call]').forEach(b => b.onclick = () => callModal(i.contacts.find(c => c.id === +b.dataset.call)));
  const tb = $('#ttsBtn'); if (tb) { const go = () => { const v = $('#ttsIn').value.trim(); if (v) send({ action: 'speaker_tts', text: v }, 'Εκφωνήθηκε από το μεγάφωνο'); }; tb.onclick = go; $('#ttsIn').onkeydown = e => { if (e.key === 'Enter') go(); }; }
  const nb = $('#noteBtn'); if (nb) { const go = () => { const v = $('#noteIn').value.trim(); if (v) send({ action: 'note', text: v }); }; nb.onclick = go; $('#noteIn').onkeydown = e => { if (e.key === 'Enter') go(); }; }
  const ptt = $('#pttBtn'); if (ptt) bindPtt(ptt);
}

/* push-to-talk through the drone speaker */
function bindPtt(btn) {
  const start = async e => {
    e.preventDefault(); if (S.rec) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream), chunks = [], t0 = Date.now();
      rec.ondataavailable = ev => chunks.push(ev.data);
      rec.onstop = async () => {
        stream.getTracks().forEach(t => t.stop()); btn.classList.remove('rec'); btn.textContent = 'Πατήστε και κρατήστε για να μιλήσετε';
        const secs = Math.round((Date.now() - t0) / 1000);
        if (secs < 1) { toast('Πολύ σύντομο μήνυμα'); return; }
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        const r = await fetch(`/api/incidents/${S.sel}/speaker-audio?secs=${secs}`, { method: 'POST', body: blob, credentials: 'same-origin' });
        const j = await r.json().catch(() => ({}));
        r.ok ? toast(`Μεταδόθηκε φωνητικό μήνυμα ${secs}″`) : toast(j.error || 'Αποτυχία μετάδοσης', 'err');
        loadIncident();
      };
      S.rec = rec; rec.start(); btn.classList.add('rec'); btn.textContent = 'Μιλάτε… αφήστε για αποστολή';
    } catch (err) { toast('Δεν δόθηκε πρόσβαση στο μικρόφωνο', 'err'); }
  };
  const stop = () => { if (S.rec) { S.rec.stop(); S.rec = null; } };
  btn.onpointerdown = start; btn.onpointerup = stop; btn.onpointerleave = stop;
  btn.onkeydown = e => { if (e.key === ' ' && !S.rec) start(e); }; btn.onkeyup = e => { if (e.key === ' ') stop(); };
}

/* ------------------------------------------------------------ actions */
async function send(body, okMsg) {
  try { const r = await api(`/api/incidents/${S.sel}/action`, body); if (r.incident) { S.inc = r.incident; await loadIncident(); } scheduleRefresh(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, 'err'); throw e; }
}
function act(a) {
  const i = S.inc;
  if (a === 'ack') return send({ action: 'ack' });
  if (a === 'return_home') return send({ action: 'return_home' }, 'Το drone επιστρέφει στο Dock');
  if (a === 'light') return send({ action: 'light', on: !i.mission.light });
  if (a === 'dispatch') return dispatchModal();
  if (a === 'police') return policeModal();
  if (a === 'notify_all') return notifyModal('sms', i.contacts.map(c => c.id));
  if (a === 'snooze') return modal(`<h2>Παύση κάμερας</h2><p class="mut">Η κάμερα δεν θα δημιουργεί συναγερμούς για το διάστημα που επιλέγετε.</p>
      <label for="snCam">Κάμερα</label><select id="snCam">${i.camera_list.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select>
      <label for="snMin">Διάρκεια</label><select id="snMin"><option value="30">30 λεπτά</option><option value="60" selected>1 ώρα</option><option value="240">4 ώρες</option><option value="720">12 ώρες</option></select>`,
    'Παύση', () => send({ action: 'snooze_camera', camera_id: +$('#snCam').value, minutes: +$('#snMin').value }, 'Η κάμερα τέθηκε σε παύση'));
  if (a === 'false_alarm') return modal(`<h2>Ψευδής συναγερμός;</h2><p>${esc(i.org.name)} · ${esc(i.camera ? i.camera.name : '')}</p>
      <label for="faN">Αιτία (προαιρετικά)</label><input id="faN" placeholder="π.χ. ζώο, σκιά, βροχή, εργαζόμενος">`,
    'Κλείσιμο ως ψευδής', () => send({ action: 'false_alarm', note: $('#faN').value }, 'Το περιστατικό έκλεισε'));
  if (a === 'close') return modal(`<h2>Κλείσιμο περιστατικού #${i.id}</h2>
      <label>Αποτέλεσμα</label>${OUTCOMES.map((o, k) => `<label class="check"><input type="radio" name="oc" value="${esc(o)}" ${k === 0 ? 'checked' : ''}> ${esc(o)}</label>`).join('')}
      <label for="clN">Σημείωση</label><textarea id="clN" placeholder="Τι βρέθηκε, ποιος ενημερώθηκε"></textarea>`,
    'Κλείσιμο', () => send({ action: 'close', outcome: $('input[name=oc]:checked').value, note: $('#clN').value }, 'Το περιστατικό έκλεισε'));
}
function dispatchModal() {
  const i = S.inc, opts = i.dock_options || [];
  const best = opts.find(d => d.available && d.in_range);
  modal(`<h2>Αποστολή drone</h2><p class="mut">${esc(i.org.name)} · ${esc(i.camera ? i.camera.name : '')}</p>
    ${opts.map(d => { const ok = d.available && d.in_range; return `<label class="dockopt ${ok ? '' : 'dis'}"><input type="radio" name="dk" value="${d.id}" ${d === best ? 'checked' : ''} ${ok ? '' : 'disabled'}>
      <span><b>${esc(d.name)}</b><div class="mut">${km(d.distance_m)} · άφιξη ~${dur(d.eta_s)} · ${d.adapter === 'sim' ? 'προσομοίωση' : 'DJI Dock'}</div></span>
      <span class="mut">${!d.available ? 'σε αποστολή' : !d.in_range ? 'εκτός εμβέλειας' : 'διαθέσιμο'}</span></label>`; }).join('') || '<p>Δεν υπάρχουν σταθμοί drone.</p>'}
    <p class="mut">Ο ελεγκτής είναι υπεύθυνος για την πτήση. Βεβαιωθείτε ότι επιτρέπεται από την άδεια πτήσεων.</p>`,
    'Επιβεβαίωση αποστολής', () => { const v = $('input[name=dk]:checked'); if (!v) throw new Error('Επιλέξτε σταθμό'); return send({ action: 'dispatch', dock_id: +v.value }, 'Το drone απογειώνεται'); }, !best);
}
function notifyModal(channel, ids) {
  const i = S.inc;
  const text = `Συναγερμός στην επιχείρηση ${i.org.name}: ${i.type_label} στην κάμερα «${i.camera ? i.camera.name : ''}» στις ${hm(i.first_ts)}. Το κέντρο ελέγχου το χειρίζεται. Αρ. #${i.id}`;
  modal(`<h2>Ειδοποίηση</h2>
    <label for="nCh">Κανάλι</label><select id="nCh"><option value="sms" ${channel === 'sms' ? 'selected' : ''}>SMS</option><option value="email" ${channel === 'email' ? 'selected' : ''}>Email</option></select>
    <label>Παραλήπτες</label>${i.contacts.map(c => `<label class="check"><input type="checkbox" value="${c.id}" ${ids.includes(c.id) ? 'checked' : ''}> ${esc(c.name)} · ${esc(c.phone || '')} ${esc(c.email || '')}</label>`).join('')}
    <label for="nMsg">Μήνυμα</label><textarea id="nMsg">${esc(text)}</textarea>`,
    'Αποστολή', async () => {
      const sel = $$('.modal .check input:checked').map(x => +x.value);
      const r = await send({ action: 'notify', channel: $('#nCh').value, contact_ids: sel, message: $('#nMsg').value });
      const st = (r.results || []).map(x => `${x.contact}: ${({ sent: 'στάλθηκε', simulated: 'προσομοίωση', failed: 'απέτυχε', skipped: 'χωρίς στοιχείο' })[x.status] || x.status}`).join(' · ');
      toast(st || 'Ολοκληρώθηκε');
    });
}
function callModal(c) {
  modal(`<h2>Κλήση: ${esc(c.name)}</h2><p>Τηλέφωνο: <b style="font-size:16px;user-select:all">${esc(c.phone)}</b> <button class="btn sm" id="cpPh">Αντιγραφή</button></p>
    <label for="cRes">Αποτέλεσμα κλήσης</label><select id="cRes"><option>Απάντησε · ενημερώθηκε</option><option>Απάντησε · ζήτησε αποστολή drone</option><option>Απάντησε · ψευδής (ήταν ο ίδιος / εργαζόμενος)</option><option>Δεν απάντησε</option><option>Λάθος αριθμός</option></select>
    <label for="cN">Σημείωση</label><input id="cN">`,
    'Καταγραφή κλήσης', () => send({ action: 'call_log', target: `${c.name} ${c.phone}`, result: $('#cRes').value + ($('#cN').value ? ' · ' + $('#cN').value : '') }, 'Η κλήση καταγράφηκε'));
  $('#cpPh').onclick = () => copy(c.phone);
}
function policeModal() {
  const i = S.inc;
  modal(`<h2>Ειδοποίηση αστυνομίας</h2>
    <p>Καλέστε <b style="font-size:16px">100</b> (Άμεση Δράση) ή <b style="font-size:16px">112</b> και διαβάστε την περίληψη:</p>
    <pre class="sum" id="psum">${esc(i.police_summary)}</pre><button class="btn sm" id="cpSum" style="margin-top:6px">Αντιγραφή περίληψης</button>
    <label for="pNum">Αριθμός</label><select id="pNum"><option>100</option><option>112</option><option>Τοπικό Α.Τ.</option></select>
    <label for="pRes">Αποτέλεσμα</label><input id="pRes" placeholder="π.χ. στάλθηκε περιπολικό, αρ. συμβάντος…">`,
    'Καταγραφή ειδοποίησης', () => send({ action: 'police', number: $('#pNum').value, result: $('#pRes').value, summary: i.police_summary }, 'Η ειδοποίηση καταγράφηκε'));
  $('#cpSum').onclick = () => copy(i.police_summary);
}
function copy(text) {
  navigator.clipboard.writeText(text).then(() => toast('Αντιγράφηκε'), () => toast('Επιλέξτε το κείμενο και αντιγράψτε το χειροκίνητα', 'err'));
}

/* ------------------------------------------------------------ modal */
function modal(html, okLabel, onOk, okDisabled) {
  $('#modal').innerHTML = `<div class="modal-bg"><div class="modal" role="dialog" aria-modal="true">${html}<div class="err" id="mErr"></div>
    <div class="mb"><button class="btn" id="mCancel">Ακύρωση</button><button class="btn pri" id="mOk" ${okDisabled ? 'disabled' : ''}>${okLabel}</button></div></div></div>`;
  const close = () => { $('#modal').innerHTML = ''; };
  $('#mCancel').onclick = close;
  $('.modal-bg').onmousedown = e => { if (e.target.classList.contains('modal-bg')) close(); };
  $('#mOk').onclick = async () => {
    $('#mOk').disabled = true;
    try { await onOk(); close(); } catch (e) { $('#mErr').textContent = e.message; $('#mOk').disabled = false; }
  };
  const f = $('.modal input:not([type=radio]):not([type=checkbox]), .modal textarea, .modal select'); (f || $('#mOk')).focus();
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && $('#modal').innerHTML) $('#modal').innerHTML = ''; });
window.modal = modal; window.toast = toast; window.api = api; window.esc = esc;

/* ------------------------------------------------------------ map */
function initMap() {
  if (S.map || typeof L === 'undefined') { if (typeof L === 'undefined') $('#map').innerHTML = '<div class="empty" style="padding-top:60px">Ο χάρτης χρειάζεται σύνδεση στο internet</div>'; return; }
  S.map = L.map('map', { zoomControl: true, attributionControl: true }).setView([37.98, 23.73], 10);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(S.map);
  S.layers.base = L.layerGroup().addTo(S.map); S.layers.live = L.layerGroup().addTo(S.map);
  $('#legend').innerHTML = '<span><i class="mk alarm" style="box-shadow:none"></i>Συναγερμός</span><span><i class="mk ok"></i>Οπλισμένη</span><span><i class="mk off"></i>Αφόπλιστη</span><span><i class="mk-dock"></i>Dock</span><span><i class="mk-drone" style="width:12px;height:12px;border-width:3px"></i>Drone</span>';
}
const icon = (cls, size = 14) => L.divIcon({ className: '', html: `<div class="${cls}"></div>`, iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
function renderMap() {
  if (!S.map || !S.ov) return;
  S.layers.base.clearLayers(); S.layers.live.clearLayers();
  const alarmOrgs = new Set(S.ov.incidents.filter(i => i.status !== 'closed' && i.priority < 4).map(i => i.org_id));
  const pts = [];
  S.ov.orgs.filter(o => o.kind === 'customer' && o.lat != null).forEach(o => {
    const cls = alarmOrgs.has(o.id) ? 'mk alarm' : o.armed ? 'mk ok' : 'mk off';
    L.marker([o.lat, o.lon], { icon: icon(cls), zIndexOffset: alarmOrgs.has(o.id) ? 500 : 0 }).bindPopup(`<b>${esc(o.name)}</b><br>${esc(o.address)}<br>${o.armed ? 'Οπλισμένη' : 'Αφόπλιστη'} · ${o.cams_online}/${o.cams_total} κάμερες`).addTo(S.layers.base);
    pts.push([o.lat, o.lon]);
  });
  S.ov.docks.forEach(d => {
    L.circle([d.lat, d.lon], { radius: d.radius_km * 1000, color: '#60a5fa', weight: 1, opacity: .35, fillOpacity: .04 }).addTo(S.layers.base);
    L.marker([d.lat, d.lon], { icon: icon('mk-dock' + (d.busy ? ' busy' : ''), 16) }).bindPopup(`<b>${esc(d.name)}</b><br>${d.busy ? 'σε αποστολή' : 'διαθέσιμο'}`).addTo(S.layers.base);
  });
  const ms = S.ov.missions.slice();
  if (S.inc && S.inc.mission && S.inc.mission.phase !== 'landed') { const k = ms.findIndex(x => x.id === S.inc.mission.id); if (k >= 0) ms[k] = S.inc.mission; else ms.push(S.inc.mission); }
  ms.forEach(m => {
    L.polyline([[m.dock.lat, m.dock.lon], [m.tlat, m.tlon]], { color: '#60a5fa', weight: 2, dashArray: '6 6', opacity: .8 }).addTo(S.layers.live);
    L.marker([m.lat, m.lon], { icon: icon('mk-drone', 18), zIndexOffset: 1000 }).addTo(S.layers.live);
  });
  if (S.inc && !S.fitted) {
    const i = S.inc, c = i.camera && i.camera.lat != null ? [i.camera.lat, i.camera.lon] : [i.org.lat, i.org.lon];
    if (c[0] != null) {
      const d = (i.dock_options || [])[0], b = [c]; if (i.mission) b.push([i.mission.dock.lat, i.mission.dock.lon]); else if (d) b.push([d.lat, d.lon]);
      S.map.fitBounds(L.latLngBounds(b).pad(.6), { maxZoom: 16 }); S.fitted = true;
    }
  } else if (!S.inc && pts.length && !S.fittedAll) { S.map.fitBounds(L.latLngBounds(pts).pad(.3)); S.fittedAll = true; }
  if (S.inc) S.inc.camera_list.filter(x => x.lat != null).forEach(x => L.circleMarker([x.lat, x.lon], { radius: 5, color: '#ef4444', weight: 2, fillOpacity: .6 }).bindTooltip(esc(x.name)).addTo(S.layers.live));
}

boot();
