#!/usr/bin/env python3
"""
Cam to Drone · edge agent (Raspberry Pi / any Linux box at the customer site).

Listens to the cameras of ONE site, translates their alarms (ONVIF, Hikvision ISAPI, Dahua, HTTP push, dry contacts)
into one common format and sends them to the central server over HTTPS (outbound only).

  python3 agent.py --config config.json          run
  python3 agent.py --discover                    find ONVIF cameras on the local network
  python3 agent.py --config config.json --test   send one test alarm per camera and exit

Python 3.8+. Standard library only, plus optional:  pip install onvif-zeep  (ONVIF cameras)  gpiozero (dry contacts)
"""
import argparse
import base64
import email
import email.policy
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

VERSION = "1.0.0"
log = logging.getLogger("edge")

# ------------------------------------------------------------------ type normalisation (same names as the server)
RAW_MAP = {
    "vmd": "motion", "motiondetection": "motion", "linedetection": "line", "fielddetection": "zone",
    "regionentrance": "zone", "regionexiting": "zone", "shelteralarm": "tamper", "tamperdetection": "tamper",
    "videoloss": "videoloss", "io": "input", "facedetection": "face", "pir": "motion",
    "videomotion": "motion", "crosslinedetection": "line", "crossregiondetection": "zone",
    "smartmotionhuman": "human", "smartmotionvehicle": "vehicle", "videoblind": "tamper", "alarmlocal": "input",
    "cellmotiondetector": "motion", "motion": "motion", "linedetector": "line", "fielddetector": "zone",
    "peopledetect": "human", "humandetect": "human", "vehicledetect": "vehicle", "tampering": "tamper",
    "digitalinput": "input", "intrusion": "zone", "tripwire": "line",
}


def norm(raw, target=None):
    key = re.sub(r"[^a-z]", "", str(raw or "").lower().split("/")[-1])
    t = RAW_MAP.get(key)
    if t is None:
        low = str(raw or "").lower()
        t = next((v for k, v in RAW_MAP.items() if k in low.replace("/", "")), "other")
    tg = str(target or "").lower()
    if t in ("line", "zone", "motion", "other") and tg in ("human", "person", "people"):
        t = "human"
    elif t in ("line", "zone", "motion", "other") and tg in ("vehicle", "car"):
        t = "vehicle"
    return t


# ------------------------------------------------------------------ uplink to the central server (with spool)
class Uplink:
    def __init__(self, cfg):
        self.base = cfg["server"].rstrip("/")
        self.token = cfg["token"]
        self.spool = cfg.get("spool_dir") or os.path.join(os.path.dirname(os.path.abspath(cfg["_path"])), "spool")
        os.makedirs(self.spool, exist_ok=True)
        self.ctx = ssl.create_default_context()
        if cfg.get("verify_tls") is False:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.lock = threading.Lock()
        self.online = False

    def post(self, path, payload, timeout=15):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}",
                                              "User-Agent": f"cam2drone-edge/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx if self.base.startswith("https") else None) as r:
            return json.loads(r.read() or b"{}")

    def send_event(self, ev):
        try:
            res = self.post("/api/edge/event", ev)
            self.online = True
            log.info("sent %s %s -> %s", ev["camera_key"], ev["type"], res.get("incident_id") or res.get("suppressed") or "ok")
            return True
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                log.error("server rejected event (%s): %s", e.code, e.read()[:200])
                return True  # do not retry a permanent error
            log.warning("server error %s, spooling", e.code)
        except Exception as e:  # noqa: BLE001
            log.warning("server unreachable (%s), spooling", e)
        self.online = False
        with open(os.path.join(self.spool, f"{time.time():.3f}_{uuid.uuid4().hex[:6]}.json"), "w") as f:
            json.dump(ev, f)
        return False

    def flush_spool(self):
        files = sorted(os.listdir(self.spool))
        for name in files[:50]:
            p = os.path.join(self.spool, name)
            try:
                with open(p) as f:
                    ev = json.load(f)
                self.post("/api/edge/event", ev)
                os.remove(p)
                log.info("delivered spooled event %s", name)
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403):
                    os.remove(p)
                return
            except Exception:  # noqa: BLE001
                return
        # keep spool bounded (about 2000 events)
        for name in sorted(os.listdir(self.spool))[:-2000]:
            os.remove(os.path.join(self.spool, name))


# ------------------------------------------------------------------ helpers
def http_opener(user, password, url):
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, user, password)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr), urllib.request.HTTPBasicAuthHandler(mgr))


def fetch(opener, url, timeout=6):
    try:
        with opener.open(url, timeout=timeout) as r:
            data = r.read(3 * 1024 * 1024)
            ctype = r.headers.get("Content-Type", "")
            return data, ("png" if "png" in ctype else "jpg")
    except Exception as e:  # noqa: BLE001
        log.debug("snapshot failed %s: %s", url, e)
        return None, None


XML_FIELDS = ["eventType", "eventState", "channelID", "dynChannelID", "channelName", "dateTime", "ipAddress",
              "activePostCount", "targetType", "detectionTarget"]


def parse_xml(text):
    out = {}
    for f in XML_FIELDS:
        m = re.search(rf"<(?:\w+:)?{f}>(.*?)</(?:\w+:)?{f}>", text, re.S)
        if m:
            out[f] = m.group(1).strip()
    return out


# ------------------------------------------------------------------ camera workers
class Worker(threading.Thread):
    def __init__(self, agent, cam):
        super().__init__(daemon=True, name=f"cam-{cam['key']}")
        self.agent, self.cam = agent, cam
        self.connected = False

    def keys(self):
        """[(camera_key, name)] this worker reports in heartbeats."""
        chans = self.cam.get("channels")
        if chans:
            return [(f"{self.cam['key']}:{c}", n) for c, n in chans.items()]
        return [(self.cam["key"], self.cam.get("name", self.cam["key"]))]

    def key_for(self, channel):
        chans = self.cam.get("channels")
        if chans:
            c = str(channel or "1")
            return f"{self.cam['key']}:{c}", chans.get(c, f"{self.cam.get('name', self.cam['key'])} κανάλι {c}")
        return self.cam["key"], self.cam.get("name", self.cam["key"])

    def run(self):
        backoff = 2
        while True:
            try:
                self.loop()
                backoff = 2
            except Exception as e:  # noqa: BLE001
                self.connected = False
                log.warning("[%s] %s, reconnecting in %ss", self.cam["key"], e, backoff)
                time.sleep(backoff)
                backoff = min(60, backoff * 2)


class HikvisionWorker(Worker):
    """ISAPI alertStream: long HTTP connection, the camera/NVR pushes <EventNotificationAlert> blocks."""

    def loop(self):
        c = self.cam
        base = f"http://{c['host']}:{c.get('port', 80)}"
        op = http_opener(c["user"], c["password"], base)
        with op.open(base + "/ISAPI/Event/notification/alertStream", timeout=90) as r:
            self.connected = True
            log.info("[%s] connected to Hikvision alertStream", c["key"])
            buf = ""
            while True:
                chunk = r.read1(8192) if hasattr(r, "read1") else r.read(1024)
                if not chunk:
                    raise ConnectionError("stream closed")
                buf += chunk.decode("utf-8", "replace")
                while True:
                    m = re.search(r"<EventNotificationAlert.*?</EventNotificationAlert>", buf, re.S)
                    if not m:
                        buf = buf[-20000:]
                        break
                    buf = buf[m.end():]
                    s = parse_xml(m.group(0))
                    if s.get("eventState", "active").lower() != "active":
                        continue  # 'videoloss inactive' is Hikvision's heartbeat
                    ch = s.get("channelID") or s.get("dynChannelID") or c.get("channel", 1)
                    snap = c.get("snapshot_url") or f"{base}/ISAPI/Streaming/channels/{int(ch)}01/picture"
                    self.agent.emit(self, ch, s.get("eventType"), s.get("targetType") or s.get("detectionTarget"),
                                    lambda: fetch(op, snap), raw=m.group(0)[:3000])


class DahuaWorker(Worker):
    """Dahua eventManager attach: multipart text stream with lines Code=...;action=Start;index=N"""

    def loop(self):
        c = self.cam
        base = f"http://{c['host']}:{c.get('port', 80)}"
        op = http_opener(c["user"], c["password"], base)
        url = base + "/cgi-bin/eventManager.cgi?action=attach&codes=%5BAll%5D&heartbeat=5"
        with op.open(url, timeout=60) as r:
            self.connected = True
            log.info("[%s] connected to Dahua eventManager", c["key"])
            for line in r:
                line = line.decode("utf-8", "replace").strip()
                m = re.search(r"Code=([^;\s]+);action=([^;\s]+);index=(\d+)", line)
                if not m or m.group(2).lower() != "start" or m.group(1) == "Heartbeat":
                    continue
                ch = int(m.group(3)) + 1
                snap = c.get("snapshot_url") or f"{base}/cgi-bin/snapshot.cgi?channel={ch}"
                target = "human" if "human" in line.lower() else "vehicle" if "vehicle" in line.lower() else None
                self.agent.emit(self, ch, m.group(1), target, lambda: fetch(op, snap), raw=line[:2000])


class OnvifWorker(Worker):
    """ONVIF PullPoint subscription (needs: pip install onvif-zeep). Camera clock must be in sync (NTP)."""

    def loop(self):
        try:
            from onvif import ONVIFCamera  # type: ignore
        except ImportError:
            raise RuntimeError("λείπει το πακέτο onvif-zeep: pip install onvif-zeep")
        from datetime import timedelta
        c = self.cam
        cam = ONVIFCamera(c["host"], c.get("port", 80), c["user"], c["password"])
        snap_uri = c.get("snapshot_url")
        if not snap_uri:
            try:
                media = cam.create_media_service()
                prof = media.GetProfiles()[0]
                snap_uri = media.GetSnapshotUri({"ProfileToken": prof.token}).Uri
            except Exception as e:  # noqa: BLE001
                log.info("[%s] no ONVIF snapshot uri: %s", c["key"], e)
        op = http_opener(c["user"], c["password"], snap_uri) if snap_uri else None
        ev = cam.create_events_service()
        ev.CreatePullPointSubscription()
        pp = cam.create_pullpoint_service()
        req = pp.create_type("PullMessages")
        req.MessageLimit = 50
        req.Timeout = timedelta(seconds=20)
        try:
            pp.SetSynchronizationPoint()
        except Exception:  # noqa: BLE001
            pass
        self.connected = True
        log.info("[%s] ONVIF PullPoint subscription active", c["key"])
        last_renew = time.time()
        while True:
            resp = pp.PullMessages(req)
            for msg in (resp.NotificationMessage or []):
                topic = str(getattr(msg.Topic, "_value_1", "") or "")
                items = {}
                try:
                    data = msg.Message._value_1.Data
                    for it in (data.SimpleItem or []):
                        items[it.Name] = str(it.Value)
                    src = msg.Message._value_1.Source
                    for it in (src.SimpleItem or []) if src is not None else []:
                        items["src_" + it.Name] = str(it.Value)
                except Exception:  # noqa: BLE001
                    pass
                state = next((v for k, v in items.items() if k in ("IsMotion", "State", "IsInside", "LogicalState", "IsTamper")), "true")
                if state.lower() not in ("true", "1", "active"):
                    continue
                target = items.get("ObjectType") or items.get("ClassTypes") or None
                self.agent.emit(self, items.get("src_VideoSourceConfigurationToken"), topic, target,
                                (lambda: fetch(op, snap_uri)) if op else None, raw=json.dumps({"topic": topic, **items})[:2000])
            if time.time() - last_renew > 50:
                try:
                    pp.Renew({"TerminationTime": "PT120S"})
                except Exception:  # noqa: BLE001
                    pass
                last_renew = time.time()


class WebhookHub:
    """Local HTTP server for cameras configured to PUSH alarms (Hikvision 'HTTP listening', Dahua/others 'alarm server')."""

    def __init__(self, agent, port):
        self.agent, self.port = agent, port
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                hub.handle(self.client_address[0], self.path, self.headers.get("Content-Type", ""), body)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

            do_PUT = do_POST

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

        self.srv = ThreadingHTTPServer(("0.0.0.0", port), H)

    def start(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        log.info("local webhook listening on :%s", self.port)

    def handle(self, ip, path, ctype, body):
        summary, image = {}, None
        if "multipart" in ctype.lower():
            msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body, policy=email.policy.default)
            for part in (msg.iter_parts() if msg.is_multipart() else []):
                payload = part.get_payload(decode=True) or b""
                if part.get_content_type().startswith("image/"):
                    image = (payload, "jpg")
                else:
                    summary.update(parse_xml(payload.decode("utf-8", "replace")))
        else:
            text = body.decode("utf-8", "replace")
            if text.lstrip().startswith("{"):
                try:
                    j = json.loads(text)
                    summary = {"eventType": j.get("eventType") or j.get("event") or j.get("Code") or j.get("type"),
                               "channelID": j.get("channel") or j.get("channelID"), "targetType": j.get("target"),
                               "eventState": j.get("state", "active")}
                except ValueError:
                    pass
            elif "<" in text:
                summary = parse_xml(text)
            else:
                summary = {k: v[0] for k, v in parse_qs(text).items()}
        if str(summary.get("eventState", "active")).lower() not in ("active", "start", "true", "1"):
            return
        src_ip = summary.get("ipAddress") or ip
        hook = path.strip("/").split("/")[-1] if path.startswith("/hook/") else None
        w = next((w for w in self.agent.workers if isinstance(w, PushCamera) and
                  ((hook and w.cam["key"] == hook) or (not hook and w.cam.get("match_ip") == src_ip))), None)
        if not w:
            log.warning("push from unknown camera %s %s (add it to config with kind=webhook)", src_ip, path)
            return
        w.connected = True
        w.last = time.time()
        img = (lambda: image) if image else None
        self.agent.emit(w, summary.get("channelID"), summary.get("eventType"), summary.get("targetType") or summary.get("detectionTarget"),
                        img, raw=body[:2000].decode("utf-8", "replace"))


class PushCamera(Worker):
    """Placeholder worker for cameras that push to the local webhook. Online = pushed something in the last hour."""

    def run(self):
        self.last = 0
        self.connected = True

    def keys(self):
        return super().keys()


class GpioWorker(Worker):
    """Dry contact on a Raspberry Pi GPIO pin (e.g. alarm panel output, door sensor). Needs gpiozero."""

    def loop(self):
        try:
            from gpiozero import Button  # type: ignore
        except ImportError:
            raise RuntimeError("λείπει το gpiozero (sudo apt install python3-gpiozero)")
        c = self.cam
        b = Button(int(c["pin"]), pull_up=bool(c.get("active_low", True)), bounce_time=0.2)
        self.connected = True
        log.info("[%s] watching GPIO %s", c["key"], c["pin"])
        while True:
            b.wait_for_press()
            self.agent.emit(self, None, "input", None, None, raw=f"gpio {c['pin']}")
            b.wait_for_release()


KINDS = {"hikvision": HikvisionWorker, "dahua": DahuaWorker, "onvif": OnvifWorker, "webhook": PushCamera, "gpio": GpioWorker}


# ------------------------------------------------------------------ agent
class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.up = Uplink(cfg)
        self.workers = self._build()
        self.last = {}
        self.throttle = float(cfg.get("throttle_seconds", 10))

    def _build(self):
        ws = []
        for c in self.cfg.get("cameras", []):
            if c.get("enabled", True) is False:
                continue
            kind = c.get("kind", "onvif")
            if kind not in KINDS:
                log.error("unknown kind %s for %s", kind, c.get("key"))
                continue
            ws.append(KINDS[kind](self, c))
        for g in self.cfg.get("gpio", []):
            ws.append(GpioWorker(self, {**g, "kind": "gpio"}))
        return ws

    def emit(self, worker, channel, raw_type, target, snapshot_fn, raw=""):
        key, name = worker.key_for(channel)
        etype = norm(raw_type, target)
        k = (key, etype)
        now = time.time()
        if now - self.last.get(k, 0) < self.throttle:
            return  # cameras repeat the same alarm every second while it lasts
        self.last[k] = now
        ev = {"camera_key": key, "camera_name": name, "type": etype, "raw_type": str(raw_type or ""), "target": target,
              "state": "active", "ts": now, "raw": raw}
        if snapshot_fn:
            img, ext = snapshot_fn() or (None, None)
            if img:
                ev["image_b64"] = base64.b64encode(img).decode()
                ev["image_ext"] = ext or "jpg"
        log.info("[%s] %s (%s)", key, etype, raw_type)
        threading.Thread(target=self.up.send_event, args=(ev,), daemon=True).start()

    def heartbeat_loop(self):
        while True:
            cams = []
            for w in self.workers:
                online = w.connected if not isinstance(w, PushCamera) else (time.time() - getattr(w, "last", 0) < 3600 or getattr(w, "last", 0) == 0)
                for key, name in w.keys():
                    cams.append({"key": key, "name": name, "online": bool(online)})
            try:
                res = self.up.post("/api/edge/heartbeat", {"version": VERSION, "cameras": cams, "hostname": socket.gethostname()})
                self.up.online = True
                skew = abs(res.get("server_time", time.time()) - time.time())
                if skew > 10:
                    log.warning("clock differs from server by %.0fs: enable NTP (needed by ONVIF too)", skew)
                self.up.flush_spool()
            except Exception as e:  # noqa: BLE001
                self.up.online = False
                log.warning("heartbeat failed: %s", e)
            time.sleep(float(self.cfg.get("heartbeat_seconds", 30)))

    def run(self):
        if self.cfg.get("local_webhook_port"):
            WebhookHub(self, int(self.cfg["local_webhook_port"])).start()
        for w in self.workers:
            w.start()
        log.info("edge agent %s started with %d sources", VERSION, len(self.workers))
        self.heartbeat_loop()


# ------------------------------------------------------------------ ONVIF discovery (WS-Discovery, stdlib)
def discover(timeout=4):
    msg = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    s.sendto(msg.encode(), ("239.255.255.250", 3702))
    found = {}
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, (ip, _) = s.recvfrom(65535)
        except socket.timeout:
            break
        text = data.decode("utf-8", "replace")
        xaddr = re.search(r"XAddrs>([^<]+)<", text)
        scopes = re.search(r"Scopes>([^<]+)<", text)
        name = re.search(r"onvif://www\.onvif\.org/name/([^\s<]+)", text)
        hw = re.search(r"onvif://www\.onvif\.org/hardware/([^\s<]+)", text)
        found[ip] = {"ip": ip, "xaddr": xaddr.group(1).split()[0] if xaddr else "", "name": name.group(1) if name else "",
                     "hardware": hw.group(1) if hw else "", "scopes": scopes.group(1)[:200] if scopes else ""}
    return list(found.values())


def main():
    ap = argparse.ArgumentParser(description="Cam to Drone edge agent")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--discover", action="store_true", help="find ONVIF cameras on the LAN and exit")
    ap.add_argument("--test", action="store_true", help="send one test alarm per configured camera and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.discover:
        res = discover()
        if not res:
            print("Δεν βρέθηκαν ONVIF συσκευές (ελέγξτε ότι το ONVIF είναι ενεργό στις κάμερες).")
        for d in res:
            print(f"{d['ip']:16} {d['name'] or '-':24} {d['hardware'] or '-':20} {d['xaddr']}")
        return
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_path"] = a.config
    agent = Agent(cfg)
    if a.test:
        for w in agent.workers:
            key, _ = w.keys()[0]
            agent.throttle = 0
            agent.emit(w, None, "fielddetection", "human", None, raw="test from edge agent")
        time.sleep(3)
        return
    agent.run()


if __name__ == "__main__":
    main()
