"""Business logic: alarm normalisation, arming/filters, incidents, escalation, drones, notifications, live bus."""
import base64
import hashlib
import hmac
import json
import math
import os
import queue
import re
import secrets
import smtplib
import threading
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage

import db

SIM_SPEED = float(os.environ.get("SIM_SPEED", "1"))
MERGE_SECONDS = 120          # alarms of the same organisation within this window join one incident
CAMERA_OFFLINE_AFTER = 120   # seconds without heartbeat
CRUISE_MS = 15.0             # simulated drone speed m/s
TAKEOFF_S = 20.0

# ---------------------------------------------------------------- types
TYPES = {
    "human":     ("Άτομο", 1),
    "vehicle":   ("Όχημα", 1),
    "input":     ("Είσοδος συναγερμού", 1),
    "zone":      ("Είσοδος σε ζώνη", 2),
    "line":      ("Διέλευση γραμμής", 2),
    "tamper":    ("Παραποίηση κάμερας", 2),
    "face":      ("Πρόσωπο", 2),
    "motion":    ("Κίνηση", 3),
    "other":     ("Συναγερμός", 3),
    "videoloss": ("Απώλεια εικόνας", 4),
    "offline":   ("Εκτός σύνδεσης", 4),
}
TECHNICAL = {"videoloss", "offline"}

RAW_MAP = {
    # Hikvision ISAPI eventType
    "vmd": "motion", "motiondetection": "motion", "linedetection": "line", "fielddetection": "zone",
    "regionentrance": "zone", "regionexiting": "zone", "shelteralarm": "tamper", "tamperdetection": "tamper",
    "videoloss": "videoloss", "io": "input", "facedetection": "face", "pir": "motion",
    # Dahua codes
    "videomotion": "motion", "crosslinedetection": "line", "crossregiondetection": "zone",
    "smartmotionhuman": "human", "smartmotionvehicle": "vehicle", "videoblind": "tamper",
    "alarmlocal": "input", "intrusion": "zone", "tripwire": "line",
    # ONVIF topic tails
    "cellmotiondetector": "motion", "motion": "motion", "linedetector": "line", "fielddetector": "zone",
    "peopledetect": "human", "humandetect": "human", "vehicledetect": "vehicle", "tampering": "tamper",
    "digitalinput": "input",
    # generic
    "human": "human", "person": "human", "vehicle": "vehicle", "car": "vehicle", "line": "line",
    "zone": "zone", "tamper": "tamper", "input": "input",
}


def normalize_type(raw_type, target=None):
    t = RAW_MAP.get(re.sub(r"[^a-z]", "", str(raw_type or "").lower().split("/")[-1]), None)
    if t is None:
        for k, v in RAW_MAP.items():
            if k in str(raw_type or "").lower().replace("_", ""):
                t = v
                break
    t = t or "other"
    tg = str(target or "").lower()
    if t in ("line", "zone", "motion", "other"):
        if tg in ("human", "person", "people"):
            t = "human"
        elif tg in ("vehicle", "car"):
            t = "vehicle"
    return t


def type_label(t):
    return TYPES.get(t, TYPES["other"])[0]


# ---------------------------------------------------------------- live bus (SSE)
_subs = []
_subs_lock = threading.Lock()


def subscribe():
    qq = queue.Queue(maxsize=500)
    with _subs_lock:
        _subs.append(qq)
    return qq


def unsubscribe(qq):
    with _subs_lock:
        if qq in _subs:
            _subs.remove(qq)


def publish(kind, org_id=None, **data):
    msg = {"kind": kind, "org_id": org_id, **data}
    with _subs_lock:
        for qq in list(_subs):
            try:
                qq.put_nowait(msg)
            except queue.Full:
                pass


# ---------------------------------------------------------------- auth
def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def check_pw(pw, stored):
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex(), h)


def new_session(user_id):
    tok = secrets.token_urlsafe(32)
    db.ex("INSERT INTO sessions(token,user_id,expires) VALUES(?,?,?)", (tok, user_id, time.time() + 12 * 3600))
    return tok


def user_by_session(tok):
    if not tok:
        return None
    s = db.q1("SELECT * FROM sessions WHERE token=? AND expires>?", (tok, time.time()))
    if not s:
        return None
    return db.q1("SELECT * FROM users WHERE id=? AND active=1", (s["user_id"],))


def user_by_api_token(tok):
    if not tok:
        return None
    return db.q1("SELECT * FROM users WHERE api_token=? AND active=1", (tok,))


def visible_org_ids(user):
    """Customers see only their organisation (and its children). Others see everything."""
    if user["role"] != "customer":
        return None
    ids = {user["org_id"]}
    for o in db.q("SELECT id FROM orgs WHERE parent_id=?", (user["org_id"],)):
        ids.add(o["id"])
    return ids


def audit(who, action, detail=""):
    db.insert("audit", {"ts": time.time(), "who": who, "action": action, "detail": detail})


# ---------------------------------------------------------------- geo
def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- arming
def is_armed(org, at=None):
    mode = org.get("arming_mode") or "schedule"
    if mode == "armed":
        return True
    if mode == "disarmed":
        return False
    sched = org.get("schedule") or []
    if not sched:
        return True
    now = at or datetime.now()
    day = now.isoweekday()           # 1=Mon..7=Sun
    prev_day = 7 if day == 1 else day - 1
    hm = now.hour * 60 + now.minute
    for s in sched:
        try:
            f = int(s["from"][:2]) * 60 + int(s["from"][3:5])
            t = int(s["to"][:2]) * 60 + int(s["to"][3:5])
        except (KeyError, ValueError):
            continue
        days = s.get("days") or [1, 2, 3, 4, 5, 6, 7]
        if f <= t:
            if day in days and f <= hm < t:
                return True
        else:  # overnight, e.g. 22:00-07:00
            if (day in days and hm >= f) or (prev_day in days and hm < t):
                return True
    return False


# ---------------------------------------------------------------- incidents
def add_timeline(incident_id, text, who="σύστημα", kind="info"):
    db.insert("timeline", {"incident_id": incident_id, "ts": time.time(), "who": who, "kind": kind, "text": text})
    publish("incident", org_id=None, id=incident_id)


def save_image(b64_or_bytes, ext="jpg"):
    data = base64.b64decode(b64_or_bytes) if isinstance(b64_or_bytes, str) else b64_or_bytes
    if not data:
        return None
    name = f"{int(time.time()*1000)}_{secrets.token_hex(4)}.{ext}"
    with open(os.path.join(db.MEDIA, name), "wb") as f:
        f.write(data)
    return name


def get_or_create_camera(org_id, ext_key, name=None, edge_id=None):
    cam = db.q1("SELECT * FROM cameras WHERE org_id=? AND ext_key=?", (org_id, ext_key))
    if cam:
        return cam
    cid = db.insert("cameras", {"org_id": org_id, "edge_id": edge_id, "ext_key": ext_key,
                                "name": name or ext_key, "last_seen": time.time()})
    return db.q1("SELECT * FROM cameras WHERE id=?", (cid,))


def ingest(org_id, camera_key, raw_type, target=None, state="active", camera_name=None, image=None,
           image_ext="jpg", raw=None, source="webhook", edge_id=None, ts=None, ignore_arming=False):
    """Main entry for every camera signal. Returns dict with outcome."""
    org = db.q1("SELECT * FROM orgs WHERE id=?", (org_id,))
    if not org:
        return {"ok": False, "error": "unknown organisation"}
    cam = get_or_create_camera(org_id, camera_key, camera_name, edge_id)
    now = ts or time.time()
    db.update("cameras", cam["id"], {"last_seen": now, "online": 1})
    etype = normalize_type(raw_type, target)
    st = str(state or "active").lower()
    img = save_image(image, image_ext) if image else None

    ev = {"ts": now, "org_id": org_id, "camera_id": cam["id"], "type": etype, "raw_type": str(raw_type or "")[:80],
          "target": target, "state": st, "image": img, "raw": (raw or "")[:4000], "source": source}

    reason = None
    if st in ("inactive", "stop", "false", "0"):
        reason = "τέλος συμβάντος"
    elif not cam["enabled"]:
        reason = "κάμερα απενεργοποιημένη"
    elif cam["snooze_until"] and cam["snooze_until"] > now:
        reason = "κάμερα σε παύση"
    elif etype not in TECHNICAL and etype != "tamper" and not ignore_arming and not is_armed(org):
        reason = "αφόπλιστο (εκτός ωραρίου)"
    elif org["allowed_types"] and etype not in org["allowed_types"] and etype not in TECHNICAL:
        reason = "τύπος φιλτραρισμένος"
    if reason:
        ev["suppressed"] = reason
        db.insert("events", ev)
        return {"ok": True, "suppressed": reason}

    label, prio = TYPES.get(etype, TYPES["other"])
    with db.write_lock:
        inc = db.q1("SELECT * FROM incidents WHERE org_id=? AND status!='closed' AND last_ts>? "
                    "ORDER BY last_ts DESC LIMIT 1", (org_id, now - MERGE_SECONDS))
        if inc and (etype in TECHNICAL) == (inc["type"] in TECHNICAL):
            cams = inc["cameras"]
            if cam["id"] not in cams:
                cams.append(cam["id"])
            upd = {"last_ts": now, "count": inc["count"] + 1, "cameras": cams}
            if prio < inc["priority"]:
                upd.update(priority=prio, type=etype)
            db.update("incidents", inc["id"], upd)
            ev["incident_id"] = inc["id"]
            db.insert("events", ev)
            add_timeline(inc["id"], f"Νέο σήμα: {label} · {cam['name']}", kind="signal")
            publish("incident", org_id=org_id, id=inc["id"], merged=True)
            return {"ok": True, "incident_id": inc["id"], "merged": True}

        iid = db.insert("incidents", {"org_id": org_id, "camera_id": cam["id"], "status": "new", "priority": prio,
                                      "type": etype, "first_ts": now, "last_ts": now, "cameras": [cam["id"]]})
        ev["incident_id"] = iid
        db.insert("events", ev)
    add_timeline(iid, f"Συναγερμός: {label} · {cam['name']}", kind="alarm")
    publish("incident", org_id=org_id, id=iid, new=True, priority=prio)
    print(f"[ALARM] #{iid} {org['name']} / {cam['name']} / {label}")
    return {"ok": True, "incident_id": iid}


def incident_view(iid):
    inc = db.q1("SELECT * FROM incidents WHERE id=?", (iid,))
    if not inc:
        return None
    org = db.q1("SELECT * FROM orgs WHERE id=?", (inc["org_id"],))
    cam = db.q1("SELECT * FROM cameras WHERE id=?", (inc["camera_id"],)) if inc["camera_id"] else None
    cams = [db.q1("SELECT id,name,lat,lon,online FROM cameras WHERE id=?", (c,)) for c in inc["cameras"]]
    images = [e["image"] for e in db.q("SELECT image FROM events WHERE incident_id=? AND image IS NOT NULL ORDER BY ts", (iid,))]
    user = db.q1("SELECT name FROM users WHERE id=?", (inc["assigned_user"],)) if inc["assigned_user"] else None
    m = db.q1("SELECT * FROM missions WHERE incident_id=? ORDER BY id DESC LIMIT 1", (iid,))
    return {
        **inc, "type_label": type_label(inc["type"]),
        "org": {k: org[k] for k in ("id", "name", "address", "lat", "lon", "sop", "speaker_presets", "notes", "arming_mode")} if org else None,
        "camera": cam and {k: cam[k] for k in ("id", "name", "lat", "lon", "ext_key")},
        "camera_list": [c for c in cams if c],
        "images": images[-12:],
        "assigned_name": user["name"] if user else None,
        "contacts": db.q("SELECT * FROM contacts WHERE org_id=? ORDER BY priority", (inc["org_id"],)),
        "timeline": db.q("SELECT ts,who,kind,text FROM timeline WHERE incident_id=? ORDER BY ts, id", (iid,)),
        "notifications": db.q("SELECT * FROM notifications WHERE incident_id=? ORDER BY ts", (iid,)),
        "mission": mission_view(m) if m else None,
        "age": int(time.time() - inc["first_ts"]),
    }


def incident_list(org_ids=None, include_closed=40):
    rows = db.q("SELECT i.*, o.name AS org_name, c.name AS camera_name FROM incidents i "
                "JOIN orgs o ON o.id=i.org_id LEFT JOIN cameras c ON c.id=i.camera_id "
                "WHERE i.status!='closed' ORDER BY i.priority, i.first_ts")
    closed = db.q("SELECT i.*, o.name AS org_name, c.name AS camera_name FROM incidents i "
                  "JOIN orgs o ON o.id=i.org_id LEFT JOIN cameras c ON c.id=i.camera_id "
                  "WHERE i.status='closed' ORDER BY i.closed_ts DESC LIMIT ?", (include_closed,))
    out = []
    for r in rows + closed:
        if org_ids is not None and r["org_id"] not in org_ids:
            continue
        img = db.q1("SELECT image FROM events WHERE incident_id=? AND image IS NOT NULL ORDER BY ts DESC LIMIT 1", (r["id"],))
        u = db.q1("SELECT name FROM users WHERE id=?", (r["assigned_user"],)) if r["assigned_user"] else None
        m = db.q1("SELECT id FROM missions WHERE incident_id=? AND landed_ts IS NULL", (r["id"],))
        out.append({**r, "type_label": type_label(r["type"]), "thumb": img["image"] if img else None,
                    "assigned_name": u["name"] if u else None, "flying": bool(m)})
    return out


# ---------------------------------------------------------------- drones
class DroneError(Exception):
    pass


class SimAdapter:
    """Simulated dock. All state lives in the missions table."""
    name = "Προσομοίωση"

    def dispatch(self, dock, lat, lon): pass
    def return_home(self, dock): pass
    def speaker_tts(self, dock, text, volume): pass
    def speaker_audio(self, dock, path, volume): pass
    def light(self, dock, on): pass
    def camera(self, dock, mode): pass


class DjiCloudAdapter:
    """
    Placeholder for the real DJI Dock connection (DJI Cloud API, MQTT + HTTPS).
    Mapping to implement when a Dock is available:
      dispatch     -> fly_to_point (Live Flight Controls) or flighttask_prepare/flighttask_execute (wayline)
      return_home  -> return_home
      speaker_tts  -> drc_speaker_tts_play_start (+ drc_speaker_play_volume_set)
      speaker_audio-> speaker audio play / real-time broadcast mode (Dock 3 + AS1)
      light        -> AL1 searchlight control
      camera       -> camera_mode_switch / live_lens_change (wide, zoom, ir)
      live video   -> live_start_push (RTMP/WebRTC) to the media server
    """
    name = "DJI Cloud API"

    def _no(self, *a, **k):
        raise DroneError("Η σύνδεση με πραγματικό DJI Dock δεν έχει ενεργοποιηθεί ακόμα (DJI Cloud API).")

    dispatch = return_home = speaker_tts = speaker_audio = light = camera = _no


ADAPTERS = {"sim": SimAdapter(), "dji": DjiCloudAdapter()}


def adapter_for(dock):
    return ADAPTERS.get(dock["adapter"], ADAPTERS["sim"])


def mission_view(m):
    if not m:
        return None
    dock = db.q1("SELECT * FROM docks WHERE id=?", (m["dock_id"],))
    t = time.time()
    el = (t - m["start_ts"]) * SIM_SPEED
    flight = max(10.0, (m["distance_m"] or 0) / CRUISE_MS)
    if m["return_ts"] is None:
        if el < TAKEOFF_S:
            phase, p = "takeoff", 0.0
        elif el < TAKEOFF_S + flight:
            phase, p = "enroute", (el - TAKEOFF_S) / flight
        else:
            phase, p = "onsite", 1.0
        eta = max(0, int((TAKEOFF_S + flight - el) / SIM_SPEED))
    else:
        tr = (t - m["return_ts"]) * SIM_SPEED
        dur = max(10.0, flight * m["p_ret"])
        if tr < dur:
            phase, p = "returning", m["p_ret"] * (1 - tr / dur)
        else:
            phase, p = "landed", 0.0
        eta = max(0, int((dur - tr) / SIM_SPEED)) if phase == "returning" else 0
    if m["landed_ts"]:
        phase, p = "landed", 0.0
    end = m["landed_ts"] or t
    battery = max(15, int(100 - (end - m["start_ts"]) * SIM_SPEED * 0.08))
    lat = dock["lat"] + (m["tlat"] - dock["lat"]) * p
    lon = dock["lon"] + (m["tlon"] - dock["lon"]) * p
    alt = 0 if phase == "landed" else (int(40 * min(1, el / TAKEOFF_S)) if phase == "takeoff" else 40)
    return {**m, "phase": phase, "progress": round(p, 4), "lat": lat, "lon": lon, "alt": alt,
            "battery": battery, "eta": eta, "elapsed": int(end - m["start_ts"]),
            "dock": {k: dock[k] for k in ("id", "name", "lat", "lon", "adapter", "has_speaker", "has_light")},
            "simulated": dock["adapter"] == "sim"}


def busy_dock_ids():
    return {r["dock_id"] for r in db.q("SELECT dock_id FROM missions WHERE landed_ts IS NULL")}


def dock_options(lat, lon):
    busy = busy_dock_ids()
    out = []
    for d in db.q("SELECT * FROM docks WHERE enabled=1"):
        dist = haversine_m(d["lat"], d["lon"], lat, lon)
        out.append({**d, "distance_m": int(dist), "in_range": dist <= d["radius_km"] * 1000,
                    "available": d["id"] not in busy, "eta_s": int((TAKEOFF_S + dist / CRUISE_MS) / SIM_SPEED)})
    out.sort(key=lambda x: (not (x["available"] and x["in_range"]), x["distance_m"]))
    return out


def target_of(inc):
    cam = db.q1("SELECT * FROM cameras WHERE id=?", (inc["camera_id"],)) if inc["camera_id"] else None
    org = db.q1("SELECT * FROM orgs WHERE id=?", (inc["org_id"],))
    if cam and cam["lat"] is not None:
        return cam["lat"], cam["lon"]
    if org and org["lat"] is not None:
        return org["lat"], org["lon"]
    return None


PHASES = {"takeoff": "Απογείωση", "enroute": "Το drone απογειώθηκε, σε πορεία", "onsite": "Το drone έφτασε στο σημείο",
          "returning": "Επιστροφή στο Dock", "landed": "Το drone προσγειώθηκε στο Dock"}


# ---------------------------------------------------------------- notifications
def render(tpl, inc):
    org = inc["org"] or {}
    cam = inc["camera"] or {}
    return (tpl.replace("{org}", org.get("name", "")).replace("{address}", org.get("address", ""))
            .replace("{camera}", cam.get("name", "")).replace("{type}", inc["type_label"])
            .replace("{time}", datetime.fromtimestamp(inc["first_ts"]).strftime("%H:%M"))
            .replace("{id}", str(inc["id"])))


def send_sms(phone, text):
    cfg = db.setting("sms", {}) or {}
    if not cfg.get("url"):
        return "simulated", "Δεν έχει ρυθμιστεί πάροχος SMS"
    body = json.dumps(cfg.get("body", {"to": "{to}", "text": "{text}"}), ensure_ascii=False)
    body = body.replace("{to}", phone).replace("{text}", json.dumps(text, ensure_ascii=False)[1:-1])
    if cfg.get("sender"):
        body = body.replace("{sender}", cfg["sender"])
    req = urllib.request.Request(cfg["url"], data=body.encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(cfg.get("headers") or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return ("sent", None) if r.status < 300 else ("failed", f"HTTP {r.status}")
    except Exception as e:  # noqa: BLE001
        return "failed", str(e)[:200]


def send_email(to, subject, text):
    cfg = db.setting("smtp", {}) or {}
    if not cfg.get("host"):
        return "simulated", "Δεν έχει ρυθμιστεί SMTP"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg.get("from", cfg.get("user", "")), to, subject
    msg.set_content(text)
    try:
        with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587)), timeout=20) as s:
            if cfg.get("tls", True):
                s.starttls()
            if cfg.get("user"):
                s.login(cfg["user"], cfg.get("password", ""))
            s.send_message(msg)
        return "sent", None
    except Exception as e:  # noqa: BLE001
        return "failed", str(e)[:200]


def notify(iid, channel, target, message, who):
    if channel == "sms":
        status, err = send_sms(target, message)
    elif channel == "email":
        status, err = send_email(target, f"Συναγερμός #{iid}", message)
    else:  # voice / police: performed by the operator, we log the outcome
        status, err = "logged", None
    db.insert("notifications", {"incident_id": iid, "ts": time.time(), "channel": channel, "target": target,
                                "message": message, "status": status, "error": err, "who": who})
    label = {"sms": "SMS", "email": "Email", "voice": "Κλήση", "police": "Αστυνομία"}.get(channel, channel)
    extra = {"simulated": " (προσομοίωση: δεν έχει ρυθμιστεί πάροχος)", "failed": f" (απέτυχε: {err})"}.get(status, "")
    add_timeline(iid, f"{label} → {target}{extra}", who, kind="notify")
    return status, err


def police_summary(inc):
    org = inc["org"] or {}
    cam = inc["camera"] or {}
    lat, lon = (cam.get("lat") or org.get("lat")), (cam.get("lon") or org.get("lon"))
    lines = [
        f"Συναγερμός σε επιχείρηση: {org.get('name', '')}",
        f"Διεύθυνση: {org.get('address', '') or '—'}",
        f"Συντεταγμένες: {lat:.5f}, {lon:.5f}" if lat is not None else "Συντεταγμένες: —",
        f"Συμβάν: {inc['type_label']} από κάμερα «{cam.get('name', '—')}» στις {datetime.fromtimestamp(inc['first_ts']).strftime('%H:%M')}",
    ]
    m = inc.get("mission")
    if m and m["phase"] in ("onsite", "enroute"):
        lines.append("Drone επιτήρησης βρίσκεται στο σημείο με ζωντανή εικόνα." if m["phase"] == "onsite"
                     else "Drone επιτήρησης κατευθύνεται στο σημείο.")
    lines.append(f"Αριθμός περιστατικού: #{inc['id']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- background ticker
def ticker():
    while True:
        time.sleep(1)
        try:
            tick()
        except Exception as e:  # noqa: BLE001
            print("[ticker]", e)


def tick():
    now = time.time()
    # mission phase changes
    for m in db.q("SELECT * FROM missions WHERE landed_ts IS NULL"):
        v = mission_view(m)
        if v["phase"] != m["last_phase"]:
            upd = {"last_phase": v["phase"]}
            if v["phase"] == "landed":
                upd["landed_ts"] = now
            db.update("missions", m["id"], upd)
            if v["phase"] in ("enroute", "onsite", "landed"):
                add_timeline(m["incident_id"], PHASES[v["phase"]], kind="drone")
        publish("mission", id=m["id"], incident_id=m["incident_id"])
    # escalation of unacknowledged incidents
    for inc in db.q("SELECT i.*, o.ack_timeout FROM incidents i JOIN orgs o ON o.id=i.org_id "
                    "WHERE i.status='new' AND i.escalated=0"):
        if now - inc["first_ts"] > (inc["ack_timeout"] or 60):
            db.update("incidents", inc["id"], {"escalated": 1, "priority": max(1, inc["priority"] - 1)})
            add_timeline(inc["id"], f"Κλιμάκωση: χωρίς ανάληψη για {inc['ack_timeout']}″, ειδοποίηση επόπτη", kind="escalate")
            publish("escalation", org_id=inc["org_id"], id=inc["id"])
    # camera health (only cameras managed by an edge box report heartbeats)
    for c in db.q("SELECT * FROM cameras WHERE edge_id IS NOT NULL AND online=1 AND last_seen<?", (now - CAMERA_OFFLINE_AFTER,)):
        db.update("cameras", c["id"], {"online": 0})
        publish("camera", org_id=c["org_id"], id=c["id"], online=False)
