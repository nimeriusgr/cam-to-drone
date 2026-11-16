#!/usr/bin/env python3
"""
Cam to Drone · central server.
Run:  python server/app.py [--port 8080]
Only Python 3 standard library (SQLite included).
"""
import argparse
import json
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import queue
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db       # noqa: E402
import features  # noqa: E402
import logic    # noqa: E402
import parsers  # noqa: E402
import seed     # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = "1.1.0"

# ------------------------------------------------------------------ admin schema (what the admin pages may edit)
ADMIN = {
    "orgs": ["parent_id", "kind", "name", "address", "lat", "lon", "notes", "arming_mode", "schedule",
             "allowed_types", "sop", "ack_timeout", "speaker_presets"],
    "contacts": ["org_id", "name", "role", "phone", "email", "priority", "notify_sms", "notify_email"],
    "edges": ["org_id", "name"],
    "cameras": ["org_id", "edge_id", "ext_key", "name", "lat", "lon", "enabled", "snooze_until", "arming_mode"],
    "docks": ["name", "lat", "lon", "radius_km", "adapter", "serial", "has_speaker", "has_light", "enabled"],
    "users": ["username", "name", "role", "org_id", "active"],
    "nofly": ["name", "lat", "lon", "radius_m", "level", "notes", "enabled"],
}
ROLES = {"admin", "supervisor", "operator", "customer"}



UNIQUE_MSG = {
    "users.username": "Το όνομα χρήστη χρησιμοποιείται ήδη από άλλον χρήστη. Διαλέξτε διαφορετικό όνομα χρήστη.",
    "users.api_token": "Το API token συμπίπτει με άλλου χρήστη. Δοκιμάστε ξανά.",
    "cameras.org_id": "Υπάρχει ήδη κάμερα με αυτό το αναγνωριστικό στην ίδια επιχείρηση.",
    "boxes.token": "Το token του κουτιού συμπίπτει με άλλο. Δοκιμάστε ξανά.",
    "orgs.webhook_token": "Το webhook token συμπίπτει με άλλης επιχείρησης. Δοκιμάστε ξανά.",
}


def unique_message(msg):
    """Turn a database integrity error into a message people understand."""
    import re
    m = re.search(r"UNIQUE constraint failed: ([\w.]+)", msg)
    if m:
        return UNIQUE_MSG.get(m.group(1), "Η τιμή που δώσατε υπάρχει ήδη σε άλλη εγγραφή. Αλλάξτε την και δοκιμάστε ξανά.")
    return "Τα στοιχεία δεν είναι έγκυρα (σύγκρουση με άλλη εγγραφή)."


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def need(cond, code, msg):
    if not cond:
        raise ApiError(code, msg)


# ------------------------------------------------------------------ incident actions
def incident_action(iid, user, body):
    who = user["name"]
    a = body.get("action")
    inc = db.q1("SELECT * FROM incidents WHERE id=?", (iid,))
    need(inc, 404, f"Δεν βρέθηκε το περιστατικό #{iid}")
    need(user["role"] != "customer", 403, "Ο ρόλος πελάτη έχει μόνο προβολή")
    m = db.q1("SELECT * FROM missions WHERE incident_id=? AND landed_ts IS NULL", (iid,))
    closed = inc["status"] == "closed"
    if a not in ("note",):
        need(not closed, 409, "Το περιστατικό είναι κλειστό")

    if a == "ack":
        db.update("incidents", iid, {"assigned_user": user["id"], "acked_ts": inc["acked_ts"] or time.time(),
                                     "status": "ack" if inc["status"] == "new" else inc["status"]})
        logic.add_timeline(iid, "Ανάληψη περιστατικού", who, "ack")
    elif a == "assign":
        uid = body.get("user_id")
        if uid:
            target = db.q1("SELECT id, name FROM users WHERE id=? AND active=1 AND role IN ('admin','supervisor','operator')", (int(uid),))
            need(target, 400, "Άγνωστος ελεγκτής")
            db.update("incidents", iid, {"assigned_user": target["id"], "acked_ts": inc["acked_ts"] or time.time(),
                                         "status": "ack" if inc["status"] == "new" else inc["status"]})
            logic.add_timeline(iid, "Ανάθεση σε " + target["name"] if target["id"] != user["id"] else "Ανάληψη περιστατικού", who, "assign")
        else:
            db.update("incidents", iid, {"assigned_user": None})
            logic.add_timeline(iid, "Αποδέσμευση περιστατικού", who, "assign")
    elif a == "sop":
        done = set(inc["sop_done"])
        i = int(body.get("step", -1))
        org = db.q1("SELECT sop FROM orgs WHERE id=?", (inc["org_id"],))
        need(0 <= i < len(org["sop"]), 400, "Άγνωστο βήμα")
        if body.get("done", True):
            done.add(i)
            logic.add_timeline(iid, f"✓ {org['sop'][i]}", who, "sop")
        else:
            done.discard(i)
        db.update("incidents", iid, {"sop_done": sorted(done)})
    elif a == "postpone":
        # drone cannot fly now (weather, zone, equipment): the reason goes to the timeline and the audit log
        need(not m, 409, "Το drone είναι ήδη σε αποστολή")
        reason = (body.get("reason") or "").strip()[:500]
        need(reason, 400, "Γράψτε την αιτία της αναβολής")
        logic.add_timeline(iid, f"Αναβολή αποστολής drone: {reason}", who, "postpone")
        body["_detail"] = reason
    elif a == "note":
        need(body.get("text"), 400, "Κενή σημείωση")
        logic.add_timeline(iid, f"Σημείωση: {body['text']}", who, "note")
    elif a == "false_alarm":
        need(not m, 409, "Το drone είναι σε αποστολή. Δώστε πρώτα επιστροφή.")
        db.update("incidents", iid, {"status": "closed", "outcome": "Ψευδής συναγερμός", "note": body.get("note", ""),
                                     "closed_ts": time.time(), "assigned_user": inc["assigned_user"] or user["id"]})
        logic.add_timeline(iid, "Κλείσιμο ως ψευδής συναγερμός" + (f": {body['note']}" if body.get("note") else ""), who, "close")
        features.push("closed", iid)
    elif a == "snooze_camera":
        cid = int(body.get("camera_id") or inc["camera_id"])
        mins = int(body.get("minutes", 60))
        db.update("cameras", cid, {"snooze_until": time.time() + mins * 60})
        cam = db.q1("SELECT name FROM cameras WHERE id=?", (cid,))
        logic.add_timeline(iid, f"Παύση κάμερας «{cam['name']}» για {mins} λεπτά", who, "camera")
    elif a == "dispatch":
        need(not m, 409, "Υπάρχει ήδη drone σε αποστολή για αυτό το περιστατικό")
        tgt = logic.target_of(inc)
        need(tgt, 400, "Δεν υπάρχουν συντεταγμένες για την κάμερα ή την επιχείρηση")
        opts = logic.dock_options(*tgt)
        if body.get("dock_id"):
            dock = next((d for d in opts if d["id"] == int(body["dock_id"])), None)
        else:
            dock = next((d for d in opts if d["available"] and d["in_range"]), None)
        need(dock, 409, "Δεν υπάρχει διαθέσιμο drone εντός εμβέλειας")
        need(dock["available"], 409, f"Το {dock['name']} είναι ήδη σε αποστολή")
        pre = features.preflight(tgt[0], tgt[1], [dock])
        chk = dock["check"]
        if chk["level"] == "block":
            need(body.get("override"), 409, "Ο έλεγχος πτήσης δεν επιτρέπει την αποστολή: " + "; ".join(i["text"] for i in chk["issues"] if i["level"] == "block"))
            need(user["role"] in ("admin", "supervisor"), 403, "Μόνο επόπτης ή διαχειριστής μπορεί να παρακάμψει τον έλεγχο πτήσης")
            need((body.get("override_reason") or "").strip(), 400, "Γράψτε αιτιολογία για την παράκαμψη")
        try:
            logic.adapter_for(dock).dispatch(dock, *tgt)
        except logic.DroneError as e:
            raise ApiError(502, str(e))
        db.insert("missions", {"incident_id": iid, "dock_id": dock["id"], "start_ts": time.time(), "tlat": tgt[0],
                               "tlon": tgt[1], "distance_m": dock["distance_m"]})
        db.update("incidents", iid, {"status": "dispatched", "assigned_user": inc["assigned_user"] or user["id"],
                                     "acked_ts": inc["acked_ts"] or time.time()})
        logic.add_timeline(iid, f"Αποστολή drone από {dock['name']} · {dock['distance_m']/1000:.1f} km", who, "drone")
        w = pre["weather"]
        if w:
            logic.add_timeline(iid, f"Καιρός: άνεμος {w['wind']} m/s, ριπές {w['gust']} m/s, {w['desc'] or '—'}, {w['temp']}°C", who, "check")
        if chk["issues"]:
            logic.add_timeline(iid, ("Παράκαμψη ελέγχου πτήσης: " if chk["level"] == "block" else "Προειδοποιήσεις πτήσης: ")
                               + "; ".join(i["text"] for i in chk["issues"])
                               + (f" · αιτιολογία: {body['override_reason'].strip()}" if chk["level"] == "block" else ""), who, "check")
    elif a in ("return_home", "light", "camera_mode", "speaker_tts"):
        need(m, 409, "Δεν υπάρχει drone σε πτήση για αυτό το περιστατικό")
        dock = db.q1("SELECT * FROM docks WHERE id=?", (m["dock_id"],))
        ad = logic.adapter_for(dock)
        try:
            if a == "return_home":
                need(m["return_ts"] is None, 409, "Το drone επιστρέφει ήδη")
                ad.return_home(dock)
                v = logic.mission_view(m)
                db.update("missions", m["id"], {"return_ts": time.time(), "p_ret": v["progress"]})
                logic.add_timeline(iid, "Εντολή επιστροφής στο Dock", who, "drone")
            elif a == "light":
                need(dock["has_light"], 400, "Το drone δεν έχει προβολέα")
                on = bool(body.get("on"))
                ad.light(dock, on)
                db.update("missions", m["id"], {"light": int(on)})
                logic.add_timeline(iid, "Προβολέας " + ("ανοιχτός" if on else "κλειστός"), who, "drone")
            elif a == "camera_mode":
                mode = body.get("mode")
                need(mode in ("wide", "zoom", "ir"), 400, "Άγνωστη κάμερα")
                ad.camera(dock, mode)
                db.update("missions", m["id"], {"camera_mode": mode})
            elif a == "speaker_tts":
                need(dock["has_speaker"], 400, "Το drone δεν έχει μεγάφωνο")
                text = (body.get("text") or "").strip()
                need(text, 400, "Κενό μήνυμα")
                ad.speaker_tts(dock, text, int(body.get("volume", 80)))
                logic.add_timeline(iid, f"Μεγάφωνο: «{text}»", who, "speaker")
        except logic.DroneError as e:
            raise ApiError(502, str(e))
    elif a == "notify":
        ch = body.get("channel")
        need(ch in ("sms", "email"), 400, "Κανάλι sms ή email")
        msg = (body.get("message") or "").strip()
        need(msg, 400, "Κενό μήνυμα")
        ids = body.get("contact_ids") or []
        need(ids, 400, "Επιλέξτε τουλάχιστον μία επαφή")
        results = []
        for cid in ids:
            c = db.q1("SELECT * FROM contacts WHERE id=? AND org_id=?", (cid, inc["org_id"]))
            if not c:
                continue
            target = c["phone"] if ch == "sms" else c["email"]
            if not target:
                results.append({"contact": c["name"], "status": "skipped"})
                continue
            st, err = logic.notify(iid, ch, target, msg, who)
            results.append({"contact": c["name"], "status": st, "error": err})
        return {"ok": True, "results": results}
    elif a == "call_log":
        need(body.get("target"), 400, "Με ποιον μιλήσατε;")
        logic.notify(iid, "voice", body["target"] + (f" · {body['result']}" if body.get("result") else ""), body.get("result", ""), who)
    elif a == "police":
        num = body.get("number", "100")
        logic.notify(iid, "police", f"{num}" + (f" · {body['result']}" if body.get("result") else ""), body.get("summary", ""), who)
    elif a == "close":
        need(not m, 409, "Το drone δεν έχει προσγειωθεί ακόμα")
        outcome = body.get("outcome") or "Άλλο"
        db.update("incidents", iid, {"status": "closed", "outcome": outcome, "note": body.get("note", ""),
                                     "closed_ts": time.time(), "assigned_user": inc["assigned_user"] or user["id"]})
        logic.add_timeline(iid, f"Κλείσιμο: {outcome}" + (f" · {body['note']}" if body.get("note") else ""), who, "close")
        features.push("closed", iid)
    else:
        raise ApiError(400, "Άγνωστη ενέργεια")
    logic.audit(who, f"incident.{a}", f"#{iid}" + (f" · {body['_detail']}" if body.get("_detail") else ""))
    logic.publish("incident", org_id=inc["org_id"], id=iid)
    return {"ok": True, "incident": logic.incident_view(iid)}


COMMANDS = {"ack": "ack", "dispatch_drone": "dispatch", "return_home": "return_home", "false_alarm": "false_alarm",
            "close": "close", "speaker": "speaker_tts", "light_on": "light", "light_off": "light", "note": "note"}


def demo_alarm(org_id=None):
    orgs = db.q("SELECT * FROM orgs WHERE kind='customer'" + (" AND id=?" if org_id else ""), (org_id,) if org_id else ())
    need(orgs, 404, "Δεν υπάρχει οργανισμός")
    org = random.choice(orgs)
    cams = db.q("SELECT * FROM cameras WHERE org_id=?", (org["id"],))
    need(cams, 404, "Ο οργανισμός δεν έχει κάμερες")
    cam = random.choice(cams)
    raw, target = random.choice([("linedetection", "human"), ("fielddetection", "human"), ("fielddetection", "vehicle"),
                                 ("VMD", None), ("linedetection", None)])
    svg = demo_snapshot(org["name"], cam["name"], target)
    return logic.ingest(org["id"], cam["ext_key"], raw, target=target, camera_name=cam["name"], image=svg.encode(),
                        image_ext="svg", raw=f"demo {raw} {target or ''}", source="demo", edge_id=cam["edge_id"],
                        ignore_arming=True)


def demo_snapshot(org, cam, target):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fig = ""
    x = random.randint(260, 900)
    if target == "vehicle":
        fig = f'<rect x="{x}" y="430" width="220" height="90" rx="18" fill="#3a4654"/><rect x="{x-12}" y="418" width="244" height="114" fill="none" stroke="#facc15" stroke-width="4"/><text x="{x-10}" y="408" fill="#facc15" font-size="22" font-family="Arial">vehicle 0.91</text>'
    else:
        fig = f'<circle cx="{x}" cy="380" r="22" fill="#56606c"/><rect x="{x-24}" y="404" width="48" height="110" rx="14" fill="#56606c"/><rect x="{x-44}" y="346" width="88" height="180" fill="none" stroke="#facc15" stroke-width="4"/><text x="{x-44}" y="336" fill="#facc15" font-size="22" font-family="Arial">human 0.94</text>' if target == "human" else \
              f'<circle cx="{x}" cy="400" r="18" fill="#56606c"/><rect x="{x-20}" y="420" width="40" height="90" rx="12" fill="#56606c"/>'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1b232c"/><stop offset="1" stop-color="#2b333b"/></linearGradient></defs>
<rect width="1280" height="720" fill="url(#g)"/><path d="M0 560 L1280 520 L1280 720 L0 720Z" fill="#232b33"/><path d="M0 600 L1280 470" stroke="#3b444e" stroke-width="6"/>
<rect x="80" y="260" width="300" height="250" fill="#262e37"/><rect x="900" y="230" width="320" height="290" fill="#262e37"/><rect x="960" y="300" width="70" height="90" fill="#36404a"/>
{fig}<text x="24" y="44" fill="#e6edf3" font-size="26" font-family="Arial">{cam}</text><text x="1256" y="44" fill="#e6edf3" font-size="24" font-family="Arial" text-anchor="end">{now}</text>
<text x="24" y="700" fill="#8b9bb0" font-size="20" font-family="Arial">{org} · ΔΕΙΓΜΑ ΕΠΙΔΕΙΞΗΣ</text></svg>'''


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Cam2Drone/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def send(self, code, data, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit=20):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit * 1024 * 1024:
            raise ApiError(413, "Πολύ μεγάλο αίτημα")
        return self.rfile.read(n) if n > 0 else b""

    def json_body(self):
        try:
            return json.loads(self.body() or b"{}")
        except ValueError:
            raise ApiError(400, "Το σώμα πρέπει να είναι JSON")

    def cookie(self, name):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[name].value if name in c else None

    def bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def user(self, required=True, roles=None):
        u = logic.user_by_session(self.cookie("c2d")) or logic.user_by_api_token(self.bearer() or self.headers.get("X-Token"))
        if required:
            need(u, 401, "Απαιτείται σύνδεση")
        if roles and u:
            need(u["role"] in roles, 403, "Δεν έχετε δικαίωμα")
        return u

    def file(self, path, ctype=None, headers=None):
        need(os.path.isfile(path), 404, "Δεν βρέθηκε")
        with open(path, "rb") as f:
            data = f.read()
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and data and (rng.group(1) or rng.group(2)):
            size = len(data)
            a = int(rng.group(1)) if rng.group(1) else max(0, size - int(rng.group(2)))
            b = int(rng.group(2)) if rng.group(1) and rng.group(2) else size - 1
            b = min(b, size - 1)
            if a > b:
                return self.send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            return self.send(206, data[a:b + 1], ctype, {"Content-Range": f"bytes {a}-{b}/{size}", "Accept-Ranges": "bytes", **(headers or {})})
        self.send(200, data, ctype, {"Accept-Ranges": "bytes", **(headers or {})})

    def handle_any(self, method):
        try:
            u = urlparse(self.path)
            self.route(method, u.path, parse_qs(u.query))
        except ApiError as e:
            self.send(e.code, {"ok": False, "error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except sqlite3.IntegrityError as e:
            self.send(409, {"ok": False, "error": unique_message(str(e))})
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self.send(500, {"ok": False, "error": f"Σφάλμα server: {e}"})
            except Exception:  # noqa: BLE001
                pass

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")

    # ---- routes
    def route(self, method, path, qs):
        # static
        if method == "GET" and path in ("/", "/index.html"):
            return self.file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if method == "GET" and path in ("/sw.js", "/manifest.webmanifest"):
            return self.file(os.path.join(STATIC, path[1:]), "text/javascript; charset=utf-8" if path.endswith(".js") else "application/manifest+json")
        if method == "GET" and path.startswith("/static/"):
            name = os.path.normpath(unquote(path[len("/static/"):])).replace("\\", "/")
            need(not name.startswith(".."), 404, "Δεν βρέθηκε")
            return self.file(os.path.join(STATIC, name))
        if method == "GET" and path.startswith("/media/"):
            self.user()
            name = os.path.basename(unquote(path[len("/media/"):]))
            return self.file(os.path.join(db.MEDIA, name))

        # camera webhook (per organisation token)
        m = re.fullmatch(r"/webhook/([\w-]+)(/.*)?", path)
        if m:
            org = db.q1("SELECT * FROM orgs WHERE webhook_token=?", (m.group(1),))
            need(org, 404, "Άγνωστο webhook")
            body = self.body() if method in ("POST", "PUT") else b""
            summary, images, raw = parsers.parse_body(self.headers.get("Content-Type", ""), body)
            ip = summary.get("ipAddress") or self.client_address[0]
            ch = summary.get("channelID") or summary.get("dynChannelID") or "1"
            if not summary.get("eventType") and not body:
                return self.send(200, {"ok": True, "test": True})
            img = images[-1] if images else (None, "jpg")
            res = logic.ingest(org["id"], f"{ip}:{ch}", summary.get("eventType"), target=summary.get("targetType") or summary.get("detectionTarget"),
                               state=summary.get("eventState", "active"), camera_name=summary.get("channelName"),
                               image=img[0], image_ext=img[1], raw=raw, source="webhook")
            return self.send(200, res)

        # edge boxes
        if path.startswith("/api/edge/"):
            edge = db.q1("SELECT * FROM edges WHERE token=?", (self.bearer(),))
            need(edge, 401, "Άγνωστο κουτί (token)")
            db.update("edges", edge["id"], {"last_seen": time.time(), "ip": self.client_address[0]})
            features.edge_seen(edge)
            if path == "/api/edge/event" and method == "POST":
                b = self.json_body()
                need(b.get("camera_key"), 400, "Λείπει camera_key")
                res = logic.ingest(edge["org_id"], b["camera_key"], b.get("type") or b.get("raw_type"), target=b.get("target"),
                                   state=b.get("state", "active"), camera_name=b.get("camera_name"), image=b.get("image_b64"),
                                   image_ext=b.get("image_ext", "jpg"), raw=b.get("raw", ""), source=f"edge:{edge['name']}",
                                   edge_id=edge["id"], ts=b.get("ts"))
                return self.send(200, res)
            if path == "/api/edge/heartbeat" and method == "POST":
                b = self.json_body()
                db.update("edges", edge["id"], {"version": b.get("version", "")})
                for c in b.get("cameras", []):
                    cam = logic.get_or_create_camera(edge["org_id"], c["key"], c.get("name"), edge["id"])
                    was = cam["online"]
                    upd = {"last_seen": time.time(), "online": int(bool(c.get("online", True))), "edge_id": edge["id"]}
                    if isinstance(c.get("caps"), dict):  # what the camera supports over ONVIF (PTZ, presets, outputs, day/night)
                        upd["caps"] = json.dumps(c["caps"], ensure_ascii=False)[:8000]
                    db.update("cameras", cam["id"], upd)
                    features.camera_state(edge, cam, c.get("online", True) is not False)
                    features.video_state(edge, cam, c.get("caps"))
                    if was != int(bool(c.get("online", True))):
                        logic.publish("camera", org_id=edge["org_id"], id=cam["id"], online=bool(c.get("online", True)))
                ag = features.agent_info(meta_only=True)
                if edge.get("update_requested") and ag and b.get("version") == ag["version"]:
                    db.update("edges", edge["id"], {"update_requested": 0})
                    edge["update_requested"] = 0
                return self.send(200, features.edge_poll(edge))
            if path == "/api/edge/command" and method == "POST":
                res = features.command_result(edge, self.json_body())
                need(res, 404, "Άγνωστη εντολή")
                return self.send(200, res)
            if path == "/api/edge/poll" and method == "POST":
                return self.send(200, features.edge_poll(edge))
            if path == "/api/edge/frame" and method == "POST":
                cam = db.q1("SELECT id FROM cameras WHERE org_id=? AND ext_key=?", (edge["org_id"], self.headers.get("X-Camera-Key", "")))
                need(cam, 404, "Άγνωστη κάμερα")
                data = self.body(limit=3)
                need(data, 400, "Κενή εικόνα")
                features.store_frame(cam["id"], data, self.headers.get("Content-Type") or "image/jpeg")
                return self.send(200, {"ok": True})
            if path == "/api/edge/clip" and method == "POST":
                data = self.body(limit=40)
                need(len(data) > 1000, 400, "Κενό βίντεο")
                try:
                    ts = float(self.headers.get("X-Ts") or 0)
                except ValueError:
                    ts = 0
                res = features.attach_clip(edge, self.headers.get("X-Camera-Key", ""), ts, data, self.headers.get("Content-Type"))
                need(res, 404, "Δεν βρέθηκε συναγερμός για αυτό το κλιπ")
                return self.send(200, res)
            if path == "/api/edge/agent" and method == "GET":
                ag = features.agent_info()
                need(ag, 404, "Δεν βρέθηκε το agent.py στον server")
                return self.send(200, ag["data"], "text/x-python; charset=utf-8", {"X-Agent-Version": ag["version"], "X-Agent-Sha256": ag["sha256"]})
            raise ApiError(404, "Δεν βρέθηκε")

        # auth
        if path == "/api/login" and method == "POST":
            b = self.json_body()
            ip = self.client_address[0]
            need(not features.ip_blocked(ip), 429, "Πολλές αποτυχημένες προσπάθειες. Δοκιμάστε ξανά σε 15 λεπτά.")
            u = db.q1("SELECT * FROM users WHERE username=? AND active=1", (b.get("username", ""),))
            if u and u.get("locked_until") and u["locked_until"] > time.time():
                raise ApiError(423, "Ο λογαριασμός κλειδώθηκε μετά από πολλές λάθος προσπάθειες, έως "
                               + features.local(u["locked_until"]).strftime("%H:%M") + ". Ο διαχειριστής μπορεί να τον ξεκλειδώσει.")
            if not u or not logic.check_pw(b.get("password", ""), u["pw_hash"]):
                features.login_failed(ip, u)
                time.sleep(0.7)
                raise ApiError(401, "Λάθος όνομα χρήστη ή κωδικός")
            if u.get("totp_on"):
                if not b.get("code"):
                    return self.send(401, {"ok": False, "need_2fa": True, "error": "Δώστε τον 6ψήφιο κωδικό από την εφαρμογή επαλήθευσης"})
                if not features.totp_ok(u["totp_secret"], b["code"]):
                    features.login_failed(ip, u)
                    time.sleep(0.7)
                    return self.send(401, {"ok": False, "need_2fa": True, "error": "Λάθος κωδικός επαλήθευσης"})
            features.login_ok(u)
            tok = logic.new_session(u["id"])
            logic.audit(u["name"], "login")
            return self.send(200, {"ok": True}, headers={"Set-Cookie": f"c2d={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})
        if path == "/api/logout" and method == "POST":
            db.ex("DELETE FROM sessions WHERE token=?", (self.cookie("c2d"),))
            return self.send(200, {"ok": True}, headers={"Set-Cookie": "c2d=; Path=/; Max-Age=0"})
        if path == "/api/me":
            u = self.user()
            return self.send(200, {k: u[k] for k in ("id", "username", "name", "role", "org_id")} | {"version": VERSION, "platform": "python", "totp_on": bool(u.get("totp_on"))})
        if path in ("/api/2fa/setup", "/api/2fa/enable", "/api/2fa/disable") and method == "POST":
            u = self.user()
            b = self.json_body()
            if path.endswith("setup"):
                need(not u.get("totp_on"), 409, "Η επαλήθευση 2 βημάτων είναι ήδη ενεργή")
                secret = features.totp_new()
                db.update("users", u["id"], {"totp_secret": secret})
                return self.send(200, {"ok": True, "secret": secret, "uri": features.totp_uri(u, secret)})
            if path.endswith("enable"):
                need(u.get("totp_secret") and features.totp_ok(u["totp_secret"], b.get("code")), 400, "Λάθος κωδικός. Ελέγξτε ότι η ώρα του κινητού είναι σωστή.")
                db.update("users", u["id"], {"totp_on": 1})
                logic.audit(u["name"], "2fa.enable")
                return self.send(200, {"ok": True})
            need(logic.check_pw(b.get("password", ""), u["pw_hash"]), 400, "Λάθος κωδικός πρόσβασης")
            need(features.totp_ok(u.get("totp_secret"), b.get("code")), 400, "Λάθος κωδικός επαλήθευσης")
            db.update("users", u["id"], {"totp_on": 0, "totp_secret": None})
            logic.audit(u["name"], "2fa.disable")
            return self.send(200, {"ok": True})
        if path == "/api/nofly":
            self.user(roles=features.STAFF)
            return self.send(200, db.q("SELECT id,name,lat,lon,radius_m,level,geom FROM nofly WHERE enabled=1"))
        if path == "/api/admin/nofly/import" and method == "POST":
            u = self.user(roles={"admin"})
            b = self.json_body()
            try:
                n = features.import_zones(b.get("zones"), b.get("source"), bool(b.get("replace")))
            except ValueError as e:
                raise ApiError(400, str(e))
            logic.audit(u["name"], "nofly.import", f"{n} ζώνες · {b.get('source') or ''}")
            return self.send(200, {"ok": True, "imported": n})
        if path == "/api/password" and method == "POST":
            u = self.user()
            b = self.json_body()
            need(logic.check_pw(b.get("old", ""), u["pw_hash"]), 400, "Λάθος τρέχων κωδικός")
            need(len(b.get("new", "")) >= 8, 400, "Ο νέος κωδικός θέλει τουλάχιστον 8 χαρακτήρες")
            db.update("users", u["id"], {"pw_hash": logic.hash_pw(b["new"])})
            return self.send(200, {"ok": True})

        # live stream
        if path == "/api/stream":
            return self.stream(self.user())

        # overview for the console
        if path == "/api/overview":
            u = self.user()
            vis = logic.visible_org_ids(u)
            orgs = [o for o in db.q("SELECT id,name,kind,address,lat,lon,arming_mode,schedule,parent_id FROM orgs")
                    if vis is None or o["id"] in vis]
            for o in orgs:
                o["armed"] = logic.is_armed(o) if o["kind"] == "customer" else None
                cams = db.q("SELECT online, enabled FROM cameras WHERE org_id=?", (o["id"],))
                o["cams_total"] = len(cams)
                o["cams_online"] = sum(1 for c in cams if c["online"] and c["enabled"])
            busy = logic.busy_dock_ids()
            docks = [{**d, "busy": d["id"] in busy} for d in db.q("SELECT * FROM docks WHERE enabled=1")]
            flying = [logic.mission_view(mm) for mm in db.q("SELECT * FROM missions WHERE landed_ts IS NULL")]
            if vis is not None:
                flying = [f for f in flying if db.q1("SELECT org_id FROM incidents WHERE id=?", (f["incident_id"],))["org_id"] in vis]
            extra = {} if vis is not None else {"on_duty": features.on_duty(),
                                                "nofly_ver": "%s:%s" % tuple(db.q1("SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS m FROM nofly WHERE enabled=1").values())}
            return self.send(200, {"orgs": orgs, "docks": docks, "missions": flying, **extra,
                                   "incidents": logic.incident_list(vis), "server_time": time.time(),
                                   "types": {k: v[0] for k, v in logic.TYPES.items()}})
        m = re.fullmatch(r"/api/incidents/(\d+)", path)
        if m and method == "GET":
            u = self.user()
            inc = logic.incident_view(int(m.group(1)))
            need(inc, 404, "Δεν βρέθηκε")
            vis = logic.visible_org_ids(u)
            need(vis is None or inc["org_id"] in vis, 404, "Δεν βρέθηκε")
            tgt = logic.target_of(inc)
            inc["dock_options"] = logic.dock_options(*tgt) if tgt and u["role"] != "customer" else []
            if inc["dock_options"] and inc["status"] != "closed" and not (inc["mission"] and inc["mission"]["phase"] != "landed"):
                inc["preflight"] = features.preflight(tgt[0], tgt[1], inc["dock_options"])
            inc["police_summary"] = logic.police_summary(inc)
            if inc["mission"] and not inc["mission"]["simulated"]:  # real Dock: it pushes its video to the media server
                inc["mission"]["live_whep"] = features.media_play("dock", inc["mission"]["dock_id"])
            return self.send(200, inc)
        m = re.fullmatch(r"/api/incidents/(\d+)/action", path)
        if m and method == "POST":
            return self.send(200, incident_action(int(m.group(1)), self.user(), self.json_body()))
        m = re.fullmatch(r"/api/incidents/(\d+)/speaker-audio", path)
        if m and method == "POST":
            u = self.user(roles={"admin", "supervisor", "operator"})
            iid = int(m.group(1))
            mm = db.q1("SELECT * FROM missions WHERE incident_id=? AND landed_ts IS NULL", (iid,))
            need(mm, 409, "Δεν υπάρχει drone σε πτήση")
            data = self.body()
            need(len(data) > 200, 400, "Κενή ηχογράφηση")
            name = logic.save_image(data, "webm")
            dock = db.q1("SELECT * FROM docks WHERE id=?", (mm["dock_id"],))
            try:
                logic.adapter_for(dock).speaker_audio(dock, os.path.join(db.MEDIA, name), 80)
            except logic.DroneError as e:
                raise ApiError(502, str(e))
            secs = qs.get("secs", ["?"])[0]
            logic.add_timeline(iid, f"Μεγάφωνο: φωνητικό μήνυμα ελεγκτή ({secs}″)", u["name"], "speaker")
            return self.send(200, {"ok": True, "file": name})
        m = re.fullmatch(r"/report/(\d+)", path)
        if m:  # the page builds the report from /api/incidents/{id}/report (static/report.js, shared with Cloudflare)
            return self.file(os.path.join(STATIC, "report.html"), "text/html; charset=utf-8")
        m = re.fullmatch(r"/api/incidents/(\d+)/report", path)
        if m:
            u = self.user()
            iid = int(m.group(1))
            inc = logic.incident_view(iid)
            need(inc, 404, "Δεν βρέθηκε")
            vis = logic.visible_org_ids(u)
            need(vis is None or inc["org_id"] in vis, 404, "Δεν βρέθηκε")
            inc["events"] = db.q("SELECT e.ts, e.type, e.raw_type, e.target, e.state, e.source, e.image, e.clip, c.name AS camera FROM events e "
                                 "LEFT JOIN cameras c ON c.id=e.camera_id WHERE e.incident_id=? ORDER BY e.ts", (iid,))
            inc["missions"] = [logic.mission_view(x) for x in db.q("SELECT * FROM missions WHERE incident_id=? ORDER BY id", (iid,))]
            inc["center"] = db.q1("SELECT name, address FROM orgs WHERE kind='center' ORDER BY id LIMIT 1")
            inc["types"] = {k: v[0] for k, v in logic.TYPES.items()}
            inc.update(generated=time.time(), generated_by=u["name"])
            return self.send(200, inc)

        # Postman / integrations
        if path == "/api/command" and method == "POST":
            u = self.user(roles={"admin", "supervisor", "operator"})
            b = self.json_body()
            cmd = b.get("command")
            if cmd == "ping":
                return self.send(200, {"ok": True, "pong": time.time()})
            if cmd == "demo_alarm":
                return self.send(200, demo_alarm(b.get("org_id")))
            need(cmd in COMMANDS, 400, f"Άγνωστη εντολή. Δεκτές: {sorted(list(COMMANDS) + ['ping', 'demo_alarm'])}")
            iid = b.get("incident_id")
            if not iid:
                r = db.q1("SELECT id FROM incidents WHERE status!='closed' ORDER BY priority, first_ts LIMIT 1")
                need(r, 404, "Δεν υπάρχει ανοιχτό περιστατικό")
                iid = r["id"]
            body = {**b, "action": COMMANDS[cmd]}
            if cmd in ("light_on", "light_off"):
                body["on"] = cmd == "light_on"
            return self.send(200, incident_action(int(iid), u, body))
        if path == "/api/demo/alarm" and method == "POST":
            self.user(roles={"admin", "supervisor", "operator"})
            return self.send(200, demo_alarm(self.json_body().get("org_id")))

        # shifts
        if path == "/api/shift":
            return self.send(200, features.shift_state(self.user(roles=features.STAFF)))
        if path == "/api/shift/start" and method == "POST":
            return self.send(200, features.shift_start(self.user(roles=features.STAFF)))
        if path == "/api/shift/end" and method == "POST":
            u = self.user(roles=features.STAFF)
            b = self.json_body()
            return self.send(200, features.shift_end(u, b.get("note", ""), bool(b.get("release"))))

        # statistics
        if path in ("/api/stats", "/api/stats/incidents.csv"):
            u = self.user()
            vis = logic.visible_org_ids(u)
            try:
                to = float(qs.get("to", [time.time()])[0])
                frm = float(qs.get("from", [to - 30 * 86400])[0])
                org = int(qs["org_id"][0]) if qs.get("org_id", [""])[0] else None
            except ValueError:
                raise ApiError(400, "Λάθος παράμετροι")
            need(0 < to - frm <= 400 * 86400, 400, "Το διάστημα πρέπει να είναι έως 400 ημέρες")
            if path.endswith(".csv"):
                return self.send(200, features.incidents_csv(vis, frm, to, org), "text/csv; charset=utf-8",
                                 {"Content-Disposition": 'attachment; filename="incidents.csv"'})
            return self.send(200, features.stats(vis, frm, to, org, staff=u["role"] != "customer"))

        # push notifications
        if path == "/api/push":
            features.SUBJECT["value"] = "https://" + (self.headers.get("Host") or "localhost")
            return self.send(200, features.push_status(self.user()))
        if path in ("/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test") and method == "POST":
            u = self.user()
            b = self.json_body()
            if path.endswith("unsubscribe"):
                features.push_unsubscribe(u, b.get("endpoint", ""))
            elif path.endswith("subscribe"):
                need(features.webpush.AVAILABLE, 501, features.push_status(u).get("reason", ""))
                try:
                    features.push_subscribe(u, b)
                except ValueError as e:
                    raise ApiError(400, str(e))
            else:
                need(features.webpush.AVAILABLE, 501, features.push_status(u).get("reason", ""))
                subs = db.q("SELECT * FROM push_subs WHERE user_id=?", (u["id"],))
                need(subs, 404, "Δεν υπάρχει συσκευή με ενεργές ειδοποιήσεις")
                res = [features.webpush.send(s, {"title": "Δοκιμαστική ειδοποίηση", "body": "Οι ειδοποιήσεις λειτουργούν.", "tag": "test", "url": "/"},
                                             features.SUBJECT["value"]) for s in subs]
                return self.send(200, {"ok": True, "results": res})
            return self.send(200, {"ok": True, **features.push_status(u)})

        # camera control: arming per camera, ONVIF commands through the box
        m = re.fullmatch(r"/api/cameras/(\d+)/(control|arming|command)", path)
        if m:
            u = self.user(roles=features.STAFF)
            cam = db.q1("SELECT * FROM cameras WHERE id=?", (int(m.group(1)),))
            need(cam, 404, "Δεν βρέθηκε")
            if m.group(2) == "control":
                return self.send(200, features.camera_control(cam))
            need(method == "POST", 405, "Μη επιτρεπτή μέθοδος")
            b = self.json_body()
            if m.group(2) == "arming":
                mode = b.get("mode")
                need(mode in features.ARMING, 400, "Οπλισμός: inherit, armed ή disarmed")
                db.update("cameras", cam["id"], {"arming_mode": mode})
                logic.audit(u["name"], "camera.arming", f"{cam['name']}: {features.ARMING[mode]}")
                if b.get("incident_id"):
                    logic.add_timeline(int(b["incident_id"]), f"Κάμερα «{cam['name']}»: {features.ARMING[mode].lower()}", u["name"], "camera")
                logic.publish("camera", org_id=cam["org_id"], id=cam["id"])
                return self.send(200, features.camera_control(db.q1("SELECT * FROM cameras WHERE id=?", (cam["id"],))))
            need(cam["edge_id"], 409, "Η κάμερα δεν συνδέεται μέσω κουτιού, άρα δεν δέχεται εντολές")
            try:
                cid = features.queue_command(cam, u, b.get("cmd"), b.get("args") or {})
            except PermissionError as e:
                raise ApiError(403, str(e))
            except (ValueError, TypeError) as e:
                raise ApiError(400, str(e))
            return self.send(200, {"ok": True, "id": cid})
        m = re.fullmatch(r"/api/commands/(\d+)", path)
        if m:
            self.user(roles=features.STAFF)
            q = features.command_view(int(m.group(1)))
            need(q, 404, "Δεν βρέθηκε")
            return self.send(200, q)

        # ICE servers for live video: Cloudflare TURN relay (env TURN_KEY_ID / TURN_KEY_TOKEN) for networks that block a direct connection
        if path == "/api/ice":
            self.user()
            return self.send(200, features.ice_servers())

        # real-time video through the box (go2rtc): the browser's WebRTC offer goes to the box as a command, the answer comes back
        m = re.fullmatch(r"/api/cameras/(\d+)/webrtc", path)
        if m:
            u = self.user()
            cam = db.q1("SELECT * FROM cameras WHERE id=?", (int(m.group(1)),))
            vis = logic.visible_org_ids(u)
            need(cam and (vis is None or cam["org_id"] in vis), 404, "Δεν βρέθηκε")
            need(cam["edge_id"] and features.caps_of(cam).get("webrtc"), 409, "Η κάμερα δεν έχει ζωντανό βίντεο μέσω κουτιού")
            if method == "POST":
                sdp = str(self.json_body().get("sdp") or "")
                need(sdp.startswith("v=0") and len(sdp) < 20000, 400, "Άκυρο αίτημα βίντεο")
                features.live_request(cam["id"])
                cid = db.insert("cam_commands", {"camera_id": cam["id"], "edge_id": cam["edge_id"], "cmd": "webrtc", "args": {"sdp": sdp},
                                                 "who": u["name"], "created": time.time()})
                return self.send(200, {"ok": True, "id": cid})
            q = db.q1("SELECT status, result FROM cam_commands WHERE id=? AND camera_id=? AND cmd='webrtc'",
                      (int((qs.get("id") or ["0"])[0] or 0), cam["id"]))
            need(q, 404, "Δεν βρέθηκε")
            try:
                r = json.loads(q["result"] or "null")
            except ValueError:
                r = None
            return self.send(200, {"status": q["status"], "sdp": r.get("sdp") if q["status"] == "done" and r else None,
                                   "error": r.get("error") if q["status"] == "failed" and r else None})

        # near-live camera image (through the edge box)
        m = re.fullmatch(r"/api/cameras/(\d+)/(live|frame)", path)
        if m:
            u = self.user()
            cam = db.q1("SELECT * FROM cameras WHERE id=?", (int(m.group(1)),))
            vis = logic.visible_org_ids(u)
            need(cam and (vis is None or cam["org_id"] in vis), 404, "Δεν βρέθηκε")
            need(cam["edge_id"], 409, "Η κάμερα δεν συνδέεται μέσω κουτιού, άρα δεν υπάρχει ζωντανή εικόνα")
            features.live_request(cam["id"])
            fr = features.live_frame(cam["id"])
            if m.group(2) == "frame":
                need(fr, 404, "Δεν υπάρχει ακόμα εικόνα")
                return self.send(200, fr[1], fr[2])
            edge = db.q1("SELECT last_seen FROM edges WHERE id=?", (cam["edge_id"],))
            online = bool(edge and edge["last_seen"] and time.time() - edge["last_seen"] < 120)
            return self.send(200, {"ok": True, "frame_ts": fr[0] if fr else None, "server_time": time.time(), "edge_online": online,
                                   "whep": features.media_play("cam", cam["id"]), "p2p": bool(online and features.caps_of(cam).get("webrtc"))})

        # admin
        m = re.fullmatch(r"/api/admin/(\w+)(?:/(\d+))?", path)
        if m:
            return self.admin(method, m.group(1), m.group(2))
        raise ApiError(404, "Δεν βρέθηκε")

    def admin(self, method, table, id_):
        u = self.user(roles={"admin", "supervisor"} if method == "GET" else {"admin"})
        if table == "settings":
            if method == "GET":
                media = features.media_cfg()
                return self.send(200, {"flight": features.flight_limits(), "retention_days": features.retention_days(),
                                       "media": {"publish": media.get("publish", ""), "play": media.get("play", "")},
                                       "sms": db.setting("sms", {}), "smtp": {**(db.setting("smtp", {}) or {}), "password": "••••" if (db.setting("smtp", {}) or {}).get("password") else ""}})
            b = self.json_body()
            if "sms" in b:
                db.set_setting("sms", b["sms"])
            if "retention_days" in b:
                try:
                    db.set_setting("retention_days", max(0, min(3650, int(b["retention_days"]))))
                except (TypeError, ValueError):
                    raise ApiError(400, "Ημέρες διατήρησης: αριθμός")
            if "media" in b:
                old = features.media_cfg()
                pub, play = str(b["media"].get("publish") or "").strip(), str(b["media"].get("play") or "").strip()
                need(not pub or (pub.startswith(("rtsp://", "rtsps://", "rtmp://", "rtmps://", "srt://")) and "{path}" in pub), 400,
                     "Η διεύθυνση αποστολής πρέπει να είναι rtsp/rtmp/srt και να περιέχει {path}")
                need(not play or (play.startswith("https://") and "{path}" in play), 400, "Η διεύθυνση προβολής πρέπει να είναι https και να περιέχει {path}")
                db.set_setting("media", {"publish": pub, "play": play, "secret": old.get("secret") or secrets.token_hex(16)})
            if "flight" in b:
                try:
                    db.set_setting("flight", {k: float(b["flight"][k]) for k in features.FLIGHT_DEFAULTS if k in b["flight"]})
                except (TypeError, ValueError):
                    raise ApiError(400, "Τα όρια πτήσης πρέπει να είναι αριθμοί")
            if "smtp" in b:
                old = db.setting("smtp", {}) or {}
                new = b["smtp"]
                if new.get("password") in ("", "••••"):
                    new["password"] = old.get("password", "")
                db.set_setting("smtp", new)
            logic.audit(u["name"], "settings.update")
            return self.send(200, {"ok": True})
        if table == "audit":
            return self.send(200, db.q("SELECT * FROM audit ORDER BY ts DESC LIMIT 300"))
        if table == "events":
            return self.send(200, db.q("SELECT e.id,e.ts,e.type,e.raw_type,e.target,e.state,e.suppressed,e.source,e.incident_id,"
                                       "o.name AS org,c.name AS camera FROM events e LEFT JOIN orgs o ON o.id=e.org_id "
                                       "LEFT JOIN cameras c ON c.id=e.camera_id ORDER BY e.ts DESC LIMIT 300"))
        need(table in ADMIN, 404, "Άγνωστος πίνακας")
        fields = ADMIN[table]
        if method == "GET":
            rows = db.q(f"SELECT * FROM {table} ORDER BY id")
            for r in rows:
                r.pop("pw_hash", None)
                r.pop("totp_secret", None)
                if table == "users":
                    r["api_token"] = (r.get("api_token") or "")[:6] + "…"
                if table == "edges":
                    r["token"] = (r.get("token") or "")[:6] + "…"
                    r["cameras"] = db.q1("SELECT COUNT(*) AS n FROM cameras WHERE edge_id=?", (r["id"],))["n"]
                    ag = features.agent_info(meta_only=True)
                    r["agent_latest"] = ag["version"] if ag else None
                if table == "orgs":
                    r["webhook_url"] = f"/webhook/{r['webhook_token']}" if r.get("kind") == "customer" else ""
                    r.pop("webhook_token", None)
            return self.send(200, rows)
        if method == "DELETE":
            need(id_, 400, "Λείπει id")
            if table == "users":
                need(int(id_) != u["id"], 400, "Δεν μπορείτε να διαγράψετε τον εαυτό σας")
            db.ex(f"DELETE FROM {table} WHERE id=?", (int(id_),))
            logic.audit(u["name"], f"{table}.delete", id_)
            return self.send(200, {"ok": True})
        b = self.json_body()
        data = {k: b[k] for k in fields if k in b}
        for k, v in list(data.items()):
            if v == "" and k in ("lat", "lon", "parent_id", "org_id", "edge_id", "snooze_until"):
                data[k] = None
        if table == "users":
            need(data.get("role", "operator") in ROLES, 400, "Άγνωστος ρόλος")
            if b.get("password"):
                need(len(b["password"]) >= 8, 400, "Ο κωδικός θέλει τουλάχιστον 8 χαρακτήρες")
                data["pw_hash"] = logic.hash_pw(b["password"])
        extra = {}
        if method == "POST":
            if table == "orgs":
                data["webhook_token"] = secrets.token_urlsafe(12)
            if table == "edges":
                data["token"] = secrets.token_urlsafe(24)
                extra["token"] = data["token"]
            if table == "users":
                need(data.get("pw_hash"), 400, "Ορίστε κωδικό")
                data["api_token"] = secrets.token_urlsafe(24)
                extra["api_token"] = data["api_token"]
            new_id = db.insert(table, data)
            logic.audit(u["name"], f"{table}.create", str(new_id))
            return self.send(200, {"ok": True, "id": new_id, **extra})
        if method == "PUT":
            need(id_, 400, "Λείπει id")
            if table == "users" and b.get("reset_2fa"):
                data.update(totp_on=0, totp_secret=None)
            if table == "users" and b.get("unlock"):
                data.update(locked_until=None, failed_logins=0)
            if table == "edges" and b.get("request_update"):
                data["update_requested"] = 1
            if table == "edges" and b.get("regenerate_token"):
                data["token"] = secrets.token_urlsafe(24)
                extra["token"] = data["token"]
            db.update(table, int(id_), data)
            logic.audit(u["name"], f"{table}.update", id_)
            return self.send(200, {"ok": True, **extra})
        raise ApiError(405, "Μη επιτρεπτή μέθοδος")

    def stream(self, user):
        vis = logic.visible_org_ids(user)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        qq = logic.subscribe()
        last_mission = 0
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = qq.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if vis is not None and msg.get("org_id") is not None and msg["org_id"] not in vis:
                    continue
                if msg["kind"] == "mission":
                    if time.time() - last_mission < 0.9:
                        continue
                    last_mission = time.time()
                self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            logic.unsubscribe(qq)
            self.close_connection = True


def report_html(inc):
    esc = lambda s: str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M:%S") if t else "—"  # noqa: E731
    imgs = "".join(f'<img src="/media/{esc(i)}" alt="">' for i in inc["images"][-6:])
    tl = "".join(f"<tr><td>{fmt(t['ts'])}</td><td>{esc(t['text'])}</td><td>{esc(t['who'])}</td></tr>" for t in inc["timeline"])
    m = inc["mission"]
    return f"""<!doctype html><html lang="el"><head><meta charset="utf-8"><title>Αναφορά περιστατικού #{inc['id']}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;color:#1d232a;max-width:900px;margin:24px auto;padding:0 16px}}
h1{{font-size:22px;margin:0 0 4px}}.meta{{color:#5b6672;margin-bottom:18px}}table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border-bottom:1px solid #dde3e9;padding:6px 8px;text-align:left;vertical-align:top}}th{{color:#5b6672;font-weight:600;width:200px}}
.imgs{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}}.imgs img{{width:100%;border-radius:6px;border:1px solid #dde3e9}}
h2{{font-size:16px;margin:22px 0 8px}}@media print{{button{{display:none}}}}</style></head><body>
<button onclick="print()" style="float:right;padding:8px 14px">Εκτύπωση / PDF</button>
<h1>Αναφορά περιστατικού #{inc['id']}</h1><div class="meta">{esc(inc['org']['name'])} · {esc(inc['type_label'])} · {fmt(inc['first_ts'])}</div>
<table><tr><th>Επιχείρηση</th><td>{esc(inc['org']['name'])}<br>{esc(inc['org']['address'])}</td></tr>
<tr><th>Κάμερες</th><td>{esc(', '.join(c['name'] for c in inc['camera_list']))}</td></tr>
<tr><th>Σήματα</th><td>{inc['count']}</td></tr><tr><th>Ελεγκτής</th><td>{esc(inc['assigned_name'])}</td></tr>
<tr><th>Drone</th><td>{esc(m['dock']['name'] + ' · ' + ('προσομοίωση' if m['simulated'] else 'DJI Dock')) if m else 'Δεν στάλθηκε'}</td></tr>
<tr><th>Αποτέλεσμα</th><td>{esc(inc['outcome'] or 'Ανοιχτό')}</td></tr><tr><th>Σημείωση</th><td>{esc(inc['note'])}</td></tr>
<tr><th>Κλείσιμο</th><td>{fmt(inc['closed_ts'])}</td></tr></table>
<h2>Εικόνες</h2><div class="imgs">{imgs or '<p>Δεν υπάρχουν εικόνες.</p>'}</div>
<h2>Χρονολόγιο</h2><table>{tl}</table></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Cam to Drone server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    except AttributeError:
        pass
    db.init()
    logic.HOOKS["push"] = features.push
    created = seed.run()
    threading.Thread(target=logic.ticker, daemon=True).start()

    def housekeeping():  # offline boxes every 30 s, old media once an hour
        n = 0
        while True:
            time.sleep(30)
            try:
                features.offline_check()
                if n % 120 == 0:
                    features.retention_run()
            except Exception as e:  # noqa: BLE001
                print("[housekeeping]", e)
            n += 1
    threading.Thread(target=housekeeping, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"Cam to Drone {VERSION}")
    print(f"  Κονσόλα:  http://localhost:{a.port}/")
    print(f"  Δεδομένα: {db.DATA}")
    if created:
        print(f"  Πρώτη εκκίνηση: κωδικοί στο {created}")
    if logic.SIM_SPEED != 1:
        print(f"  Ταχύτητα προσομοίωσης x{logic.SIM_SPEED}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
