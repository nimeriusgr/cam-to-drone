/* SKYWARDEN · detailed incident report, built from /api/incidents/{id}/report (same for both servers) */
(async function () {
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const dt = t => t ? new Date(t * 1000).toLocaleString('el-GR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '—';
  const tm = t => t ? new Date(t * 1000).toLocaleTimeString('el-GR', { hour12: false }) : '—';
  const span = s => { if (s == null || s < 0) return '—'; s = Math.round(s); if (s < 60) return `${s}″`; const m = Math.floor(s / 60); return m < 60 ? `${m}′ ${String(s % 60).padStart(2, '0')}″` : `${Math.floor(m / 60)}ω ${m % 60}′`; };
  const km = m => m == null ? '—' : m >= 1000 ? (m / 1000).toFixed(2) + ' km' : Math.round(m) + ' m';
  const STATUS = { new: 'Νέο', ack: 'Σε χειρισμό', dispatched: 'Drone σε αποστολή', closed: 'Κλειστό' };
  const PRIO = { 1: 'Υψηλή', 2: 'Μεσαία', 3: 'Χαμηλή', 4: 'Τεχνικό' };
  const CH = { sms: 'SMS', email: 'Email', voice: 'Κλήση', police: 'Αστυνομία' };
  const NST = { sent: 'στάλθηκε', simulated: 'προσομοίωση (χωρίς πάροχο)', failed: 'απέτυχε', logged: 'καταγράφηκε' };
  const KIND = { alarm: 'Συναγερμός', signal: 'Σήμα', ack: 'Ανάληψη', assign: 'Ανάθεση', sop: 'Πρωτόκολλο', note: 'Σημείωση', drone: 'Drone', speaker: 'Μεγάφωνο',
    notify: 'Ειδοποίηση', escalate: 'Κλιμάκωση', close: 'Κλείσιμο', camera: 'Κάμερα', check: 'Έλεγχος πτήσης', postpone: 'Αναβολή', clip: 'Κλιπ', info: 'Πληροφορία' };
  const PHASE = { takeoff: 'απογείωση', enroute: 'σε πορεία', onsite: 'στο σημείο', returning: 'επιστροφή', landed: 'προσγειώθηκε' };
  const id = +(location.pathname.match(/(\d+)\/?$/) || [])[1];
  let d;
  try {
    const r = await fetch(`/api/incidents/${id}/report`, { credentials: 'same-origin', cache: 'no-store' });
    if (r.status === 401) { $('#r').innerHTML = '<p>Συνδεθείτε στην κονσόλα και ανοίξτε ξανά την αναφορά.</p><p><a href="/">Σύνδεση</a></p>'; return; }
    d = await r.json(); if (!r.ok) throw new Error(d.error || r.status);
  } catch (e) { $('#r').innerHTML = `<p>Η αναφορά δεν φορτώθηκε: ${esc(e.message)}</p>`; return; }

  // time only when on the incident's day, otherwise date and time
  const day0 = new Date(d.first_ts * 1000).toDateString();
  const tw = t => !t ? '—' : new Date(t * 1000).toDateString() === day0 ? tm(t) : dt(t);
  const org = d.org || {}, cam = d.camera || {}, closed = d.status === 'closed';
  const lat = cam.lat ?? org.lat, lon = cam.lon ?? org.lon;
  const tl = d.timeline || [], ev = d.events || [], ms = d.missions || [], nt = d.notifications || [];
  const sop = org.sop || [], done = new Set(d.sop_done || []);
  const sopWhen = k => { const x = tl.find(t => t.kind === 'sop' && t.text === `✓ ${sop[k]}`); return x ? `${tw(x.ts)} · ${esc(x.who)}` : ''; };
  const ackS = d.acked_ts ? d.acked_ts - d.first_ts : null, endS = (d.closed_ts || d.generated) - d.first_ts;
  const postpones = tl.filter(t => t.kind === 'postpone'), checks = tl.filter(t => t.kind === 'check');
  document.title = `Αναφορά #${d.id} · ${org.name || ''}`;

  const evRows = ev.map(e => `<tr><td class="t">${tw(e.ts)}</td><td>${esc(e.camera || '—')}</td><td>${esc((d.types || {})[e.type] || e.type)}</td>
    <td class="muted">${esc(e.raw_type || '')}${e.target ? ' · ' + esc(e.target) : ''}</td><td class="muted">${esc(e.source || '')}</td><td>${e.image ? '📷' : ''}${e.clip ? ' 🎬' : ''}</td></tr>`).join('');
  const msRows = ms.map(m => `<table class="kv"><tr><th>Σταθμός</th><td>${esc(m.dock.name)} ${m.simulated ? '<span class="muted">(προσομοίωση)</span>' : ''}</td></tr>
    <tr><th>Αποστολή</th><td>${dt(m.start_ts)}</td></tr>
    <tr><th>Άφιξη στο σημείο</th><td>${m.onsite_ts ? `${dt(m.onsite_ts)} <span class="muted">(σε ${span(m.onsite_ts - m.start_ts)})</span>` : '—'}</td></tr>
    <tr><th>Εντολή επιστροφής</th><td>${dt(m.return_ts)}</td></tr><tr><th>Προσγείωση</th><td>${dt(m.landed_ts)}${m.landed_ts ? ` <span class="muted">(πτήση ${span(m.landed_ts - m.start_ts)})</span>` : ` <span class="muted">· τώρα: ${PHASE[m.phase] || m.phase}</span>`}</td></tr>
    <tr><th>Απόσταση από σταθμό</th><td>${km(m.distance_m)}</td></tr><tr><th>Στόχος</th><td>${(+m.tlat).toFixed(5)}, ${(+m.tlon).toFixed(5)}</td></tr></table>`).join('');
  const imgs = ev.filter(e => e.image).map(e => `<figure><img src="/media/${encodeURIComponent(e.image)}" alt="" loading="lazy" onerror="this.parentNode.remove()"><figcaption>${tw(e.ts)} · ${esc(e.camera || '')}</figcaption></figure>`).join('');
  const clips = ev.filter(e => e.clip).map(e => `<li><a href="/media/${encodeURIComponent(e.clip)}" target="_blank">Κλιπ ${tw(e.ts)} · ${esc(e.camera || '')}</a></li>`).join('');

  $('#r').innerHTML = `
    <div class="head"><div><div class="brand">SKYWARDEN</div><div class="muted">${esc((d.center && d.center.name) || 'Κέντρο λήψης σημάτων')}</div></div>
      <div class="meta">Αναφορά περιστατικού <b>#${d.id}</b><br>Δημιουργήθηκε ${dt(d.generated)}<br>από ${esc(d.generated_by || '')}</div></div>
    <h1>${esc(d.type_label)} · ${esc(org.name || '')}<span class="status ${closed ? 'closed' : 'open'}">${closed ? esc(d.outcome || 'Κλειστό') : STATUS[d.status]}</span></h1>
    <div class="muted">${dt(d.first_ts)} · ${PRIO[d.priority]} προτεραιότητα${d.escalated ? ' · <span class="warn">κλιμακώθηκε (καθυστέρηση ανάληψης)</span>' : ''}</div>
    <div class="grid">
      <div class="kpi"><div class="l">Ανάληψη σε</div><div class="v">${span(ackS)}</div></div>
      <div class="kpi"><div class="l">${closed ? 'Συνολική διάρκεια' : 'Ανοιχτό εδώ και'}</div><div class="v">${span(endS)}</div></div>
      <div class="kpi"><div class="l">Σήματα</div><div class="v">${d.count}</div></div>
      <div class="kpi"><div class="l">Drone</div><div class="v">${ms.length ? (ms[0].onsite_ts ? 'στο σημείο σε ' + span(ms[0].onsite_ts - ms[0].start_ts) : 'στάλθηκε') : postpones.length ? 'αναβολή' : 'όχι'}</div></div>
    </div>

    <h2>Επιχείρηση και τόπος</h2>
    <table class="kv"><tr><th>Επιχείρηση</th><td>${esc(org.name)}</td></tr><tr><th>Διεύθυνση</th><td>${esc(org.address || '—')}</td></tr>
      <tr><th>Συντεταγμένες</th><td>${lat != null ? `${(+lat).toFixed(5)}, ${(+lon).toFixed(5)}` : '—'}</td></tr>
      <tr><th>Κάμερες περιστατικού</th><td>${(d.camera_list || []).map(c => esc(c.name)).join(', ') || '—'}</td></tr>
      ${org.notes ? `<tr><th>Σημειώσεις επιχείρησης</th><td>${esc(org.notes)}</td></tr>` : ''}
      <tr><th>Επαφές</th><td>${(d.contacts || []).map(c => `${esc(c.name)} <span class="muted">(${esc(c.role || '')}) ${esc(c.phone || '')} ${esc(c.email || '')}</span>`).join('<br>') || '—'}</td></tr></table>

    <h2>Χρόνοι</h2>
    <table class="kv"><tr><th>Πρώτο σήμα</th><td>${dt(d.first_ts)}</td></tr><tr><th>Τελευταίο σήμα</th><td>${dt(d.last_ts)}</td></tr>
      <tr><th>Ανάληψη</th><td>${dt(d.acked_ts)}${ackS != null ? ` <span class="muted">(σε ${span(ackS)})</span>` : ''}</td></tr>
      <tr><th>Ελεγκτής</th><td>${esc(d.assigned_name || '—')}</td></tr>
      <tr><th>Κλείσιμο</th><td>${dt(d.closed_ts)}</td></tr></table>

    <h2>Σήματα καμερών (${ev.length})</h2>
    ${ev.length ? `<table><tr><th>Ώρα</th><th>Κάμερα</th><th>Τύπος</th><th>Ανίχνευση</th><th>Πηγή</th><th></th></tr>${evRows}</table>` : '<p class="muted">Χωρίς σήματα καμερών (τεχνικό περιστατικό ή χειροκίνητο).</p>'}

    ${sop.length ? `<h2>Πρωτόκολλο ενεργειών (${done.size}/${sop.length})</h2><table>${sop.map((s, k) => `<tr><td style="width:28px" class="${done.has(k) ? 'ok' : 'no'}">${done.has(k) ? '✓' : '✗'}</td><td>${esc(s)}</td><td class="muted t">${sopWhen(k)}</td></tr>`).join('')}</table>` : ''}

    <h2>Drone</h2>
    ${ms.length ? msRows : '<p class="muted">Δεν στάλθηκε drone.</p>'}
    ${checks.length ? `<p><b>Έλεγχος πτήσης:</b><br>${checks.map(c => `${tw(c.ts)} · ${esc(c.text)}`).join('<br>')}</p>` : ''}
    ${postpones.length ? `<p class="warn"><b>Αναβολή αποστολής:</b><br>${postpones.map(c => `${tw(c.ts)} · ${esc(c.text.replace(/^Αναβολή αποστολής drone: /, ''))} · ${esc(c.who)}`).join('<br>')}</p>` : ''}

    <h2>Ειδοποιήσεις και κλήσεις (${nt.length})</h2>
    ${nt.length ? `<table><tr><th>Ώρα</th><th>Κανάλι</th><th>Προς / αποτέλεσμα</th><th>Κατάσταση</th><th>Από</th></tr>${nt.map(n => `<tr><td class="t">${tw(n.ts)}</td><td>${CH[n.channel] || esc(n.channel)}</td><td>${esc(n.target)}</td><td>${NST[n.status] || esc(n.status)}${n.error ? ` <span class="muted">${esc(n.error)}</span>` : ''}</td><td>${esc(n.who || '')}</td></tr>`).join('')}</table>` : '<p class="muted">Καμία ειδοποίηση.</p>'}

    <h2>Εικόνες${clips ? ' και κλιπ' : ''}</h2>
    ${imgs ? `<div class="imgs">${imgs}</div>` : '<p class="muted">Δεν υπάρχουν εικόνες (ή διαγράφηκαν με βάση την πολιτική διατήρησης).</p>'}
    ${clips ? `<ul>${clips}</ul>` : ''}

    <h2>Χρονολόγιο (${tl.length})</h2>
    <table class="tl"><tr><th>Ώρα</th><th>Είδος</th><th>Ενέργεια</th><th>Από</th></tr>
      ${tl.map(t => `<tr><td class="t">${tw(t.ts)}</td><td class="k-${esc(t.kind)}">${KIND[t.kind] || esc(t.kind)}</td><td>${esc(t.text)}</td><td>${esc(t.who)}</td></tr>`).join('')}</table>

    <h2>Αποτέλεσμα</h2>
    <table class="kv"><tr><th>Αποτέλεσμα</th><td>${esc(d.outcome || (closed ? '—' : 'Ανοιχτό'))}</td></tr><tr><th>Σημείωση κλεισίματος</th><td>${esc(d.note || '—')}</td></tr></table>

    <div class="sign"><div>Ελεγκτής: ${esc(d.assigned_name || '')}</div><div>Επόπτης βάρδιας</div></div>
    <div class="foot">SKYWARDEN · Αναφορά #${d.id} · ${dt(d.generated)} · Οι ώρες είναι ώρες Ελλάδας όπως καταγράφηκαν από τον server.</div>`;
})();
