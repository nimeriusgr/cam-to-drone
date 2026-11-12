"""v1.1 features: shifts, statistics, pre-flight checks (weather + no-fly zones), push notifications,
live camera frames, alarm clips and remote updates of the edge agent."""
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.request
from datetime import datetime

import db
import logic
import webpush

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Athens")
except Exception:  # noqa: BLE001  (Windows without tzdata: use the computer's local time)
    TZ = None

STAFF = ("admin", "supervisor", "operator")


def local(ts):
    return datetime.fromtimestamp(ts, TZ) if TZ else datetime.fromtimestamp(ts)


# ================================================================== shifts
def shift_state(user):
    now = time.time()
    mine = db.q1("SELECT * FROM shifts WHERE user_id=? AND end_ts IS NULL ORDER BY id DESC LIMIT 1", (user["id"],))
    last = db.q1("SELECT s.end_ts, s.handover, u.name FROM shifts s JOIN users u ON u.id=s.user_id "
                 "WHERE s.end_ts IS NOT NULL AND s.handover!='' AND s.end_ts>? ORDER BY s.end_ts DESC LIMIT 1", (now - 86400,))
    my_open = db.q1("SELECT COUNT(*) AS n FROM incidents WHERE assigned_user=? AND status!='closed'", (user["id"],))["n"]
    return {"current": mine, "on_duty": on_duty(), "my_open": my_open,
            "last_handover": {"who": last["name"], "ts": last["end_ts"], "text": last["handover"]} if last else None}


def on_duty():
    return db.q("SELECT s.id AS shift_id, s.start_ts, u.id, u.name, u.role, "
                "(SELECT COUNT(*) FROM incidents i WHERE i.assigned_user=u.id AND i.status!='closed') AS open_n "
                "FROM shifts s JOIN users u ON u.id=s.user_id WHERE s.end_ts IS NULL AND u.active=1 ORDER BY s.start_ts")


def shift_start(user):
    cur = db.q1("SELECT * FROM shifts WHERE user_id=? AND end_ts IS NULL", (user["id"],))
    if not cur:
        db.insert("shifts", {"user_id": user["id"], "start_ts": time.time()})
        logic.audit(user["name"], "shift.start")
        logic.publish("shift")
    return shift_state(user)


def shift_end(user, note="", release=False):
    cur = db.q1("SELECT * FROM shifts WHERE user_id=? AND end_ts IS NULL", (user["id"],))
    if cur:
        db.update("shifts", cur["id"], {"end_ts": time.time(), "handover": (note or "").strip()[:4000]})
        if release:
            for inc in db.q("SELECT id FROM incidents WHERE assigned_user=? AND status!='closed'", (user["id"],)):
                db.update("incidents", inc["id"], {"assigned_user": None})
                logic.add_timeline(inc["id"], "Αποδέσμευση: τέλος βάρδιας ελεγκτή", user["name"], "assign")
        logic.audit(user["name"], "shift.end")
        logic.publish("shift")
    return shift_state(user)


# ================================================================== statistics
def _pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(math.ceil(p / 100 * len(v))) - 1)]


def _avg(values):
    return round(sum(values) / len(values), 1) if values else None


FALSE = "Ψευδής συναγερμός"
TECH = "Τεχνικό πρόβλημα"


def stats(vis, frm, to, org_id=None, staff=True):
    args = [frm, to]
    where = "i.first_ts>=? AND i.first_ts<?"
    if org_id:
        where += " AND i.org_id=?"
        args.append(org_id)
    rows = db.q(f"SELECT i.*, o.name AS org_name, u.name AS op_name, "
                f"(SELECT MIN(m.start_ts) FROM missions m WHERE m.incident_id=i.id) AS drone_ts, "
                f"(SELECT MIN(m.onsite_ts) FROM missions m WHERE m.incident_id=i.id) AS onsite_ts "
                f"FROM incidents i JOIN orgs o ON o.id=i.org_id LEFT JOIN users u ON u.id=i.assigned_user WHERE {where}", args)
    rows = [r for r in rows if vis is None or r["org_id"] in vis]
    return build_stats(rows, frm, to, _signals(vis, frm, to, org_id), staff)


def _signals(vis, frm, to, org_id):
    rows = db.q("SELECT org_id, suppressed FROM events WHERE ts>=? AND ts<?" + (" AND org_id=?" if org_id else ""),
                (frm, to, org_id) if org_id else (frm, to))
    rows = [r for r in rows if vis is None or r["org_id"] in vis]
    return {"total": len(rows), "suppressed": sum(1 for r in rows if r["suppressed"])}


def build_stats(rows, frm, to, signals, staff):
    """Same output as the Cloudflare version (see cloudflare/lib/app.js buildStats)."""
    ack = [r["acked_ts"] - r["first_ts"] for r in rows if r["acked_ts"]]
    onsite = [r["onsite_ts"] - r["drone_ts"] for r in rows if r["onsite_ts"] and r["drone_ts"]]
    handle = [r["closed_ts"] - r["first_ts"] for r in rows if r["closed_ts"]]
    closed = [r for r in rows if r["status"] == "closed"]
    false_n = sum(1 for r in closed if r["outcome"] == FALSE)
    tech_n = sum(1 for r in rows if r["type"] in logic.TECHNICAL or r["outcome"] == TECH)
    days, d = [], local(frm).replace(hour=0, minute=0, second=0, microsecond=0)
    end = local(to)
    while d < end and len(days) < 400:
        days.append(d.strftime("%Y-%m-%d"))
        d = local(d.timestamp() + 86400 + 3600).replace(hour=0, minute=0, second=0, microsecond=0)
    by_day = {k: {"day": k, "incidents": 0, "false_alarms": 0, "drone": 0} for k in days}
    by_hour = [0] * 24
    by_type, by_outcome, by_org, by_op = {}, {}, {}, {}
    for r in rows:
        lt = local(r["first_ts"])
        k = lt.strftime("%Y-%m-%d")
        if k in by_day:
            by_day[k]["incidents"] += 1
            by_day[k]["false_alarms"] += r["outcome"] == FALSE
            by_day[k]["drone"] += bool(r["drone_ts"])
        by_hour[lt.hour] += 1
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        if r["status"] == "closed":
            by_outcome[r["outcome"] or "Άλλο"] = by_outcome.get(r["outcome"] or "Άλλο", 0) + 1
        o = by_org.setdefault(r["org_id"], {"org_id": r["org_id"], "name": r["org_name"], "incidents": 0, "false_alarms": 0,
                                            "closed": 0, "drone": 0, "_ack": []})
        o["incidents"] += 1
        o["closed"] += r["status"] == "closed"
        o["false_alarms"] += r["outcome"] == FALSE
        o["drone"] += bool(r["drone_ts"])
        if r["acked_ts"]:
            o["_ack"].append(r["acked_ts"] - r["first_ts"])
        if r["op_name"]:
            p = by_op.setdefault(r["op_name"], {"name": r["op_name"], "handled": 0, "_ack": [], "drone": 0})
            p["handled"] += 1
            p["drone"] += bool(r["drone_ts"])
            if r["acked_ts"]:
                p["_ack"].append(r["acked_ts"] - r["first_ts"])
    orgs = []
    for o in by_org.values():
        o["avg_ack_s"] = _avg(o.pop("_ack"))
        o["false_rate"] = round(o["false_alarms"] / o["closed"], 3) if o["closed"] else None
        orgs.append(o)
    ops = []
    for p in by_op.values():
        p["avg_ack_s"] = _avg(p.pop("_ack"))
        ops.append(p)
    out = {
        "from": frm, "to": to,
        "totals": {"incidents": len(rows), "closed": len(closed), "open": len(rows) - len(closed), "false_alarms": false_n,
                   "technical": tech_n, "real": len(closed) - false_n - sum(1 for r in closed if r["outcome"] == TECH or r["type"] in logic.TECHNICAL),
                   "drone_missions": sum(1 for r in rows if r["drone_ts"]), "escalated": sum(1 for r in rows if r["escalated"]),
                   "avg_ack_s": _avg(ack), "p90_ack_s": _pct(ack, 90), "avg_onsite_s": _avg(onsite), "avg_handle_s": _avg(handle),
                   "false_rate": round(false_n / len(closed), 3) if closed else None},
        "signals": signals,
        "by_day": list(by_day.values()), "by_hour": by_hour,
        "by_type": sorted([{"type": k, "label": logic.type_label(k), "count": v} for k, v in by_type.items()], key=lambda x: -x["count"]),
        "by_outcome": sorted([{"outcome": k, "count": v} for k, v in by_outcome.items()], key=lambda x: -x["count"]),
        "by_org": sorted(orgs, key=lambda x: -x["incidents"]),
    }
    if staff:
        out["by_operator"] = sorted(ops, key=lambda x: -x["handled"])
    return out


def incidents_csv(vis, frm, to, org_id=None):
    rows = db.q("SELECT i.*, o.name AS org_name, c.name AS cam, u.name AS op, "
                "(SELECT MIN(m.start_ts) FROM missions m WHERE m.incident_id=i.id) AS drone_ts "
                "FROM incidents i JOIN orgs o ON o.id=i.org_id LEFT JOIN cameras c ON c.id=i.camera_id "
                "LEFT JOIN users u ON u.id=i.assigned_user WHERE i.first_ts>=? AND i.first_ts<?" + (" AND i.org_id=?" if org_id else "")
                + " ORDER BY i.first_ts", (frm, to, org_id) if org_id else (frm, to))
    rows = [r for r in rows if vis is None or r["org_id"] in vis]
    return csv_text(rows)


def csv_text(rows):
    f = lambda t: local(t).strftime("%d/%m/%Y %H:%M:%S") if t else ""  # noqa: E731
    q = lambda v: '"' + str(v if v is not None else "").replace('"', '""') + '"'  # noqa: E731
    head = ["Αρ.", "Επιχείρηση", "Κάμερα", "Τύπος", "Έναρξη", "Ανάληψη σε (δευτ.)", "Drone", "Κλείσιμο", "Αποτέλεσμα", "Ελεγκτής", "Σήματα", "Σημείωση"]
    lines = [";".join(q(h) for h in head)]
    for r in rows:
        lines.append(";".join(q(v) for v in [r["id"], r["org_name"], r["cam"], logic.type_label(r["type"]), f(r["first_ts"]),
                                              int(r["acked_ts"] - r["first_ts"]) if r["acked_ts"] else "", "Ναι" if r["drone_ts"] else "",
                                              f(r["closed_ts"]), r["outcome"], r["op"], r["count"], r["note"]]))
    return "﻿" + "\r\n".join(lines) + "\r\n"


# ================================================================== pre-flight checks
FLIGHT_DEFAULTS = {"max_wind": 12.0, "max_gust": 15.0, "max_precip": 0.5, "min_temp": -20.0, "max_temp": 50.0}
WEATHER_CODES = {0: "Αίθριος", 1: "Κυρίως αίθριος", 2: "Μερική νέφωση", 3: "Συννεφιά", 45: "Ομίχλη", 48: "Ομίχλη με πάχνη",
                 51: "Ψιχάλα", 53: "Ψιχάλα", 55: "Πυκνή ψιχάλα", 56: "Παγωμένη ψιχάλα", 57: "Παγωμένη ψιχάλα", 61: "Βροχή",
                 63: "Βροχή", 65: "Δυνατή βροχή", 66: "Παγωμένη βροχή", 67: "Παγωμένη βροχή", 71: "Χιόνι", 73: "Χιόνι",
                 75: "Πυκνό χιόνι", 77: "Χιονόκοκκοι", 80: "Μπόρες", 81: "Μπόρες", 82: "Ισχυρές μπόρες", 85: "Χιονοπτώσεις",
                 86: "Χιονοπτώσεις", 95: "Καταιγίδα", 96: "Καταιγίδα με χαλάζι", 99: "Καταιγίδα με χαλάζι"}
_wx_cache, _wx_lock = {}, threading.Lock()


def flight_limits():
    return {**FLIGHT_DEFAULTS, **(db.setting("flight", {}) or {})}


def weather(lat, lon):
    key = (round(lat, 2), round(lon, 2))
    with _wx_lock:
        hit = _wx_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f&wind_speed_unit=ms&timezone=auto"
           "&current=temperature_2m,precipitation,weather_code,wind_speed_10m,wind_gusts_10m,visibility" % (lat, lon))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "cam2drone"}), timeout=6) as r:
            cur = json.loads(r.read()).get("current") or {}
        w = {"wind": cur.get("wind_speed_10m"), "gust": cur.get("wind_gusts_10m"), "precip": cur.get("precipitation"),
             "temp": cur.get("temperature_2m"), "code": cur.get("weather_code"), "visibility": cur.get("visibility"),
             "desc": WEATHER_CODES.get(cur.get("weather_code"), ""), "ts": time.time(), "source": "Open-Meteo"}
    except Exception:  # noqa: BLE001
        w = None
    with _wx_lock:
        _wx_cache[key] = (time.time() if w else time.time() - 540, w)
    return w


def weather_issues(w, lim):
    """Same rules as weatherIssues() in cloudflare/lib/app.js."""
    if not w:
        return [{"level": "warn", "text": "Δεν υπάρχουν δεδομένα καιρού. Ελέγξτε τις συνθήκες πριν την πτήση."}]
    out = []
    if w["wind"] is not None:
        if w["wind"] > lim["max_wind"]:
            out.append({"level": "block", "text": f"Άνεμος {w['wind']:.1f} m/s (όριο {lim['max_wind']:g})"})
        elif w["wind"] > lim["max_wind"] * 0.8:
            out.append({"level": "warn", "text": f"Άνεμος {w['wind']:.1f} m/s, κοντά στο όριο"})
    if w["gust"] is not None and w["gust"] > lim["max_gust"]:
        out.append({"level": "block", "text": f"Ριπές {w['gust']:.1f} m/s (όριο {lim['max_gust']:g})"})
    if w["precip"] is not None:
        if w["precip"] > lim["max_precip"]:
            out.append({"level": "block", "text": f"Βροχόπτωση {w['precip']:.1f} mm (όριο {lim['max_precip']:g})"})
        elif w["precip"] > 0:
            out.append({"level": "warn", "text": f"Ελαφριά βροχόπτωση {w['precip']:.1f} mm"})
    if w["temp"] is not None and not (lim["min_temp"] <= w["temp"] <= lim["max_temp"]):
        out.append({"level": "block", "text": f"Θερμοκρασία {w['temp']:.0f}°C εκτός ορίων"})
    if w["code"] in (95, 96, 99):
        out.append({"level": "block", "text": "Καταιγίδα στην περιοχή"})
    elif w["code"] in (45, 48):
        out.append({"level": "warn", "text": "Ομίχλη: περιορισμένη ορατότητα"})
    if w["visibility"] is not None and w["visibility"] < 1000:
        out.append({"level": "warn", "text": f"Ορατότητα {int(w['visibility'])} m"})
    return out


def _seg_dist_m(lat1, lon1, lat2, lon2, plat, plon):
    """Distance from point P to segment A-B, metres (local flat projection, fine for a few km)."""
    k = 111320.0
    c = math.cos(math.radians(plat))
    ax, ay, bx, by = (lon1 - plon) * k * c, (lat1 - plat) * k, (lon2 - plon) * k * c, (lat2 - plat) * k
    dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def zone_issues(dock, lat, lon):
    out = []
    for z in db.q("SELECT * FROM nofly WHERE enabled=1"):
        lvl = "block" if z["level"] == "block" else "warn"
        if logic.haversine_m(z["lat"], z["lon"], lat, lon) <= z["radius_m"]:
            out.append({"level": lvl, "text": f"Ο στόχος είναι μέσα στη ζώνη «{z['name']}»", "zone_id": z["id"]})
        elif dock and _seg_dist_m(dock["lat"], dock["lon"], lat, lon, z["lat"], z["lon"]) <= z["radius_m"]:
            out.append({"level": lvl, "text": f"Η διαδρομή από {dock['name']} περνά από τη ζώνη «{z['name']}»", "zone_id": z["id"]})
    return out


def level_of(issues):
    return "block" if any(i["level"] == "block" for i in issues) else "warn" if issues else "ok"


def preflight(lat, lon, docks):
    """Weather once for the target, zones per dock. Adds `check` to each dock option."""
    w = weather(lat, lon)
    wi = weather_issues(w, flight_limits())
    for d in docks:
        iss = wi + zone_issues(d, lat, lon)
        d["check"] = {"level": level_of(iss), "issues": iss}
    return {"weather": w, "issues": wi, "level": level_of(wi)}


# ================================================================== push notifications
def push_status(user):
    if not webpush.AVAILABLE:
        return {"enabled": False, "reason": "Ο server χρειάζεται το πακέτο cryptography (pip install cryptography)"}
    n = db.q1("SELECT COUNT(*) AS n FROM push_subs WHERE user_id=?", (user["id"],))["n"]
    return {"enabled": True, "key": webpush.public_key(), "devices": n}


def push_subscribe(user, sub):
    keys = sub.get("keys") or {}
    if not (sub.get("endpoint", "").startswith("https://") and keys.get("p256dh") and keys.get("auth")):
        raise ValueError("Μη έγκυρη συνδρομή")
    db.ex("DELETE FROM push_subs WHERE endpoint=?", (sub["endpoint"],))
    db.insert("push_subs", {"user_id": user["id"], "endpoint": sub["endpoint"], "p256dh": keys["p256dh"], "auth": keys["auth"], "ts": time.time()})


def push_unsubscribe(user, endpoint):
    db.ex("DELETE FROM push_subs WHERE user_id=? AND endpoint=?", (user["id"], endpoint))


def recipients(kind, inc):
    """Customers of the organisation (and its parent) for new/onsite/closed; staff on duty (or all staff) for new/escalation."""
    users = []
    if kind in ("new", "onsite", "closed"):
        org = db.q1("SELECT id, parent_id FROM orgs WHERE id=?", (inc["org_id"],))
        ids = [org["id"]] + ([org["parent_id"]] if org and org["parent_id"] else [])
        users += db.q(f"SELECT id FROM users WHERE role='customer' AND active=1 AND org_id IN ({','.join('?' * len(ids))})", ids)
    if kind in ("new", "escalation"):
        duty = db.q("SELECT DISTINCT user_id AS id FROM shifts WHERE end_ts IS NULL")
        users += duty or db.q("SELECT id FROM users WHERE role IN ('admin','supervisor','operator') AND active=1")
    return {u["id"] for u in users}


def push_message(kind, inc):
    org = db.q1("SELECT name FROM orgs WHERE id=?", (inc["org_id"],)) or {"name": ""}
    cam = db.q1("SELECT name FROM cameras WHERE id=?", (inc["camera_id"],)) if inc.get("camera_id") else None
    t = local(inc["first_ts"]).strftime("%H:%M")
    label = logic.type_label(inc["type"])
    title, body = {
        "new": (f"Συναγερμός · {org['name']}", f"{label}{' · ' + cam['name'] if cam else ''} · {t}"),
        "escalation": (f"Χωρίς ανάληψη · {org['name']}", f"Περιστατικό #{inc['id']} ({label}) περιμένει χειρισμό"),
        "onsite": (f"Drone στο σημείο · {org['name']}", f"Το drone επιτήρησης έφτασε για το περιστατικό #{inc['id']}"),
        "closed": (f"Ολοκληρώθηκε · {org['name']}", f"{inc.get('outcome') or 'Έκλεισε'} · περιστατικό #{inc['id']}"),
    }[kind]
    return {"title": title, "body": body, "tag": f"inc-{inc['id']}", "url": f"/#inc={inc['id']}", "kind": kind,
            "urgent": kind in ("new", "escalation")}


SUBJECT = {"value": "https://localhost"}


def push(kind, iid):
    """Fire and forget: runs in a background thread."""
    if not webpush.AVAILABLE:
        return

    def run():
        try:
            inc = db.q1("SELECT * FROM incidents WHERE id=?", (iid,))
            if not inc:
                return
            users = recipients(kind, inc)
            if not users:
                return
            msg = push_message(kind, inc)
            for s in db.q(f"SELECT * FROM push_subs WHERE user_id IN ({','.join('?' * len(users))})", list(users)):
                if webpush.send(s, msg, SUBJECT["value"]) == "gone":
                    db.ex("DELETE FROM push_subs WHERE id=?", (s["id"],))
        except Exception as e:  # noqa: BLE001
            print("[push]", e)
    threading.Thread(target=run, daemon=True).start()


# ================================================================== live frames (near-live camera image through the edge box)
LIVE_SECONDS = 30
_live_req, _frames, _live_lock = {}, {}, threading.Lock()


def live_request(cam_id):
    with _live_lock:
        _live_req[cam_id] = time.time() + LIVE_SECONDS


def live_frame(cam_id):
    with _live_lock:
        return _frames.get(cam_id)


def store_frame(cam_id, data, ctype):
    with _live_lock:
        _frames[cam_id] = (time.time(), data, ctype)


def live_keys(edge_id):
    now = time.time()
    with _live_lock:
        ids = [c for c, until in _live_req.items() if until > now]
    if not ids:
        return []
    return [r["ext_key"] for r in db.q(f"SELECT ext_key FROM cameras WHERE edge_id=? AND id IN ({','.join('?' * len(ids))})", [edge_id] + ids)]


def edge_poll(edge):
    live = live_keys(edge["id"])
    busy = db.q1("SELECT 1 AS x FROM incidents WHERE org_id=? AND status!='closed' LIMIT 1", (edge["org_id"],))
    return {"ok": True, "server_time": time.time(), "fast": bool(busy or live), "live": live,
            "update": bool(edge.get("update_requested")), "agent": agent_info(meta_only=True)}


# ================================================================== alarm clips
def attach_clip(edge, key, ts, data, ctype):
    cam = db.q1("SELECT * FROM cameras WHERE org_id=? AND ext_key=?", (edge["org_id"], key))
    if not cam:
        return None
    ev = db.q1("SELECT * FROM events WHERE camera_id=? AND suppressed IS NULL AND incident_id IS NOT NULL AND ts BETWEEN ? AND ? "
               "ORDER BY ABS(ts-?) LIMIT 1", (cam["id"], ts - 20, ts + 20, ts))
    if not ev:
        return None
    name = logic.save_image(data, "mp4" if "mp4" in (ctype or "mp4") else "webm")
    db.update("events", ev["id"], {"clip": name})
    logic.add_timeline(ev["incident_id"], f"Κλιπ βίντεο συναγερμού · {cam['name']}", "σύστημα", "clip")
    logic.publish("incident", org_id=edge["org_id"], id=ev["incident_id"])
    return {"ok": True, "incident_id": ev["incident_id"], "clip": name}


def clips_of(iid):
    return db.q("SELECT e.clip AS name, e.ts, c.name AS camera FROM events e LEFT JOIN cameras c ON c.id=e.camera_id "
                "WHERE e.incident_id=? AND e.clip IS NOT NULL ORDER BY e.ts", (iid,))


# ================================================================== remote update of the edge agent
AGENT_PATH = os.environ.get("C2D_AGENT", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "edge", "agent.py"))
_agent = {"mtime": None}


def agent_info(meta_only=False):
    try:
        mt = os.path.getmtime(AGENT_PATH)
    except OSError:
        return None
    if _agent["mtime"] != mt:
        with open(AGENT_PATH, "rb") as f:
            data = f.read()
        m = re.search(rb'^VERSION\s*=\s*"([^"]+)"', data, re.M)
        _agent.update(mtime=mt, data=data, version=m.group(1).decode() if m else "?", sha256=hashlib.sha256(data).hexdigest())
    return {"version": _agent["version"], "sha256": _agent["sha256"]} if meta_only else _agent


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])
