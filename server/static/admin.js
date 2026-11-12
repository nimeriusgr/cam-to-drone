/* Cam to Drone · administration pages */
(function () {
  const TYPES = { human: 'Άτομο', vehicle: 'Όχημα', input: 'Είσοδος συναγερμού', zone: 'Είσοδος σε ζώνη', line: 'Διέλευση γραμμής', tamper: 'Παραποίηση κάμερας', face: 'Πρόσωπο', motion: 'Κίνηση', other: 'Άλλο' };
  const DAYS = ['', 'Δε', 'Τρ', 'Τε', 'Πε', 'Πα', 'Σα', 'Κυ'];
  const fmtTs = t => t ? new Date(t * 1000).toLocaleString('el-GR', { hour12: false }) : '—';
  const ago = t => { if (!t) return 'ποτέ'; const s = Date.now() / 1000 - t; return s < 90 ? 'τώρα' : s < 3600 ? Math.round(s / 60) + ' λ.' : s < 86400 ? Math.round(s / 3600) + ' ω.' : fmtTs(t); };
  let cache = {}, page = 'orgs';

  // schedule <-> text: one rule per line: "1-7 21:00-07:00" or "1,2,3 22:00-06:00"
  const schedToText = s => (s || []).map(r => `${daysToText(r.days)} ${r.from}-${r.to}`).join('\n');
  const daysToText = d => { d = d || [1, 2, 3, 4, 5, 6, 7]; const c = d.join(','); return c === '1,2,3,4,5,6,7' ? '1-7' : c === '1,2,3,4,5' ? '1-5' : c; };
  const textToSched = t => t.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
    const m = l.match(/^([\d,\-]+)\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$/);
    if (!m) throw new Error(`Λάθος γραμμή ωραρίου: «${l}». Μορφή: 1-7 21:00-07:00`);
    const days = []; m[1].split(',').forEach(p => { const [a, b] = p.split('-').map(Number); for (let k = a; k <= (b || a); k++) if (k >= 1 && k <= 7) days.push(k); });
    const pad = x => x.padStart(5, '0'); return { days, from: pad(m[2]), to: pad(m[3]) };
  });

  const PAGES = {
    orgs: {
      title: 'Οργανισμοί', table: 'orgs', add: 'Νέα επιχείρηση',
      cols: [['Όνομα', r => `<b>${esc(r.name)}</b><div class="mut">${r.kind === 'center' ? 'Κέντρο λήψης' : 'Επιχείρηση'}</div>`],
        ['Διεύθυνση', r => esc(r.address)],
        ['Οπλισμός', r => ({ armed: 'Πάντα οπλισμένη', disarmed: 'Αφόπλιστη', schedule: 'Με ωράριο' })[r.arming_mode] + (r.arming_mode === 'schedule' ? `<div class="mut">${esc(schedToText(r.schedule)).replace(/\n/g, '<br>') || 'χωρίς ωράριο = πάντα'}</div>` : '')],
        ['Webhook για κάμερες', r => r.webhook_url ? `<code>${esc(location.origin + r.webhook_url)}</code>` : '—']],
      fields: () => [
        ['name', 'Όνομα', 'text', { required: true }], ['kind', 'Τύπος', 'select', { options: { customer: 'Επιχείρηση (πελάτης)', center: 'Κέντρο λήψης' } }],
        ['parent_id', 'Ανήκει σε', 'select', { options: orgOptions(true) }], ['address', 'Διεύθυνση', 'text'],
        ['lat', 'Γεωγρ. πλάτος', 'number'], ['lon', 'Γεωγρ. μήκος', 'number'],
        ['arming_mode', 'Οπλισμός', 'select', { options: { armed: 'Πάντα οπλισμένη', schedule: 'Με ωράριο', disarmed: 'Αφόπλιστη' } }],
        ['schedule', 'Ωράριο οπλισμού (μία γραμμή ανά κανόνα: ημέρες 1=Δευτέρα…7=Κυριακή, π.χ. «1-7 21:00-07:00»)', 'schedule'],
        ['allowed_types', 'Τύποι που δημιουργούν συναγερμό (κανένα επιλεγμένο = όλοι)', 'types'],
        ['ack_timeout', 'Κλιμάκωση αν δεν γίνει ανάληψη σε (δευτ.)', 'number'],
        ['sop', 'Πρωτόκολλο ενεργειών (ένα βήμα ανά γραμμή)', 'lines'], ['speaker_presets', 'Έτοιμα μηνύματα μεγαφώνου (ένα ανά γραμμή)', 'lines'],
        ['notes', 'Σημειώσεις για τον ελεγκτή', 'textarea']],
    },
    contacts: {
      title: 'Επαφές', table: 'contacts', add: 'Νέα επαφή',
      cols: [['Επιχείρηση', r => esc(orgName(r.org_id))], ['Όνομα', r => `<b>${esc(r.name)}</b><div class="mut">${esc(r.role)}</div>`], ['Τηλέφωνο', r => esc(r.phone)], ['Email', r => esc(r.email)], ['Σειρά', r => r.priority]],
      fields: () => [['org_id', 'Επιχείρηση', 'select', { options: orgOptions(false), required: true }], ['name', 'Όνομα', 'text', { required: true }], ['role', 'Ρόλος', 'text'],
        ['phone', 'Τηλέφωνο', 'text'], ['email', 'Email', 'text'], ['priority', 'Σειρά κλήσης', 'number'], ['notify_sms', 'Λαμβάνει SMS', 'check'], ['notify_email', 'Λαμβάνει email', 'check']],
    },
    cameras: {
      title: 'Κάμερες', table: 'cameras', add: 'Νέα κάμερα',
      cols: [['Επιχείρηση', r => esc(orgName(r.org_id))], ['Κάμερα', r => `<b>${esc(r.name)}</b><div class="mut"><code>${esc(r.ext_key)}</code></div>`],
        ['Κατάσταση', r => `<span class="dot ${!r.enabled ? 'grey' : r.online ? '' : 'red'}" style="display:inline-block"></span> ${!r.enabled ? 'απενεργοποιημένη' : r.online ? 'online' : 'εκτός σύνδεσης'}${r.snooze_until > Date.now() / 1000 ? `<div class="mut">σε παύση έως ${fmtTs(r.snooze_until)}</div>` : ''}`],
        ['Οπλισμός', r => ({ armed: '<span class="pill ack">Οπλισμένη</span>', disarmed: '<span class="pill">Αφοπλισμένη</span>' })[r.arming_mode] || '<span class="mut">όπως η επιχείρηση</span>'],
        ['Τελευταίο σήμα', r => ago(r.last_seen)], ['Θέση', r => r.lat != null ? `${(+r.lat).toFixed(5)}, ${(+r.lon).toFixed(5)}` : '—'],
        ['', r => `<button class="btn sm" data-camctl="${r.id}">Έλεγχος ⚙</button>`]],
      fields: () => [['org_id', 'Επιχείρηση', 'select', { options: orgOptions(false), required: true }], ['edge_id', 'Κουτί', 'select', { options: edgeOptions() }],
        ['name', 'Όνομα', 'text', { required: true }], ['ext_key', 'Κλειδί κάμερας (όπως το στέλνει το κουτί, π.χ. 192.168.1.64:1)', 'text', { required: true }],
        ['lat', 'Γεωγρ. πλάτος', 'number'], ['lon', 'Γεωγρ. μήκος', 'number'], ['enabled', 'Ενεργή', 'check'], ['arming_mode', 'Οπλισμός κάμερας', 'select', { options: { inherit: 'Όπως η επιχείρηση', armed: 'Πάντα οπλισμένη', disarmed: 'Αφοπλισμένη' } }],
        ['snooze_until', 'Παύση έως (κενό = χωρίς παύση)', 'clearsnooze']],
    },
    edges: {
      title: 'Κουτιά (Raspberry / Linux)', table: 'edges', add: 'Νέο κουτί',
      cols: [['Επιχείρηση', r => esc(orgName(r.org_id))], ['Όνομα', r => `<b>${esc(r.name)}</b>`], ['Κατάσταση', r => `<span class="dot ${r.last_seen && Date.now() / 1000 - r.last_seen < 120 ? '' : 'red'}" style="display:inline-block"></span> ${ago(r.last_seen)}`],
        ['Έκδοση / IP', r => `${esc(r.version || '—')}${r.version && r.agent_latest && r.version !== r.agent_latest ? ` <span class="pill ack">νέα: ${esc(r.agent_latest)}</span>` : ''}${r.update_requested ? ' <span class="pill dispatched">ενημέρωση σε αναμονή</span>' : ''}<div class="mut">${esc(r.ip || '')}</div>`],
        ['Κάμερες', r => r.cameras], ['Token', r => `<code>${esc(r.token)}</code> <button class="btn sm" data-regen="${r.id}">Νέο token</button>`],
        ['', r => S.me.role === 'admin' ? `<button class="btn sm" data-upd="${r.id}" title="Το κουτί κατεβάζει την έκδοση ${esc(r.agent_latest || '')} στην επόμενη επικοινωνία">Ενημέρωση</button>` : '']],
      fields: () => [['org_id', 'Επιχείρηση', 'select', { options: orgOptions(false), required: true }], ['name', 'Όνομα', 'text', { required: true }]],
      created: r => showSecret('Token κουτιού', r.token, 'Βάλτε το στο config.json του κουτιού (πεδίο "token"). Δεν θα ξαναεμφανιστεί.'),
    },
    docks: {
      title: 'Σταθμοί drone', table: 'docks', add: 'Νέος σταθμός',
      cols: [['Όνομα', r => `<b>${esc(r.name)}</b><div class="mut">${esc(r.serial || '')}</div>`], ['Θέση', r => `${(+r.lat).toFixed(5)}, ${(+r.lon).toFixed(5)}`], ['Ακτίνα', r => r.radius_km + ' km'],
        ['Σύνδεση', r => r.adapter === 'sim' ? 'Προσομοίωση' : 'DJI Cloud API'], ['Εξοπλισμός', r => [r.has_speaker ? 'Μεγάφωνο' : '', r.has_light ? 'Προβολέας' : ''].filter(Boolean).join(', ') || '—'], ['Ενεργός', r => r.enabled ? 'Ναι' : 'Όχι']],
      fields: () => [['name', 'Όνομα', 'text', { required: true }], ['lat', 'Γεωγρ. πλάτος', 'number', { required: true }], ['lon', 'Γεωγρ. μήκος', 'number', { required: true }],
        ['radius_km', 'Ακτίνα κάλυψης (km)', 'number'], ['adapter', 'Σύνδεση', 'select', { options: { sim: 'Προσομοίωση', dji: 'DJI Cloud API (πραγματικό Dock)' } }], ['serial', 'Σειριακός αριθμός Dock', 'text'],
        ['has_speaker', 'Έχει μεγάφωνο (AS1)', 'check'], ['has_light', 'Έχει προβολέα (AL1)', 'check'], ['enabled', 'Ενεργός', 'check']],
    },
    nofly: {
      title: 'Ζώνες απαγόρευσης πτήσης', table: 'nofly', add: 'Νέα ζώνη',
      intro: 'Πριν από κάθε αποστολή drone ελέγχεται αν ο στόχος ή η διαδρομή από τον σταθμό περνά από ενεργή ζώνη. «Απαγόρευση»: η αποστολή μπλοκάρεται και μόνο επόπτης μπορεί να την παρακάμψει με αιτιολογία. «Προσοχή»: εμφανίζεται προειδοποίηση. Οι επίσημες ζώνες UAS δημοσιεύονται από την ΥΠΑ.',
      cols: [['Ζώνη', r => `<b>${esc(r.name)}</b><div class="mut">${esc(r.notes || '')}</div>`], ['Κέντρο', r => `${(+r.lat).toFixed(5)}, ${(+r.lon).toFixed(5)}`], ['Ακτίνα', r => (r.radius_m >= 1000 ? (r.radius_m / 1000).toFixed(1) + ' km' : r.radius_m + ' m')],
        ['Επίπεδο', r => r.level === 'block' ? '<span class="pill new">Απαγόρευση</span>' : '<span class="pill ack">Προσοχή</span>'], ['Ενεργή', r => r.enabled ? 'Ναι' : 'Όχι']],
      fields: () => [['name', 'Όνομα', 'text', { required: true }], ['lat', 'Γεωγρ. πλάτος κέντρου', 'number', { required: true }], ['lon', 'Γεωγρ. μήκος κέντρου', 'number', { required: true }],
        ['radius_m', 'Ακτίνα (μέτρα)', 'number', { required: true }], ['level', 'Επίπεδο', 'select', { options: { block: 'Απαγόρευση (μπλοκάρει την αποστολή)', warn: 'Προσοχή (μόνο προειδοποίηση)' } }],
        ['notes', 'Σημειώσεις', 'textarea'], ['enabled', 'Ενεργή', 'check']],
    },
    users: {
      title: 'Χρήστες', table: 'users', add: 'Νέος χρήστης',
      cols: [['Χρήστης', r => `<b>${esc(r.name)}</b><div class="mut">${esc(r.username)}</div>`], ['Ρόλος', r => ({ admin: 'Διαχειριστής', supervisor: 'Επόπτης', operator: 'Ελεγκτής', customer: 'Πελάτης' })[r.role]],
        ['Επιχείρηση', r => esc(orgName(r.org_id) || '—')], ['API token', r => `<code>${esc(r.api_token)}</code>`], ['Ενεργός', r => r.active ? 'Ναι' : 'Όχι']],
      fields: (r) => [['username', 'Όνομα χρήστη', 'text', { required: true }], ['name', 'Ονοματεπώνυμο', 'text', { required: true }],
        ['role', 'Ρόλος', 'select', { options: { operator: 'Ελεγκτής', supervisor: 'Επόπτης', admin: 'Διαχειριστής', customer: 'Πελάτης (μόνο προβολή της επιχείρησής του)' } }],
        ['org_id', 'Επιχείρηση (για πελάτες)', 'select', { options: orgOptions(true) }], ['active', 'Ενεργός', 'check'],
        ['password', r ? 'Νέος κωδικός (κενό = χωρίς αλλαγή)' : 'Κωδικός (τουλάχιστον 8 χαρακτήρες)', 'password']],
      created: r => showSecret('API token χρήστη', r.api_token, 'Για Postman: header Authorization: Bearer <token>. Δεν θα ξαναεμφανιστεί.'),
    },
  };
  const NAV = [['orgs', 'Οργανισμοί'], ['contacts', 'Επαφές'], ['cameras', 'Κάμερες'], ['edges', 'Κουτιά'], ['docks', 'Σταθμοί drone'], ['nofly', 'Ζώνες πτήσης'], ['users', 'Χρήστες'],
    ['settings', 'Ειδοποιήσεις (SMS / Email)'], ['events', 'Καταγραφή σημάτων'], ['audit', 'Ιστορικό ενεργειών']];

  const orgName = id => (cache.orgs || []).find(o => o.id === id)?.name || '';
  const orgOptions = withEmpty => Object.assign(withEmpty ? { '': '—' } : {}, ...(cache.orgs || []).map(o => ({ [o.id]: o.name })));
  const edgeOptions = () => Object.assign({ '': '— (webhook)' }, ...(cache.edges || []).map(e => ({ [e.id]: `${e.name}` })));

  async function load(t) { cache[t] = await api('/api/admin/' + t); return cache[t]; }

  async function open() {
    const el = $('#admin');
    if (!el.dataset.ready) {
      el.innerHTML = `<aside class="pane"><nav id="anav">${NAV.map(([k, v]) => `<button data-p="${k}">${v}</button>`).join('')}</nav></aside><section class="pane"><div class="body" id="abody"></div></section>`;
      $$('#anav button').forEach(b => b.onclick = () => { page = b.dataset.p; render(); });
      el.dataset.ready = 1;
    }
    render();
  }

  async function render() {
    $$('#anav button').forEach(b => b.classList.toggle('on', b.dataset.p === page));
    const body = $('#abody'); body.innerHTML = '<div class="empty">Φόρτωση…</div>';
    const ro = S.me.role !== 'admin';
    try {
      await Promise.all(['orgs', 'edges'].map(load));
      if (page === 'settings') return renderSettings(body, ro);
      if (page === 'events') return renderLog(body, 'events');
      if (page === 'audit') return renderLog(body, 'audit');
      const p = PAGES[page], rows = page === 'orgs' || page === 'edges' ? cache[page] : await load(p.table);
      body.innerHTML = `<div class="bar"><h2>${p.title}</h2><span class="mut">${rows.length}</span><span class="sp"></span>${ro ? '<span class="mut">μόνο προβολή</span>' : `<button class="btn pri" id="aAdd">${p.add}</button>`}</div>
        ${p.intro ? `<p class="mut" style="max-width:90ch;margin-top:-4px">${p.intro}</p>` : ''}
        <table><thead><tr>${p.cols.map(c => `<th>${c[0]}</th>`).join('')}${ro ? '' : '<th></th>'}</tr></thead>
        <tbody>${rows.map(r => `<tr>${p.cols.map(c => `<td>${c[1](r)}</td>`).join('')}${ro ? '' : `<td style="white-space:nowrap"><button class="btn sm" data-edit="${r.id}">Επεξεργασία</button> <button class="btn sm" data-del="${r.id}">Διαγραφή</button></td>`}</tr>`).join('')
        || `<tr><td colspan="9" class="mut">Δεν υπάρχουν εγγραφές.</td></tr>`}</tbody></table>`;
      $$('[data-camctl]', body).forEach(b => b.onclick = () => Features.openCamera(+b.dataset.camctl));
      if (ro) return;
      $('#aAdd').onclick = () => form(p, null);
      $$('[data-edit]', body).forEach(b => b.onclick = () => form(p, rows.find(r => r.id === +b.dataset.edit)));
      $$('[data-del]', body).forEach(b => b.onclick = () => modal(`<h2>Διαγραφή;</h2><p>Η εγγραφή θα διαγραφεί οριστικά.</p>`, 'Διαγραφή', async () => { await api(`/api/admin/${p.table}/${b.dataset.del}`, undefined, 'DELETE'); toast('Διαγράφηκε'); render(); }));
      $$('[data-upd]', body).forEach(b => b.onclick = () => modal(`<h2>Ενημέρωση κουτιού;</h2><p>Το κουτί θα κατεβάσει τη νέα έκδοση του προγράμματος από τον server στην επόμενη επικοινωνία (έως 30″), θα ελέγξει ότι ξεκινά σωστά και θα επανεκκινηθεί. Οι ρυθμίσεις του (config.json) δεν αλλάζουν.</p>`, 'Ενημέρωση', async () => {
        await api(`/api/admin/edges/${b.dataset.upd}`, { request_update: true }, 'PUT'); toast('Η ενημέρωση ζητήθηκε'); render();
      }));
      $$('[data-regen]', body).forEach(b => b.onclick = () => modal(`<h2>Νέο token κουτιού;</h2><p>Το παλιό token σταματά να δουλεύει αμέσως. Θα χρειαστεί να βάλετε το νέο στο κουτί.</p>`, 'Δημιουργία', async () => {
        const r = await api(`/api/admin/edges/${b.dataset.regen}`, { regenerate_token: true }, 'PUT'); showSecret('Νέο token κουτιού', r.token, 'Βάλτε το στο config.json του κουτιού.'); render();
      }));
    } catch (e) { body.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }

  function field([k, label, type, o = {}], r) {
    const v = r ? r[k] : undefined, id = 'f_' + k;
    if (type === 'select') return `<label for="${id}">${label}</label><select id="${id}" data-k="${k}">${Object.entries(o.options).map(([ov, ol]) => `<option value="${esc(ov)}" ${String(v ?? '') === String(ov) ? 'selected' : ''}>${esc(ol)}</option>`).join('')}</select>`;
    if (type === 'check') return `<label class="check"><input type="checkbox" id="${id}" data-k="${k}" ${r ? (v ? 'checked' : '') : 'checked'}> ${label}</label>`;
    if (type === 'lines') return `<label for="${id}">${label}</label><textarea id="${id}" data-k="${k}" data-t="lines">${esc((v || []).join('\n'))}</textarea>`;
    if (type === 'schedule') return `<label for="${id}">${label}</label><textarea id="${id}" data-k="${k}" data-t="schedule" style="min-height:60px">${esc(schedToText(v))}</textarea>`;
    if (type === 'textarea') return `<label for="${id}">${label}</label><textarea id="${id}" data-k="${k}">${esc(v || '')}</textarea>`;
    if (type === 'types') return `<label>${label}</label><div style="display:grid;grid-template-columns:1fr 1fr">${Object.entries(TYPES).map(([tk, tl]) => `<label class="check"><input type="checkbox" data-type="${tk}" ${(v || []).includes(tk) ? 'checked' : ''}> ${tl}</label>`).join('')}</div>`;
    if (type === 'clearsnooze') return v && v > Date.now() / 1000 ? `<label class="check"><input type="checkbox" id="${id}" data-k="${k}" data-t="clear"> Λήξη παύσης τώρα (σε παύση έως ${fmtTs(v)})</label>` : '';
    return `<label for="${id}">${label}</label><input id="${id}" data-k="${k}" type="${type === 'number' ? 'number' : type === 'password' ? 'password' : 'text'}" ${type === 'number' ? 'step="any"' : ''} value="${esc(v ?? '')}" ${o.required && type !== 'password' ? 'required' : ''} ${type === 'password' ? 'autocomplete="new-password"' : ''}>`;
  }

  function form(p, r) {
    const fields = p.fields(r);
    modal(`<h2>${r ? 'Επεξεργασία' : p.add}</h2>${fields.map(f => field(f, r)).join('')}`, r ? 'Αποθήκευση' : 'Δημιουργία', async () => {
      const data = {};
      $$('.modal [data-k]').forEach(el => {
        const k = el.dataset.k;
        if (el.type === 'checkbox' && el.dataset.t === 'clear') { if (el.checked) data[k] = null; return; }
        if (el.type === 'checkbox') data[k] = el.checked ? 1 : 0;
        else if (el.dataset.t === 'lines') data[k] = el.value.split('\n').map(x => x.trim()).filter(Boolean);
        else if (el.dataset.t === 'schedule') data[k] = textToSched(el.value);
        else if (el.type === 'number') data[k] = el.value === '' ? '' : Number(el.value);
        else if (el.tagName === 'SELECT' && /_id$/.test(k)) data[k] = el.value === '' ? '' : Number(el.value);
        else data[k] = el.value;
      });
      if ($('.modal [data-type]')) data.allowed_types = $$('.modal [data-type]:checked').map(x => x.dataset.type);
      fields.filter(f => f[3] && f[3].required).forEach(f => { if (data[f[0]] === '' || data[f[0]] == null) throw new Error(`Συμπληρώστε: ${f[1]}`); });
      if (data.password === '') delete data.password;
      const res = r ? await api(`/api/admin/${p.table}/${r.id}`, data, 'PUT') : await api(`/api/admin/${p.table}`, data);
      toast(r ? 'Αποθηκεύτηκε' : 'Δημιουργήθηκε');
      await render();
      if (!r && p.created) p.created(res);
    });
  }

  function showSecret(title, value, hint) {
    modal(`<h2>${title}</h2><p class="mut">${hint}</p><pre class="sum" style="user-select:all;font-family:var(--mono)">${esc(value)}</pre>`, 'Αντιγραφή και κλείσιμο', async () => {
      try { await navigator.clipboard.writeText(value); toast('Αντιγράφηκε'); } catch { toast('Επιλέξτε το κείμενο και αντιγράψτε το χειροκίνητα', 'err'); throw new Error('Αντιγράψτε το χειροκίνητα και πατήστε Ακύρωση'); }
    });
  }

  async function renderSettings(body, ro) {
    const s = await api('/api/admin/settings'), sms = s.sms || {}, smtp = s.smtp || {};
    if (s.platform === 'cloudflare') { renderSettingsHttp(body, ro, sms, s.email || {}); flightBox(body, ro, s.flight || {}); return mediaBox(body, ro, s.media || {}); }
    body.innerHTML = `<div class="bar"><h2>Ειδοποιήσεις</h2></div>
      <p class="mut" style="max-width:75ch">Χωρίς πάροχο, τα SMS και τα email καταγράφονται ως «προσομοίωση» και δεν στέλνονται. Στην Ελλάδα το όνομα αποστολέα SMS χρειάζεται προεγγραφή στον πάροχο.</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px" class="modal-like">
        <div class="pane" style="padding:14px"><h3 style="margin:0 0 6px">SMS (HTTP API παρόχου)</h3>
          <label class="mut" for="smsUrl">URL αποστολής (POST)</label><input id="smsUrl" class="in" value="${esc(sms.url || '')}" placeholder="https://api.provider.gr/sms/send">
          <label class="mut" for="smsH">Headers (JSON)</label><input id="smsH" class="in" value="${esc(JSON.stringify(sms.headers || {}))}" placeholder='{"Authorization":"Bearer ..."}'>
          <label class="mut" for="smsB">Σώμα (JSON, με {to}, {text}, {sender})</label><textarea id="smsB" class="in">${esc(JSON.stringify(sms.body || { to: '{to}', text: '{text}', from: '{sender}' }, null, 1))}</textarea>
          <label class="mut" for="smsS">Όνομα αποστολέα (έως 11 λατινικοί χαρακτήρες)</label><input id="smsS" class="in" value="${esc(sms.sender || '')}"></div>
        <div class="pane" style="padding:14px"><h3 style="margin:0 0 6px">Email (SMTP)</h3>
          <label class="mut" for="smH">Server</label><input id="smH" class="in" value="${esc(smtp.host || '')}" placeholder="smtp.example.com">
          <label class="mut" for="smP">Θύρα</label><input id="smP" class="in" type="number" value="${esc(smtp.port || 587)}">
          <label class="mut" for="smU">Χρήστης</label><input id="smU" class="in" value="${esc(smtp.user || '')}" autocomplete="off">
          <label class="mut" for="smW">Κωδικός</label><input id="smW" class="in" type="password" value="${esc(smtp.password || '')}" autocomplete="new-password">
          <label class="mut" for="smF">Αποστολέας</label><input id="smF" class="in" value="${esc(smtp.from || '')}">
          <label class="check"><input type="checkbox" id="smT" ${smtp.tls === false ? '' : 'checked'}> STARTTLS</label></div>
      </div>${ro ? '' : '<div style="margin-top:14px"><button class="btn pri" id="setSave">Αποθήκευση</button></div>'}`;
    $$('.in', body).forEach(i => Object.assign(i.style, { width: '100%', background: 'var(--bg)', border: '1px solid var(--line2)', borderRadius: '8px', padding: '7px 9px', margin: '2px 0 8px' }));
    if (ro) return;
    $('#setSave').onclick = async () => {
      try {
        await api('/api/admin/settings', {
          sms: { url: $('#smsUrl').value.trim(), headers: JSON.parse($('#smsH').value || '{}'), body: JSON.parse($('#smsB').value || '{}'), sender: $('#smsS').value.trim() },
          smtp: { host: $('#smH').value.trim(), port: +$('#smP').value || 587, user: $('#smU').value, password: $('#smW').value, from: $('#smF').value, tls: $('#smT').checked },
        }, 'PUT');
        toast('Αποθηκεύτηκε');
      } catch (e) { toast(e.message.includes('JSON') ? 'Τα headers και το σώμα πρέπει να είναι έγκυρο JSON' : e.message, 'err'); }
    };
    flightBox(body, ro, s.flight || {});
    mediaBox(body, ro, s.media || {});
  }

  // live video + audio: address of the media server (empty = off, the console shows ~1 frame per second)
  function mediaBox(body, ro, m) {
    const el = document.createElement('div');
    el.innerHTML = `<div class="bar" style="margin-top:22px"><h2>Ζωντανό βίντεο με ήχο</h2></div>
      <p class="mut" style="max-width:85ch">Χρειάζεται media server (π.χ. MediaMTX σε δικό σας server). Όσο κάποιος βλέπει «Ζωντανά», το κουτί στέλνει εκεί βίντεο και ήχο της κάμερας,
        και ο browser το παίζει με WebRTC (καθυστέρηση περίπου 1″). Το ίδιο ισχύει για την κάμερα του drone όταν συνδεθεί πραγματικό Dock. Κενά πεδία: απενεργοποιημένο,
        και εμφανίζεται μία εικόνα ανά δευτερόλεπτο. Το <code>{path}</code> αντικαθίσταται από μυστικό όνομα ανά κάμερα.</p>
      <div class="pane" style="padding:14px;max-width:640px">
        <label class="mut" for="mdP">Αποστολή από το κουτί (rtsp / rtmp / srt)</label><input id="mdP" class="in" value="${esc(m.publish || '')}" placeholder="rtsp://χρήστης:κωδικός@media.example.com:8554/{path}" ${ro ? 'disabled' : ''}>
        <label class="mut" for="mdW">Προβολή στον browser (WHEP, https)</label><input id="mdW" class="in" value="${esc(m.play || '')}" placeholder="https://media.example.com:8889/{path}/whep" ${ro ? 'disabled' : ''}>
      </div>${ro ? '' : '<div style="margin-top:12px"><button class="btn pri" id="mdSave">Αποθήκευση</button></div>'}`;
    body.appendChild(el);
    $$('.in', el).forEach(i => Object.assign(i.style, { width: '100%', background: 'var(--bg)', border: '1px solid var(--line2)', borderRadius: '8px', padding: '7px 9px', margin: '2px 0 8px' }));
    if (ro) return;
    $('#mdSave').onclick = async () => {
      try { await api('/api/admin/settings', { media: { publish: $('#mdP').value.trim(), play: $('#mdW').value.trim() } }, 'PUT'); toast('Αποθηκεύτηκε'); } catch (e) { toast(e.message, 'err'); }
    };
  }

  // pre-flight limits (both platforms)
  function flightBox(body, ro, f) {
    const el = document.createElement('div');
    const fld = (k, l, unit) => `<label class="mut" for="fl_${k}">${l} (${unit})</label><input id="fl_${k}" class="in" type="number" step="any" value="${esc(f[k] ?? '')}" ${ro ? 'disabled' : ''}>`;
    el.innerHTML = `<div class="bar" style="margin-top:22px"><h2>Όρια πτήσης drone</h2></div>
      <p class="mut" style="max-width:80ch">Πριν από κάθε αποστολή ελέγχεται ο τρέχων καιρός στο σημείο (Open-Meteo). Πάνω από τα όρια η αποστολή μπλοκάρεται και μόνο επόπτης μπορεί να την παρακάμψει. Βάλτε τα όρια του drone σας από τον κατασκευαστή.</p>
      <div class="pane" style="padding:14px;max-width:520px;display:grid;grid-template-columns:1fr 1fr;column-gap:14px">
        <div>${fld('max_wind', 'Μέγιστος άνεμος', 'm/s')}</div><div>${fld('max_gust', 'Μέγιστες ριπές', 'm/s')}</div>
        <div>${fld('max_precip', 'Μέγιστη βροχόπτωση', 'mm')}</div><div></div>
        <div>${fld('min_temp', 'Ελάχιστη θερμοκρασία', '°C')}</div><div>${fld('max_temp', 'Μέγιστη θερμοκρασία', '°C')}</div>
      </div>${ro ? '' : '<div style="margin-top:12px"><button class="btn pri" id="flSave">Αποθήκευση ορίων</button></div>'}`;
    body.appendChild(el);
    $$('.in', el).forEach(i => Object.assign(i.style, { width: '100%', background: 'var(--bg)', border: '1px solid var(--line2)', borderRadius: '8px', padding: '7px 9px', margin: '2px 0 8px' }));
    if (ro) return;
    $('#flSave').onclick = async () => {
      const flight = {}; for (const k of ['max_wind', 'max_gust', 'max_precip', 'min_temp', 'max_temp']) { const v = $('#fl_' + k).value; if (v !== '') flight[k] = +v; }
      try { await api('/api/admin/settings', { flight }, 'PUT'); toast('Αποθηκεύτηκε'); } catch (e) { toast(e.message, 'err'); }
    };
  }

  // Cloudflare version: both SMS and email go through the provider's HTTP API (no SMTP from Pages).
  function renderSettingsHttp(body, ro, sms, em) {
    const box = (id, title, cfg, def, hint) => `<div class="pane" style="padding:14px"><h3 style="margin:0 0 6px">${title}</h3>
      <label class="mut" for="${id}U">URL αποστολής (POST)</label><input id="${id}U" class="in" value="${esc(cfg.url || '')}" placeholder="https://api.provider.com/send">
      <label class="mut" for="${id}H">Headers (JSON)</label><input id="${id}H" class="in" value="${esc(JSON.stringify(cfg.headers || {}))}" placeholder='{"Authorization":"Bearer ..."}'>
      <label class="mut" for="${id}B">Σώμα (JSON) · ${hint}</label><textarea id="${id}B" class="in">${esc(JSON.stringify(cfg.body || def, null, 1))}</textarea>
      <label class="mut" for="${id}S">Αποστολέας</label><input id="${id}S" class="in" value="${esc(cfg.sender || '')}"></div>`;
    body.innerHTML = `<div class="bar"><h2>Ειδοποιήσεις</h2></div>
      <p class="mut" style="max-width:75ch">Τα SMS και τα email στέλνονται μέσω του HTTP API του παρόχου σας. Χωρίς ρύθμιση καταγράφονται ως «προσομοίωση». Στην Ελλάδα το όνομα αποστολέα SMS χρειάζεται προεγγραφή.</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px">
        ${box('sms', 'SMS', sms, { to: '{to}', text: '{text}', from: '{sender}' }, 'μεταβλητές {to}, {text}, {sender}')}
        ${box('em', 'Email', em, { to: '{to}', subject: '{subject}', text: '{text}', from: '{sender}' }, 'μεταβλητές {to}, {subject}, {text}, {sender}')}
      </div>${ro ? '' : '<div style="margin-top:14px"><button class="btn pri" id="setSave">Αποθήκευση</button></div>'}`;
    $$('.in', body).forEach(i => Object.assign(i.style, { width: '100%', background: 'var(--bg)', border: '1px solid var(--line2)', borderRadius: '8px', padding: '7px 9px', margin: '2px 0 8px' }));
    if (ro) return;
    const read = id => ({ url: $(`#${id}U`).value.trim(), headers: JSON.parse($(`#${id}H`).value || '{}'), body: JSON.parse($(`#${id}B`).value || '{}'), sender: $(`#${id}S`).value.trim() });
    $('#setSave').onclick = async () => {
      try { await api('/api/admin/settings', { sms: read('sms'), email: read('em') }, 'PUT'); toast('Αποθηκεύτηκε'); }
      catch (e) { toast(e.message.includes('JSON') ? 'Τα headers και το σώμα πρέπει να είναι έγκυρο JSON' : e.message, 'err'); }
    };
  }

  async function renderLog(body, kind) {
    const rows = await api('/api/admin/' + kind);
    body.innerHTML = kind === 'events'
      ? `<div class="bar"><h2>Καταγραφή σημάτων</h2><span class="mut">τελευταία 300 · όλα τα σήματα, και όσα φιλτραρίστηκαν</span></div><table><thead><tr><th>Ώρα</th><th>Επιχείρηση</th><th>Κάμερα</th><th>Τύπος</th><th>Πηγή</th><th>Αποτέλεσμα</th></tr></thead>
         <tbody>${rows.map(r => `<tr><td>${fmtTs(r.ts)}</td><td>${esc(r.org)}</td><td>${esc(r.camera)}</td><td>${esc(TYPES[r.type] || r.type)}<div class="mut">${esc(r.raw_type)} ${esc(r.target || '')}</div></td><td>${esc(r.source)}</td>
         <td>${r.suppressed ? `<span class="mut">φιλτραρίστηκε: ${esc(r.suppressed)}</span>` : r.incident_id ? `περιστατικό #${r.incident_id}` : ''}</td></tr>`).join('')}</tbody></table>`
      : `<div class="bar"><h2>Ιστορικό ενεργειών</h2></div><table><thead><tr><th>Ώρα</th><th>Χρήστης</th><th>Ενέργεια</th><th>Λεπτομέρειες</th></tr></thead>
         <tbody>${rows.map(r => `<tr><td>${fmtTs(r.ts)}</td><td>${esc(r.who)}</td><td><code>${esc(r.action)}</code></td><td>${esc(r.detail)}</td></tr>`).join('')}</tbody></table>`;
  }

  window.Admin = { open };
})();
