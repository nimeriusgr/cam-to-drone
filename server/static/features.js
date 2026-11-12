/* SKYWARDEN · shifts, reports, live camera, push notifications, customer (mobile) view */
(function () {
  const F = {};
  const ROLE = { admin: 'Διαχειριστής', supervisor: 'Επόπτης', operator: 'Ελεγκτής', customer: 'Πελάτης' };
  const secs = s => s == null ? '—' : s < 60 ? `${Math.round(s)}″` : s < 3600 ? `${Math.floor(s / 60)}′ ${String(Math.round(s % 60)).padStart(2, '0')}″` : `${Math.floor(s / 3600)}ω ${Math.round(s % 3600 / 60)}′`;
  const dateTime = t => new Date(t * 1000).toLocaleString('el-GR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const isStaff = () => S.me && S.me.role !== 'customer';

  F.boot = async () => {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => { });
      navigator.serviceWorker.addEventListener('message', e => { if (e.data && e.data.open) openFromUrl(e.data.open); });
    }
    $('#pushBtn').onclick = () => { $('#userMenu').hidden = true; pushModal(); };
    if (isStaff()) { $('#shiftBtn').onclick = shiftModal; await loadShift(); }
    openFromUrl(location.href);
    window.addEventListener('hashchange', () => openFromUrl(location.href));
  };
  F.afterRefresh = fresh => {
    if (isStaff() && F.shift && S.ov.on_duty) { F.shift.on_duty = S.ov.on_duty; }
    if (S.me.role === 'customer') { if (S.view === 'cust') renderCustomer(); if (fresh.length && 'vibrate' in navigator) navigator.vibrate([200, 100, 200]); }
  };
  function openFromUrl(url) {
    const m = String(url).match(/#inc=(\d+)/); if (!m) return;
    const id = +m[1];
    history.replaceState(null, '', location.pathname);
    if (S.me.role === 'customer') { showView('cust'); openCustIncident(id); } else { showView('console'); select(id); }
  }

  /* ================================================================ shifts */
  async function loadShift() { try { F.shift = await api('/api/shift'); } catch { return; } renderShiftBtn(); }
  function renderShiftBtn() {
    const b = $('#shiftBtn'), c = F.shift && F.shift.current;
    b.hidden = false;
    b.textContent = c ? `Σε βάρδια από ${hm(c.start_ts)}` : 'Εκτός βάρδιας';
    b.classList.toggle('on', !c);
    b.title = c ? 'Τέλος βάρδιας και σημείωμα παράδοσης' : 'Έναρξη βάρδιας: λαμβάνετε ειδοποιήσεις και αναθέσεις';
  }
  function dutyList(duty) {
    return `<label>Σε βάρδια τώρα</label>${duty.length ? `<div class="duty">${duty.map(d => `<div><span class="dot"></span><b>${esc(d.name)}</b> <span class="mut">${ROLE[d.role] || ''} · από ${hm(d.start_ts)} · ${d.open_n} ανοιχτά</span></div>`).join('')}</div>` : '<p class="mut">Κανείς</p>'}`;
  }
  async function shiftModal() {
    await loadShift();
    const s = F.shift || {}, c = s.current, lh = s.last_handover;
    if (!c) {
      modal(`<h2>Έναρξη βάρδιας</h2>
        ${lh ? `<label>Σημείωμα παράδοσης από ${esc(lh.who)} · ${dateTime(lh.ts)}</label><pre class="sum">${esc(lh.text)}</pre>` : '<p class="mut">Δεν υπάρχει σημείωμα παράδοσης από το τελευταίο 24ωρο.</p>'}
        ${dutyList(s.on_duty || [])}
        <p class="mut">Όσο είστε σε βάρδια, λαμβάνετε ειδοποιήσεις για νέους συναγερμούς και μπορούν να σας αναθέτουν περιστατικά.</p>`,
        'Έναρξη βάρδιας', async () => { F.shift = await api('/api/shift/start', {}); renderShiftBtn(); toast('Η βάρδια ξεκίνησε'); scheduleRefresh(); });
    } else {
      modal(`<h2>Τέλος βάρδιας</h2><p class="mut">Σε βάρδια από ${hm(c.start_ts)} · ${secs(Date.now() / 1000 - c.start_ts)}</p>
        ${dutyList(s.on_duty || [])}
        <label for="hoN">Σημείωμα παράδοσης για την επόμενη βάρδια</label>
        <textarea id="hoN" placeholder="Τι εκκρεμεί, ποιες κάμερες έχουν πρόβλημα, τι πρέπει να προσέξει ο επόμενος"></textarea>
        ${s.my_open ? `<label class="check"><input type="checkbox" id="hoR" checked> Αποδέσμευση των ${s.my_open} ανοιχτών περιστατικών μου</label>` : ''}`,
        'Τέλος βάρδιας', async () => { F.shift = await api('/api/shift/end', { note: $('#hoN').value, release: !!($('#hoR') && $('#hoR').checked) }); renderShiftBtn(); toast('Η βάρδια έκλεισε'); scheduleRefresh(); });
    }
  }

  /* ================================================================ WebRTC playback (WHEP) from the media server: video + sound, about 1 s delay */
  F.whep = async (url, video) => {
    const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.cloudflare.com:3478' }] });
    pc.addTransceiver('video', { direction: 'recvonly' }); pc.addTransceiver('audio', { direction: 'recvonly' });
    const ms = new MediaStream(); video.srcObject = ms; pc.ontrack = e => ms.addTrack(e.track);
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise(r => { if (pc.iceGatheringState === 'complete') return r(); const t = setTimeout(r, 2000); pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') { clearTimeout(t); r(); } }; });
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: pc.localDescription.sdp });
    if (!res.ok) { pc.close(); throw new Error(`HTTP ${res.status}`); }
    await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() });
    const loc = res.headers.get('Location');
    return () => { pc.close(); if (loc) fetch(new URL(loc, url), { method: 'DELETE' }).catch(() => { }); };
  };
  // <video> with a sound button (browsers start video muted; one click turns sound on)
  F.videoHtml = (label) => `<video autoplay muted playsinline></video><div class="live-msg empty">${label}</div>
    <span class="live-tag">LIVE</span><button type="button" class="btn sm snd">🔇 Ήχος</button>`;
  F.bindSound = box => { const v = $('video', box), b = $('.snd', box); b.onclick = () => { v.muted = !v.muted; if (!v.muted) v.play().catch(() => { }); b.textContent = v.muted ? '🔇 Ήχος' : '🔊 Ήχος'; }; };

  /* ================================================================ live camera: real video+sound through the media server, or ~1 frame/s through the box */
  let live = null;
  F.liveCam = () => live && live.cam;
  F.stopLive = () => { if (live) { clearInterval(live.timer); if (live.close) live.close(); live.dead = true; live = null; } };
  F.startLive = async (cam, box) => {
    F.stopLive();
    box.dataset.img = 'live:' + cam;
    const me = live = { cam, timer: null };
    let st;
    try { st = await api(`/api/cameras/${cam}/live`); } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (me.dead) return;
    if (st.whep && 'RTCPeerConnection' in window) return startVideo(me, st, box);
    startFrames(me, cam, box);
  };
  async function startVideo(me, st, box) {
    box.innerHTML = F.videoHtml('Το κουτί ξεκινά τη ροή βίντεο…'); F.bindSound(box);
    const v = $('video', box), msg = $('.live-msg', box);
    v.onplaying = () => { msg.hidden = true; };
    me.timer = setInterval(() => api(`/api/cameras/${me.cam}/live`).catch(() => { }), 10000); // keeps the box streaming
    for (let k = 0; k < 15 && !me.dead; k++) { // the box needs a few seconds to start sending
      try { const close = await F.whep(st.whep, v); if (me.dead) close(); else me.close = close; return; }
      catch { await new Promise(r => setTimeout(r, 2000)); }
    }
    if (!me.dead) { msg.textContent = 'Δεν ήρθε βίντεο από τον media server. Εμφάνιση εικόνας ανά δευτερόλεπτο.'; clearInterval(me.timer); setTimeout(() => !me.dead && startFrames(me, me.cam, box), 1500); }
  }
  function startFrames(me, cam, box) {
    box.innerHTML = `<img alt="Ζωντανή εικόνα κάμερας" hidden><div class="live-msg empty">Σύνδεση με την κάμερα μέσω του κουτιού…</div><span class="live-tag">LIVE · ~1 καρέ/δευτ.</span>`;
    const img = $('img', box), msg = $('.live-msg', box);
    let n = 0, lastTs = null;
    const tick = async () => {
      if (live !== me || document.hidden) return;
      n++;
      if (n % 5 === 1) {
        try {
          const st = await api(`/api/cameras/${cam}/live`);
          if (!st.edge_online) { msg.textContent = 'Το κουτί της επιχείρησης δεν είναι συνδεδεμένο.'; msg.hidden = false; }
          lastTs = st.frame_ts;
        } catch (e) { msg.textContent = e.message; msg.hidden = false; return; }
      }
      const probe = new Image();
      probe.onload = () => { if (live !== me) return; img.src = probe.src; img.hidden = false; msg.hidden = true; };
      probe.onerror = () => { if (n > 12 && img.hidden) msg.textContent = 'Δεν έρχεται εικόνα. Ελέγξτε ότι το κουτί έχει την έκδοση 1.1 και πρόσβαση στην κάμερα.'; };
      probe.src = `/api/cameras/${cam}/frame?t=${Date.now()}`;
    };
    me.timer = setInterval(tick, 1000);
    tick();
  }

  /* ================================================================ push notifications */
  const u8 = s => { s = s.replace(/-/g, '+').replace(/_/g, '/'); return Uint8Array.from(atob(s + '='.repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0)); };
  const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  async function currentSub() { try { const reg = await navigator.serviceWorker.ready; return await reg.pushManager.getSubscription(); } catch { return null; } }
  F.pushEnable = async () => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window))
      throw new Error(isIos() && !standalone() ? 'Στο iPhone: πατήστε Κοινή χρήση → «Προσθήκη στην οθόνη Αφετηρίας», ανοίξτε την εφαρμογή από το εικονίδιο και ενεργοποιήστε από εκεί.' : 'Αυτός ο browser δεν υποστηρίζει ειδοποιήσεις.');
    const st = await api('/api/push'); if (!st.enabled) throw new Error(st.reason || 'Οι ειδοποιήσεις δεν είναι διαθέσιμες στον server');
    const perm = await Notification.requestPermission(); if (perm !== 'granted') throw new Error('Δεν δόθηκε άδεια για ειδοποιήσεις. Επιτρέψτε τες από τις ρυθμίσεις του browser.');
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (sub) { const k = sub.options && sub.options.applicationServerKey; if (k && btoa(String.fromCharCode(...new Uint8Array(k))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') !== st.key) { await sub.unsubscribe(); sub = null; } }
    if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: u8(st.key) });
    return api('/api/push/subscribe', sub.toJSON());
  };
  F.pushDisable = async () => { const sub = await currentSub(); if (sub) { await api('/api/push/unsubscribe', { endpoint: sub.endpoint }).catch(() => { }); await sub.unsubscribe(); } };
  async function pushModal() {
    const sub = 'serviceWorker' in navigator ? await currentSub() : null;
    const what = isStaff() ? 'νέους συναγερμούς και κλιμακώσεις (όσο είστε σε βάρδια, ή όλους αν δεν υπάρχει κανείς σε βάρδια)' : 'κάθε συναγερμό στην επιχείρησή σας, την άφιξη του drone και το κλείσιμο του περιστατικού';
    modal(`<h2>Ειδοποιήσεις σε αυτή τη συσκευή</h2><p>Λαμβάνετε ειδοποίηση για ${what}, ακόμα κι όταν η σελίδα είναι κλειστή.</p>
      <p class="mut">Κατάσταση: <b>${sub ? 'ενεργές σε αυτή τη συσκευή' : 'ανενεργές'}</b></p>
      ${isIos() && !standalone() ? '<p class="mut">Στο iPhone οι ειδοποιήσεις λειτουργούν μόνο αν προσθέσετε την εφαρμογή στην οθόνη Αφετηρίας (Κοινή χρήση → Προσθήκη στην οθόνη Αφετηρίας).</p>' : ''}
      ${sub ? '<button class="btn sm" id="pTest">Δοκιμαστική ειδοποίηση</button> <button class="btn sm" id="pOff">Απενεργοποίηση</button>' : ''}`,
      sub ? 'Κλείσιμο' : 'Ενεργοποίηση', async () => { if (!sub) { await F.pushEnable(); toast('Οι ειδοποιήσεις ενεργοποιήθηκαν'); } });
    if (sub) {
      $('#pTest').onclick = async () => { try { await api('/api/push/test', {}); toast('Στάλθηκε δοκιμαστική ειδοποίηση'); } catch (e) { toast(e.message, 'err'); } };
      $('#pOff').onclick = async () => { await F.pushDisable(); $('#modal').innerHTML = ''; toast('Οι ειδοποιήσεις απενεργοποιήθηκαν'); };
    }
  }

  /* ================================================================ reports */
  const R = { days: 30, org: '', data: null };
  const C_REAL = '#3987e5', C_FALSE = '#d95926'; // validated pair on the dark surface (blue / orange)
  F.openReports = async () => {
    const el = $('#reports');
    const custs = (S.ov ? S.ov.orgs : []).filter(o => o.kind === 'customer');
    el.innerHTML = `<div class="rep-bar">
        <h2>Αναφορές</h2>
        <div class="chips" id="rDays">${[[7, '7 ημέρες'], [30, '30 ημέρες'], [90, '90 ημέρες'], [365, '12 μήνες']].map(([d, l]) => `<button class="chip ${R.days === d ? 'on' : ''}" data-d="${d}">${l}</button>`).join('')}</div>
        ${custs.length > 1 ? `<select id="rOrg" aria-label="Επιχείρηση"><option value="">Όλες οι επιχειρήσεις</option>${custs.map(o => `<option value="${o.id}" ${String(o.id) === R.org ? 'selected' : ''}>${esc(o.name)}</option>`).join('')}</select>` : ''}
        <span class="sp"></span>
        <button class="btn sm" id="rCsv">Λήψη CSV</button><button class="btn sm" id="rPrint">Εκτύπωση</button>
      </div><div id="rBody" class="rep-body"><div class="empty">Φόρτωση…</div></div>`;
    $$('#rDays .chip').forEach(c => c.onclick = () => { R.days = +c.dataset.d; F.openReports(); });
    if ($('#rOrg')) $('#rOrg').onchange = e => { R.org = e.target.value; F.openReports(); };
    const to = Math.floor(Date.now() / 1000), frm = to - R.days * 86400, q = `from=${frm}&to=${to}${R.org ? '&org_id=' + R.org : ''}`;
    $('#rCsv').onclick = () => { location.href = `/api/stats/incidents.csv?${q}`; };
    $('#rPrint').onclick = () => print();
    try { R.data = await api(`/api/stats?${q}`); } catch (e) { $('#rBody').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    renderReport(R.data);
  };
  function tile(label, value, sub) { return `<div class="tile"><div class="tl">${label}</div><div class="tv">${value}</div>${sub ? `<div class="ts">${sub}</div>` : ''}</div>`; }
  function renderReport(d) {
    const t = d.totals, pctf = v => v == null ? '—' : Math.round(v * 100) + '%';
    const staff = !!d.by_operator;
    $('#rBody').innerHTML = `
      <div class="tiles">
        ${tile('Περιστατικά', t.incidents, `${t.open} ανοιχτά · ${t.escalated} κλιμακώσεις`)}
        ${tile('Ψευδείς συναγερμοί', pctf(t.false_rate), `${t.false_alarms} από ${t.closed} κλειστά`)}
        ${tile('Πραγματικά συμβάντα', t.real, `${t.technical} τεχνικά`)}
        ${tile('Χρόνος ανάληψης', secs(t.avg_ack_s), `μέσος · 90% έως ${secs(t.p90_ack_s)}`)}
        ${tile('Αποστολές drone', t.drone_missions, `άφιξη σε ${secs(t.avg_onsite_s)} κατά μέσο όρο`)}
        ${tile('Σήματα καμερών', d.signals.total, `${d.signals.suppressed} φιλτραρίστηκαν (ωράριο, παύση, τύπος)`)}
      </div>
      <div class="charts">
        <section class="chart wide"><h3>Περιστατικά ανά ημέρα</h3>${legend([[C_REAL, 'Λοιπά'], [C_FALSE, 'Ψευδείς']])}<div data-chart="day"></div></section>
        <section class="chart"><h3>Ανά ώρα της ημέρας</h3><div data-chart="hour"></div></section>
        <section class="chart"><h3>Ανά τύπο συναγερμού</h3>${hbars(d.by_type.map(x => [x.label, x.count]))}</section>
        <section class="chart"><h3>Αποτελέσματα</h3>${hbars(d.by_outcome.map(x => [x.outcome, x.count]))}</section>
      </div>
      <section class="chart wide"><h3>Ανά επιχείρηση</h3>${table(['Επιχείρηση', 'Περιστατικά', 'Ψευδείς', 'Drone', 'Μέσος χρόνος ανάληψης'],
        d.by_org.map(o => [esc(o.name), o.incidents, `${o.false_alarms} <span class="mut">(${pctf(o.false_rate)})</span>`, o.drone, secs(o.avg_ack_s)]))}</section>
      ${staff ? `<section class="chart wide"><h3>Ανά ελεγκτή</h3>${table(['Ελεγκτής', 'Περιστατικά', 'Drone', 'Μέσος χρόνος ανάληψης'],
        d.by_operator.map(o => [esc(o.name), o.handled, o.drone, secs(o.avg_ack_s)]))}</section>` : ''}`;
    // draw the SVG charts at the real width of their box, so text keeps its size
    const draw = () => {
      for (const el of $$('#rBody [data-chart]')) {
        const w = Math.max(280, Math.floor(el.clientWidth));
        el.innerHTML = el.dataset.chart === 'day' ? dayChart(d.by_day, w) : hourChart(d.by_hour, w);
      }
      bindTips($('#rBody'));
    };
    draw();
    window.onresize = () => { clearTimeout(R.rt); R.rt = setTimeout(() => { if (!$('#reports').hidden && R.data === d) draw(); }, 150); };
  }
  const legend = items => `<div class="legend">${items.map(([c, l]) => `<span><i style="background:${c}"></i>${l}</span>`).join('')}</div>`;
  const table = (head, rows) => rows.length ? `<table class="rt"><thead><tr>${head.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>` : '<p class="mut">Δεν υπάρχουν δεδομένα για το διάστημα.</p>';
  function hbars(items) {
    if (!items.length) return '<p class="mut">Δεν υπάρχουν δεδομένα.</p>';
    const max = Math.max(...items.map(x => x[1]));
    return `<div class="hb">${items.map(([l, v]) => `<div class="hb-r" data-tip="${esc(l)}: ${v}"><span class="hb-l">${esc(l)}</span><span class="hb-t"><i style="width:${Math.max(2, v / max * 100)}%"></i></span><span class="hb-v">${v}</span></div>`).join('')}</div>`;
  }
  // rounded top, square base anchored to the baseline
  const bar = (x, y, w, h, fill) => h <= 0 ? '' : h < 4 ? `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}"/>` : `<path d="M${x},${y + h}V${y + 3}q0,-3 3,-3h${w - 6}q3,0 3,3V${y + h}Z" fill="${fill}"/>`;
  function axisY(max, H, top, W, left) {
    const step = Math.max(1, Math.ceil(max / 4)), lines = [];
    for (let v = 0; v <= max; v += step) { const y = top + H - v / max * H; lines.push(`<line x1="${left}" x2="${W}" y1="${y}" y2="${y}" class="grid"/><text x="${left - 6}" y="${y + 4}" class="ax" text-anchor="end">${v}</text>`); }
    return lines.join('');
  }
  function dayChart(days, W) {
    const H = 180, top = 10, left = 30, n = days.length || 1, max = Math.max(1, ...days.map(d => d.incidents));
    const slot = (W - left) / n, bw = Math.max(2, Math.min(22, slot - 2));
    const every = Math.ceil(n / Math.max(2, Math.floor((W - left) / 56)));
    return `<svg viewBox="0 0 ${W} ${H + 34}" width="${W}" height="${H + 34}" class="svgc" role="img" aria-label="Περιστατικά ανά ημέρα">${axisY(max, H, top, W, left)}
      ${days.map((d, k) => {
        const x = left + k * slot + (slot - bw) / 2, hf = d.false_alarms / max * H, hr = (d.incidents - d.false_alarms) / max * H, base = top + H;
        const lbl = new Date(d.day + 'T12:00:00').toLocaleDateString('el-GR', { day: '2-digit', month: '2-digit' });
        return `<g class="hit" data-tip="${lbl}: ${d.incidents} περιστατικά · ${d.false_alarms} ψευδείς · ${d.drone} με drone"><rect x="${left + k * slot}" y="${top}" width="${slot}" height="${H}" fill="transparent"/>
          ${hr > 0 ? (hf > 0 ? `<rect x="${x}" y="${base - hr}" width="${bw}" height="${hr}" fill="${C_REAL}"/>` : bar(x, base - hr, bw, hr, C_REAL)) : ''}
          ${hf > 0 ? bar(x, base - (hr > 0 ? hr + 2 : 0) - hf, bw, hf, C_FALSE) : ''}
          ${k % every === 0 ? `<text x="${left + k * slot + slot / 2}" y="${H + top + 18}" class="ax" text-anchor="middle">${lbl}</text>` : ''}</g>`;
      }).join('')}</svg>`;
  }
  function hourChart(hours, W) {
    const H = 140, top = 10, left = 26, max = Math.max(1, ...hours), slot = (W - left) / 24, bw = slot - 2;
    return `<svg viewBox="0 0 ${W} ${H + 30}" width="${W}" height="${H + 30}" class="svgc" role="img" aria-label="Περιστατικά ανά ώρα">${axisY(max, H, top, W, left)}
      ${hours.map((v, h) => `<g class="hit" data-tip="${String(h).padStart(2, '0')}:00–${String(h).padStart(2, '0')}:59 · ${v} περιστατικά"><rect x="${left + h * slot}" y="${top}" width="${slot}" height="${H}" fill="transparent"/>
        ${bar(left + h * slot + 1, top + H - v / max * H, bw, v / max * H, C_REAL)}${h % (slot < 18 ? 6 : 3) === 0 ? `<text x="${left + h * slot + slot / 2}" y="${H + top + 16}" class="ax" text-anchor="middle">${String(h).padStart(2, '0')}</text>` : ''}</g>`).join('')}</svg>`;
  }
  function bindTips(root) {
    let tip = $('#vizTip'); if (!tip) { tip = document.createElement('div'); tip.id = 'vizTip'; tip.className = 'viztip'; tip.hidden = true; document.body.appendChild(tip); }
    $$('[data-tip]', root).forEach(el => {
      el.onmousemove = e => { tip.textContent = el.dataset.tip; tip.hidden = false; tip.style.left = Math.min(innerWidth - tip.offsetWidth - 8, e.clientX + 12) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; el.classList.add('hov'); };
      el.onmouseleave = () => { tip.hidden = true; el.classList.remove('hov'); };
    });
  }

  /* ================================================================ customer view (mobile first) */
  const CSTATUS = { new: 'Σε έλεγχο', ack: 'Σε χειρισμό', dispatched: 'Drone σε αποστολή', closed: 'Ολοκληρώθηκε' };
  let custOpen = null;
  function renderCustomer() {
    if (!S.ov) return;
    const el = $('#cust'), orgs = S.ov.orgs.filter(o => o.kind === 'customer');
    if (custOpen) return; // detail sheet is open; it refreshes itself
    const list = S.ov.incidents, open = list.filter(i => i.status !== 'closed');
    el.innerHTML = `
      ${orgs.map(o => `<section class="c-card c-org"><div><div class="c-t">${esc(o.name)}</div><div class="mut">${esc(o.address || '')}</div></div>
        <div class="c-state ${o.armed ? 'on' : ''}"><span><span class="dot ${o.armed ? '' : 'grey'}"></span>${o.armed ? 'Οπλισμένη' : 'Αφόπλιστη'}</span><div class="mut">${o.cams_online}/${o.cams_total} κάμερες online</div></div></section>`).join('')}
      <section class="c-card c-push" id="cPush"></section>
      <h3 class="c-h">${open.length ? `Σε εξέλιξη (${open.length})` : 'Δεν υπάρχει ανοιχτός συναγερμός'}</h3>
      ${open.map(itemHtml).join('')}
      <h3 class="c-h">Πρόσφατα</h3>
      ${list.filter(i => i.status === 'closed').map(itemHtml).join('') || '<p class="mut">Δεν υπάρχουν πρόσφατα περιστατικά.</p>'}`;
    $$('#cust [data-inc]').forEach(b => b.onclick = () => openCustIncident(+b.dataset.inc));
    renderPushCard();
  }
  F.renderCustomer = () => { custOpen = null; renderCustomer(); };
  function itemHtml(i) {
    return `<button class="c-item ${i.status !== 'closed' ? 'live' : ''}" data-inc="${i.id}">
      <span class="th" style="${i.thumb ? `background-image:url('/media/${encodeURIComponent(i.thumb)}')` : ''}"></span>
      <span class="c-main"><span class="c-t">${esc(i.type_label)} · ${esc(i.camera_name || '')}</span>
        <span class="mut">${dateTime(i.first_ts)}${S.ov.orgs.filter(o => o.kind === 'customer').length > 1 ? ' · ' + esc(i.org_name) : ''}</span></span>
      <span class="c-st ${i.status}">${i.status === 'closed' ? esc(i.outcome || CSTATUS.closed) : CSTATUS[i.status]}${i.flying ? '<br><small>drone στον αέρα</small>' : ''}</span></button>`;
  }
  async function renderPushCard() {
    const box = $('#cPush'); if (!box) return;
    const sub = 'serviceWorker' in navigator ? await currentSub() : null;
    box.innerHTML = sub ? `<div><b>Ειδοποιήσεις ενεργές</b><div class="mut">Θα ειδοποιηθείτε σε αυτή τη συσκευή για κάθε συναγερμό.</div></div><button class="btn sm" id="cPushBtn">Ρυθμίσεις</button>`
      : `<div><b>Ειδοποιήσεις στο κινητό</b><div class="mut">Μάθετε αμέσως για κάθε συναγερμό, την άφιξη του drone και το αποτέλεσμα.</div></div><button class="btn pri sm" id="cPushBtn">Ενεργοποίηση</button>`;
    $('#cPushBtn').onclick = async () => {
      if (sub) return pushModal();
      try { await F.pushEnable(); toast('Οι ειδοποιήσεις ενεργοποιήθηκαν'); renderPushCard(); } catch (e) { toast(e.message, 'err'); }
    };
  }
  async function openCustIncident(id) {
    custOpen = id; F.stopLive();
    const el = $('#cust');
    let i;
    try { i = await api(`/api/incidents/${id}`); } catch (e) { custOpen = null; toast(e.message, 'err'); renderCustomer(); return; }
    if (custOpen !== id) return;
    const m = i.mission, liveCam = i.camera_list.find(c => c.edge_id);
    el.innerHTML = `<button class="btn sm c-back" id="cBack">← Πίσω</button>
      <section class="c-card"><div class="c-t big">${esc(i.type_label)} · ${esc(i.camera ? i.camera.name : '')}</div>
        <div class="mut">${esc(i.org.name)} · ${dateTime(i.first_ts)} · #${i.id}</div>
        <div class="c-st ${i.status}" style="margin-top:8px">${i.status === 'closed' ? esc(i.outcome || CSTATUS.closed) : CSTATUS[i.status]}</div>
        ${m ? `<div class="mut" style="margin-top:6px">Drone από ${esc(m.dock.name)} · ${({ takeoff: 'απογείωση', enroute: 'σε πορεία', onsite: 'στο σημείο', returning: 'επιστροφή', landed: 'προσγειώθηκε' })[m.phase]}${m.eta ? ` · άφιξη σε ${dur(m.eta)}` : ''}</div>` : ''}
        ${i.note ? `<p>${esc(i.note)}</p>` : ''}</section>
      <section class="c-media" id="cMedia">${(i.clips || []).map(c => `<video src="/media/${encodeURIComponent(c.name)}" controls playsinline preload="metadata"></video>`).join('')}
        ${i.images.slice().reverse().map(n => `<img src="/media/${encodeURIComponent(n)}" alt="Εικόνα συναγερμού" loading="lazy">`).join('') || ((i.clips || []).length ? '' : '<p class="mut">Δεν υπάρχουν εικόνες.</p>')}</section>
      ${liveCam ? `<section class="c-card"><div class="c-row"><b>Ζωντανή εικόνα</b><button class="btn sm" id="cLive">Προβολή</button></div><div class="viewer c-live" id="cLiveBox" hidden></div></section>` : ''}
      <section class="c-card"><h3 class="c-h" style="margin-top:0">Τι έγινε</h3><ul class="tl">${i.timeline.filter(t => !['note', 'sop'].includes(t.kind)).map(t => `<li class="${t.kind}"><time>${hm(t.ts)}</time><span>${esc(t.text)}</span></li>`).join('')}</ul>
        <a class="btn sm" style="display:inline-block;margin-top:10px;text-decoration:none" href="/report/${i.id}" target="_blank">Αναφορά για εκτύπωση</a></section>`;
    $('#cBack').onclick = () => { F.stopLive(); custOpen = null; renderCustomer(); };
    const lb = $('#cLive');
    if (lb) lb.onclick = () => {
      const box = $('#cLiveBox');
      if (F.liveCam()) { F.stopLive(); box.hidden = true; lb.textContent = 'Προβολή'; } else { box.hidden = false; F.startLive(liveCam.id, box); lb.textContent = 'Τέλος'; }
    };
  }

  window.Features = F;
})();
