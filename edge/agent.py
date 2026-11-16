#!/usr/bin/env python3
"""
Cam to Drone · edge agent (Raspberry Pi / any Linux box at the customer site).

Listens to the cameras of ONE site, translates their alarms (ONVIF, Hikvision ISAPI, Dahua, HTTP push, dry contacts)
into one common format and sends them to the central server over HTTPS (outbound only).

  python3 agent.py --config config.json          run
  python3 agent.py --discover                    find ONVIF cameras on the local network
  python3 agent.py --config config.json --test   send one test alarm per camera and exit
  python3 agent.py --config config.json --selftest   check that this file and the config load, then exit

Since 1.1: alarm clips (rolling recording with ffmpeg, a few seconds before and after the alarm), near-live
frames on request from the console, and self-update from the central server.

Python 3.8+. Standard library only, plus optional:  pip install onvif-zeep  (ONVIF cameras)  gpiozero (dry contacts)
and the ffmpeg program for clips (sudo apt install ffmpeg).
"""
import argparse
import base64
import email
import email.policy
import json
import logging
import os
import hashlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote

VERSION = "1.5.4"
log = logging.getLogger("edge")

FROZEN = getattr(sys, "frozen", False)  # Windows build (PyInstaller): no self-update, tools bundled next to the program
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}  # no console window for ffmpeg / go2rtc on Windows
for _d in (os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "bin"), os.path.join(APP_DIR, "bin")):
    if os.path.isdir(_d) and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

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


def redact(text):
    """Hide user:password@ in addresses before a message leaves the box (errors shown in the console)."""
    return re.sub(r"(\w+://)[^/@\s]*@", r"***@", str(text))


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

    def raw(self, path, data=None, headers=None, timeout=60):
        """POST bytes (or GET when data is None). Returns (body bytes, response headers)."""
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Authorization": f"Bearer {self.token}", "User-Agent": f"cam2drone-edge/{VERSION}", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx if self.base.startswith("https") else None) as r:
            return r.read(), r.headers

    def send_event(self, ev):
        try:
            res = self.post("/api/edge/event", ev)
            self.online = True
            log.info("sent %s %s -> %s", ev["camera_key"], ev["type"], res.get("incident_id") or res.get("suppressed") or "ok")
            return res
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

    def channel_of(self, key):
        """Inverse of key_for: camera key -> channel (or None for single cameras)."""
        if self.cam.get("channels") and key.startswith(self.cam["key"] + ":"):
            return key[len(self.cam["key"]) + 1:]
        return None

    def base(self):
        return f"http://{self.cam['host']}:{self.cam.get('port', 80)}"

    def opener(self):
        if not getattr(self, "_op", None):
            self._op = http_opener(self.cam.get("user", ""), self.cam.get("password", ""), self.cam.get("snapshot_url") or self.base())
        return self._op

    def snapshot_url(self, channel):
        return self.cam.get("snapshot_url")

    def snapshot(self, channel=None):
        url = self.snapshot_url(channel)
        if not url:
            return None, None
        if "{channel}" in url:
            url = url.replace("{channel}", str(channel or 1))
        return fetch(self.opener(), url)

    def default_rtsp(self, channel):
        return None

    def rtsp(self, channel=None):
        """RTSP address for recording clips. Config `rtsp` wins (may contain {channel}); prefer the camera's sub-stream."""
        url = self.cam.get("rtsp") or self.default_rtsp(channel)
        if not url:
            return None
        return url.replace("{channel}", str(channel or 1)).replace("{user}", quote(self.cam.get("user", ""), safe="")) \
                  .replace("{password}", quote(self.cam.get("password", ""), safe=""))

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

    def snapshot_url(self, channel):
        return self.cam.get("snapshot_url") or f"{self.base()}/ISAPI/Streaming/channels/{int(channel or self.cam.get('channel', 1))}01/picture"

    def default_rtsp(self, channel):
        return "rtsp://{user}:{password}@%s:%s/Streaming/Channels/{channel}02" % (self.cam["host"], self.cam.get("rtsp_port", 554))

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
                    self.agent.emit(self, ch, s.get("eventType"), s.get("targetType") or s.get("detectionTarget"),
                                    lambda ch=ch: self.snapshot(ch), raw=m.group(0)[:3000])


class DahuaWorker(Worker):
    """Dahua eventManager attach: multipart text stream with lines Code=...;action=Start;index=N"""

    def snapshot_url(self, channel):
        return self.cam.get("snapshot_url") or f"{self.base()}/cgi-bin/snapshot.cgi?channel={channel or 1}"

    def default_rtsp(self, channel):
        return "rtsp://{user}:{password}@%s:%s/cam/realmonitor?channel={channel}&subtype=1" % (self.cam["host"], self.cam.get("rtsp_port", 554))

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
                target = "human" if "human" in line.lower() else "vehicle" if "vehicle" in line.lower() else None
                self.agent.emit(self, ch, m.group(1), target, lambda ch=ch: self.snapshot(ch), raw=line[:2000])


class OnvifWorker(Worker):
    """ONVIF PullPoint subscription (needs: pip install onvif-zeep). Camera clock must be in sync (NTP)."""
    snap_uri = None
    stream_uri = None

    def snapshot_url(self, channel):
        return self.cam.get("snapshot_url") or self.snap_uri

    def default_rtsp(self, channel):
        if not self.stream_uri:
            return None
        # put the credentials into the address ONVIF gave us
        return re.sub(r"^rtsp://", "rtsp://{user}:{password}@", self.stream_uri, count=1) if "@" not in self.stream_uri else self.stream_uri

    def loop(self):
        try:
            from onvif import ONVIFCamera  # type: ignore
        except ImportError:
            raise RuntimeError("λείπει το πακέτο onvif-zeep: pip install onvif-zeep")
        from datetime import timedelta
        c = self.cam
        cam = ONVIFCamera(c["host"], c.get("port", 80), c["user"], c["password"])
        snap_uri = c.get("snapshot_url")
        try:
            media = cam.create_media_service()
            profiles = media.GetProfiles()
            if not snap_uri:
                snap_uri = media.GetSnapshotUri({"ProfileToken": profiles[0].token}).Uri
            sub = profiles[1] if len(profiles) > 1 else profiles[0]  # the second profile is usually the sub-stream
            self.stream_uri = media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                                                  "ProfileToken": sub.token}).Uri
        except Exception as e:  # noqa: BLE001
            log.info("[%s] ONVIF media service: %s", c["key"], e)
        self.snap_uri = snap_uri
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
        img = (lambda: image) if image else (lambda: w.snapshot(summary.get("channelID"))) if w.cam.get("snapshot_url") else None
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


class OnvifControl:
    """Camera control over ONVIF (needs onvif-zeep): device info, PTZ with presets, relay outputs (siren, light),
    day/night (IR cut filter) and reboot. Works for any camera with ONVIF: kind "onvif", or set "onvif": true
    (and "onvif_port" if it is not the web port) on Hikvision / Dahua / push cameras."""

    @staticmethod
    def wanted(w):
        c = w.cam
        return not c.get("channels") and c.get("host") and c.get("user") is not None and \
            (c.get("kind") == "onvif" or c.get("onvif") or c.get("onvif_port")) and c.get("onvif") is not False

    def __init__(self, cam):
        self.cam = cam
        self.lock = threading.Lock()
        self.c = None

    def connect(self):
        if self.c:
            return self.c
        try:
            from onvif import ONVIFCamera  # type: ignore
        except ImportError:
            raise RuntimeError("λείπει το πακέτο onvif-zeep")
        c = ONVIFCamera(self.cam["host"], int(self.cam.get("onvif_port") or self.cam.get("port", 80)), self.cam["user"], self.cam["password"])
        self.media = c.create_media_service()
        prof = self.media.GetProfiles()[0]
        self.profile = prof.token
        vsc = getattr(prof, "VideoSourceConfiguration", None)
        self.source = getattr(vsc, "SourceToken", None) if vsc else None
        self.c = c
        return c

    def _ptz(self):
        if not getattr(self, "ptz", None):
            self.ptz = self.connect().create_ptz_service()
        return self.ptz

    def _imaging(self):
        if not getattr(self, "imaging", None):
            self.imaging = self.connect().create_imaging_service()
        return self.imaging

    def capabilities(self):
        with self.lock:
            c = self.connect()
            out = {"onvif": True}
            try:
                i = c.devicemgmt.GetDeviceInformation()
                out["info"] = {"manufacturer": str(i.Manufacturer), "model": str(i.Model), "firmware": str(i.FirmwareVersion)}
            except Exception:  # noqa: BLE001
                pass
            try:
                presets = self._ptz().GetPresets({"ProfileToken": self.profile}) or []
                out["ptz"] = True
                out["presets"] = [{"token": str(p.token), "name": str(getattr(p, "Name", "") or p.token)} for p in presets][:50]
            except Exception:  # noqa: BLE001
                self.ptz = None
            try:
                relays = c.devicemgmt.GetRelayOutputs() or []
                if relays:
                    out["relays"] = [{"token": str(r.token), "name": self.cam.get("relay_names", {}).get(str(r.token), f"Έξοδος {k + 1}")} for k, r in enumerate(relays)]
            except Exception:  # noqa: BLE001
                pass
            try:
                if self.source:
                    s = self._imaging().GetImagingSettings({"VideoSourceToken": self.source})
                    if getattr(s, "IrCutFilter", None) is not None:
                        out["ircut"] = str(s.IrCutFilter)
            except Exception:  # noqa: BLE001
                pass
            return out

    def run(self, cmd, a):
        if cmd == "caps":
            return {"caps": self.capabilities()}
        with self.lock:
            c = self.connect()
            if cmd == "ptz_move":
                vel = {"PanTilt": {"x": float(a.get("pan", 0)), "y": float(a.get("tilt", 0))}, "Zoom": {"x": float(a.get("zoom", 0))}}
                self._ptz().ContinuousMove({"ProfileToken": self.profile, "Velocity": vel})
                time.sleep(float(a.get("seconds", 0.6)))
                self._ptz().Stop({"ProfileToken": self.profile, "PanTilt": True, "Zoom": True})
            elif cmd == "ptz_stop":
                self._ptz().Stop({"ProfileToken": self.profile, "PanTilt": True, "Zoom": True})
            elif cmd == "ptz_home":
                self._ptz().GotoHomePosition({"ProfileToken": self.profile})
            elif cmd == "ptz_preset":
                self._ptz().GotoPreset({"ProfileToken": self.profile, "PresetToken": a["token"]})
            elif cmd == "ptz_set_preset":
                tok = self._ptz().SetPreset({"ProfileToken": self.profile, "PresetName": a["name"]})
                return {"token": str(tok)}
            elif cmd == "relay":
                c.devicemgmt.SetRelayOutputState({"RelayOutputToken": a["token"], "LogicalState": a["state"]})
            elif cmd == "ircut":
                if not self.source:
                    raise RuntimeError("άγνωστη πηγή εικόνας")
                self._imaging().SetImagingSettings({"VideoSourceToken": self.source, "ImagingSettings": {"IrCutFilter": a["mode"]},
                                                    "ForcePersistence": True})
            elif cmd == "reboot":
                c.devicemgmt.SystemReboot()
                self.c = self.ptz = self.imaging = None
            else:
                raise RuntimeError(f"άγνωστη εντολή {cmd}")
            return {}


# ------------------------------------------------------------------ real-time video without a media server
class Go2rtc:
    """go2rtc (github.com/AlexxIT/go2rtc, MIT licence) runs next to the agent and turns each camera's RTSP stream into
    WebRTC. The console's offer and go2rtc's answer travel through the central server as a camera command ("webrtc"),
    so nothing on this site listens on the internet; the video then flows directly to the browser (peer to peer, STUN).
    Active only when the go2rtc program is found (next to the agent, in bin/, or on PATH)."""
    API = "http://127.0.0.1:1984"

    def __init__(self, agent, data_dir):
        self.agent, self.dir = agent, os.path.join(data_dir, "go2rtc")
        self.bin = shutil.which("go2rtc")
        self.proc = None
        self.added = {}
        self.lock = threading.Lock()

    @staticmethod
    def name(key):
        return re.sub(r"[^\w-]", "_", key)

    def ready(self):
        return bool(self.proc and self.proc.poll() is None)

    def start(self):
        if not self.bin:
            return False
        os.makedirs(self.dir, exist_ok=True)
        cfg = {"api": {"listen": self.API.split("//")[1]}, "rtsp": {"listen": "127.0.0.1:8554"}, "srtp": {"listen": ""},  # local only: the ffmpeg sources publish through it
               "webrtc": {"listen": ":8555", "candidates": ["stun:8555"],
                          "ice_servers": [{"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302"]}]},
               "ffmpeg": {"bin": shutil.which("ffmpeg") or "ffmpeg"}, "log": {"level": "warn"}}
        if os.name == "nt":  # a go2rtc / ffmpeg left over from an earlier run keeps the port, or a camera connection
            subprocess.run(["taskkill", "/F", "/IM", os.path.basename(self.bin)], capture_output=True, **NOWIN)
            if FROZEN:  # cameras accept few connections ("453 Not Enough Bandwidth"); this computer runs ffmpeg only for us
                subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True, **NOWIN)
        path = os.path.join(self.dir, "go2rtc.yaml")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)  # JSON is valid YAML
        threading.Thread(target=self.keep, args=(path,), daemon=True, name="go2rtc").start()
        for _ in range(50):
            if self.ready():
                break
            time.sleep(0.1)
        return True

    def keep(self, path):
        while True:
            self.added = {}
            with open(os.path.join(self.dir, "go2rtc.log"), "ab") as logf:
                self.proc = subprocess.Popen([self.bin, "-config", path], stdout=logf, stderr=logf, cwd=self.dir, **NOWIN)
                log.info("go2rtc started (real-time video)")
                self.proc.wait()
            log.warning("go2rtc stopped (%s), restarting in 5s", self.proc.returncode)
            time.sleep(5)

    def _req(self, method, path, data=None, ctype=None, timeout=10):
        req = urllib.request.Request(self.API + path, data=data, method=method, headers={"Content-Type": ctype} if ctype else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:  # go2rtc says why (camera refused, codec...): pass it on to the console
            why = e.read()[:300].decode("utf-8", "replace").strip()
            raise RuntimeError(redact(f"go2rtc {e.code} {path.split('?')[0]}: {why or e.reason}")) from None

    def ensure(self, key):
        w = self.agent.by_key(key)
        url = w and w.rtsp(w.channel_of(key))
        if not url:
            raise RuntimeError("δεν υπάρχει διεύθυνση RTSP για την κάμερα")
        n = self.name(key)
        with self.lock:
            if self.added.get(n) != url:
                if shutil.which("ffmpeg"):
                    # ffmpeg reads the camera (the same way the clips do, which works with cameras go2rtc's own RTSP client
                    # cannot handle): video as is, sound to Opus; the second source is used only for H.265 cameras
                    # H.265 cameras: the H.264 copy is made from the connection already open (cameras refuse a second one)
                    srcs = [f"ffmpeg:{url}#video=copy#audio=opus", f"ffmpeg:{n}#video=h264"]
                else:
                    srcs = [url + "#backchannel=0"]  # no two-way audio request: many cameras refuse it
                q = "name=" + quote(n, safe="") + "".join("&src=" + quote(x, safe="") for x in srcs)
                self._req("PUT", "/api/streams?" + q)
                self.added[n] = url
        return n

    def local_url(self, key):
        """go2rtc's own RTSP address for this camera (this computer only). The clip recorder reads from here, so the camera
        sees a single connection; many cameras refuse a second one ("453 Not Enough Bandwidth")."""
        return f"rtsp://127.0.0.1:8554/{self.ensure(key)}"

    def answer(self, key, sdp):
        if not sdp.startswith("v=0"):
            raise RuntimeError("άκυρο αίτημα βίντεο")
        n = self.ensure(key)
        return self._req("POST", "/api/webrtc?src=" + quote(n, safe=""), sdp.encode(), "application/sdp", timeout=20).decode()

    def watched(self):
        """Stream names someone is watching right now (go2rtc consumers)."""
        try:
            st = json.loads(self._req("GET", "/api/streams", timeout=2) or b"{}")
            return {n for n, v in (st or {}).items() if v and v.get("consumers")}
        except Exception:  # noqa: BLE001
            return set()


# ------------------------------------------------------------------ agent
class Recorder(threading.Thread):
    """Keeps the last few seconds of one camera in short MPEG-TS segments (ffmpeg, stream copy, no re-encoding),
    so a clip from before the alarm exists. Segments live in RAM (/dev/shm) by default to spare the SD card."""
    SEGMENT = 2  # seconds; ffmpeg can only cut at key frames, so real segments follow the camera's I-frame interval

    def __init__(self, agent, worker, key, channel, root, keep):
        super().__init__(daemon=True, name=f"rec-{key}")
        self.agent, self.worker, self.key, self.channel = agent, worker, key, channel
        self.dir = os.path.join(root, re.sub(r"[^\w.-]", "_", key))
        self.keep = keep
        os.makedirs(self.dir, exist_ok=True)

    def run(self):
        backoff = 5
        while True:
            url = self.agent.record_source(self.worker, self.key, self.channel)
            if not url:
                time.sleep(15)  # ONVIF: the address is known after the first connection; go2rtc may be restarting
                continue
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url, "-map", "0:v:0", "-c", "copy",
                   "-f", "segment", "-segment_time", str(self.SEGMENT), "-segment_wrap", str(self.keep),
                   os.path.join(self.dir, "seg%03d.ts")]
            t0 = time.time()
            try:
                p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=None, **NOWIN)
                err = p.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if p.stderr else []
                log.warning("[%s] recording stopped (%s), restarting in %ss", self.key, err[0][:200] if err else p.returncode, backoff)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] recording failed: %s", self.key, e)
            backoff = 5 if time.time() - t0 > 60 else min(120, backoff * 2)
            time.sleep(backoff)

    def clip(self, t_alarm, pre, post):
        """Builds an MP4 covering [alarm - pre, alarm + post]. Called post+3 seconds after the alarm.
        A segment's modification time is when it ended, so it covers [mtime - SEGMENT, mtime]."""
        segs = []
        for n in os.listdir(self.dir):
            if n.endswith(".ts"):
                f = os.path.join(self.dir, n)
                mt = os.path.getmtime(f)
                if t_alarm - pre <= mt <= t_alarm + post + self.SEGMENT:
                    segs.append((mt, f))
        if not segs:
            return None
        segs.sort()
        # trim to the requested window (stream copy starts at the key frame just before, so nothing is lost)
        offset = max(0.0, (t_alarm - pre) - (segs[0][0] - self.SEGMENT))
        tmp = tempfile.mkdtemp(prefix="clip-")
        try:
            joined = os.path.join(tmp, "in.ts")
            with open(joined, "wb") as out:
                for _, f in segs:
                    with open(f, "rb") as src:
                        shutil.copyfileobj(src, out)  # MPEG-TS segments can be joined byte by byte
            mp4 = os.path.join(tmp, "clip.mp4")
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-fflags", "+genpts", "-ss", f"{offset:.2f}", "-i", joined,
                            "-t", f"{pre + post:.2f}", "-c", "copy", "-movflags", "+faststart", mp4], check=True, timeout=60, **NOWIN)
            with open(mp4, "rb") as f:
                return f.read()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] clip failed: %s", self.key, e)
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.up = Uplink(cfg)
        self.workers = self._build()
        self.last = {}
        self.throttle = float(cfg.get("throttle_seconds", 10))
        self.fast = False
        self.live = set()
        self.stream_procs = {}
        self.ffmpeg_warned = False
        self.controls = {}  # camera key -> OnvifControl
        self.caps = {}      # camera key -> capabilities reported to the server
        self.recorders = {}
        self.update_failed = None
        self.g2r = Go2rtc(self, os.path.dirname(os.path.abspath(self.up.spool))) if cfg.get("webrtc", True) else None
        clips = cfg.get("clips") or {}
        self.clip_pre = float(clips.get("pre_seconds", 10))
        self.clip_post = float(clips.get("post_seconds", 10))
        self.clips_on = bool(clips.get("enabled", False))

    def by_key(self, key):
        for w in self.workers:
            for k, _ in w.keys():
                if k == key:
                    return w
        return None

    def _recorders(self):
        if not self.clips_on:
            return
        if FROZEN and self.g2r and self.g2r.bin and not (self.cfg.get("clips") or {}).get("force"):
            # many cameras give a single connection per stream ("453 Not Enough Bandwidth"): keep it for live video
            log.info("clips off on this computer (set clips.force to record them)")
            return
        if not shutil.which("ffmpeg"):
            log.error("clips are enabled but ffmpeg is missing (sudo apt install ffmpeg)")
            return
        clips = self.cfg.get("clips") or {}
        root = clips.get("dir") or ("/dev/shm/cam2drone" if os.path.isdir("/dev/shm") else os.path.join(self.up.spool, "..", "ring"))
        keep = int((self.clip_pre + self.clip_post) / Recorder.SEGMENT) + 6
        for w in self.workers:
            if isinstance(w, GpioWorker) or w.cam.get("record") is False or (isinstance(w, PushCamera) and not w.cam.get("rtsp")):
                continue
            for key, _ in w.keys():
                r = Recorder(self, w, key, w.channel_of(key), root, keep)
                self.recorders[key] = r
                r.start()
        log.info("recording %d camera(s) for clips (%gs before, %gs after the alarm)", len(self.recorders), self.clip_pre, self.clip_post)

    def record_source(self, worker, key, channel):
        """Where the clip recorder reads the camera: through go2rtc when it runs (one connection to the camera shared
        with live view), otherwise straight from the camera."""
        if self.g2r and self.g2r.bin:
            if not self.g2r.ready():
                return None
            try:
                return self.g2r.local_url(key)
            except Exception as e:  # noqa: BLE001
                log.info("[%s] recording through go2rtc: %s", key, e)
                return None
        return worker.rtsp(channel)

    def send_clip(self, key, t_alarm):
        time.sleep(self.clip_post + 2 * Recorder.SEGMENT + 1)  # the last segment must be fully written
        data = self.recorders[key].clip(t_alarm, self.clip_pre, self.clip_post)
        if not data:
            return
        for attempt in range(4):
            try:
                self.up.raw("/api/edge/clip", data, {"Content-Type": "video/mp4", "X-Camera-Key": key, "X-Ts": f"{t_alarm:.3f}"}, timeout=120)
                log.info("[%s] clip sent (%d KB)", key, len(data) // 1024)
                return
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 404, 413, 501):
                    log.info("[%s] server did not keep the clip (%s)", key, e.code)
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] clip upload failed: %s", key, e)
            time.sleep(15 * (attempt + 1))

    def frame_loop(self):
        """While the console asks for live view, send about one snapshot per second per camera."""
        while True:
            keys = list(self.live)
            if not keys:
                time.sleep(1)
                continue
            t0 = time.time()
            watched = self.g2r.watched() if self.g2r and self.g2r.ready() else set()
            for key in keys:
                w = self.by_key(key)
                if not w or self.streaming(key) or Go2rtc.name(key) in watched:
                    continue  # a real video stream is running for this camera, no snapshots needed
                img, ext = w.snapshot(w.channel_of(key))
                if img:
                    try:
                        self.up.raw("/api/edge/frame", img, {"Content-Type": "image/png" if ext == "png" else "image/jpeg", "X-Camera-Key": key}, timeout=10)
                    except Exception as e:  # noqa: BLE001
                        log.debug("frame %s: %s", key, e)
            time.sleep(max(0.2, 1 - (time.time() - t0)))

    # ---- live video + audio to the media server (only when the central server has one configured)
    def streaming(self, key):
        p = self.stream_procs.get(key)
        return bool(p and p[0].poll() is None)

    @staticmethod
    def stream_cmd(src, dest):
        """Video is copied as is (use the camera's H.264 sub-stream); audio, if the camera has it, is converted
        to what the target accepts: Opus for RTSP/WebRTC servers, AAC for RTMP/SRT."""
        inp = ["-rtsp_transport", "tcp"] if src.startswith("rtsp") else []
        if dest.startswith(("rtmp://", "rtmps://")):
            out = ["-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-f", "flv"]
        elif dest.startswith("srt://"):
            out = ["-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-f", "mpegts"]
        else:
            out = ["-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", "-f", "rtsp", "-rtsp_transport", "tcp"]
        return ["ffmpeg", "-nostdin", "-loglevel", "error", *inp, "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *out, dest]

    def sync_streams(self, wanted):
        if wanted and not shutil.which("ffmpeg"):
            if not self.ffmpeg_warned:
                log.error("live video needs ffmpeg (sudo apt install ffmpeg); sending snapshots instead")
                self.ffmpeg_warned = True
            return
        for key, (p, _) in list(self.stream_procs.items()):
            if key not in wanted:
                p.terminate()
                del self.stream_procs[key]
                log.info("[%s] live stream stopped", key)
        for key, dest in wanted.items():
            cur = self.stream_procs.get(key)
            if cur and cur[0].poll() is None:
                continue
            if cur and time.time() - cur[1] < 5:
                continue  # it just failed; do not hammer the camera
            w = self.by_key(key)
            src = w and w.rtsp(w.channel_of(key))
            if not src:
                log.warning("[%s] no RTSP address for live video (set \"rtsp\" in config.json); sending snapshots", key)
                continue
            self.stream_procs[key] = (subprocess.Popen(self.stream_cmd(src, dest), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NOWIN), time.time())
            log.info("[%s] live stream started", key)

    def apply(self, res):
        self.fast = bool(res.get("fast"))
        live = set(res.get("live") or [])
        if live != self.live:
            log.info("live view: %s", ", ".join(sorted(live)) or "off")
        self.live = live
        self.sync_streams(res.get("streams") or {})
        for c in res.get("commands") or []:
            threading.Thread(target=self.run_command, args=(c,), daemon=True, name=f"cmd-{c.get('id')}").start()
        ag = res.get("agent") or {}
        auto = self.cfg.get("auto_update", True)
        if ag.get("version") and ag.get("sha256") and ag["sha256"] != self.update_failed and (not FROZEN or RESTART_CMD):
            newer = version_tuple(ag["version"]) > version_tuple(VERSION)
            if (res.get("update") and ag["version"] != VERSION) or (auto and newer):
                self.self_update(ag)

    def self_update(self, ag):
        """Downloads agent.py from the server, checks the hash and that it starts, then replaces itself and restarts.
        Windows program: saves it next to the program (which loads it instead of its built-in copy) and restarts."""
        me = os.path.abspath(__file__)
        log.info("updating agent %s -> %s", VERSION, ag["version"])
        try:
            data, headers = self.up.raw("/api/edge/agent", timeout=60)
            digest = hashlib.sha256(data).hexdigest()
            if digest != ag["sha256"] or headers.get("X-Agent-Sha256", digest) != digest:
                raise RuntimeError("hash mismatch")
            if FROZEN:
                compile(data, "agent.py", "exec")  # at least valid Python; the program falls back to its own copy if it fails to start
                dest = os.path.join(APP_DIR, "agent.py")
                with open(dest + ".new", "wb") as f:
                    f.write(data)
                os.replace(dest + ".new", dest)
                log.info("update saved, restarting")
                restart_windows()
                return
            fd, tmp = tempfile.mkstemp(prefix="agent-", suffix=".py", dir=os.path.dirname(me))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            test = subprocess.run([sys.executable, tmp, "--config", self.cfg["_path"], "--selftest"], capture_output=True, timeout=60)
            if test.returncode != 0:
                os.remove(tmp)
                raise RuntimeError("new version failed its self-test: " + test.stderr.decode("utf-8", "replace")[-300:])
            shutil.copy2(me, me + ".bak")
            os.chmod(tmp, 0o755)
            os.replace(tmp, me)
        except Exception as e:  # noqa: BLE001
            self.update_failed = ag["sha256"]
            log.error("update failed, staying on %s: %s", VERSION, e)
            return
        log.info("update installed, restarting")
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, me] + sys.argv[1:])

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
        if key in self.recorders:
            threading.Thread(target=self.send_clip, args=(key, now), daemon=True).start()

    # ---- camera control over ONVIF (commands from the console)
    def _controls(self):
        for w in self.workers:
            if OnvifControl.wanted(w):
                key = w.keys()[0][0]
                self.controls[key] = OnvifControl(w.cam)
        if self.controls:
            threading.Thread(target=self.caps_loop, daemon=True, name="onvif-caps").start()

    def caps_loop(self):
        """Reads what each camera supports at start and every 10 minutes; reported with the heartbeat."""
        while True:
            for key, ctl in self.controls.items():
                try:
                    self.caps[key] = ctl.capabilities()
                except Exception as e:  # noqa: BLE001
                    log.info("[%s] ONVIF capabilities: %s", key, e)
            time.sleep(600)

    def run_command(self, c):
        key, cmd, args = c.get("camera_key"), c.get("cmd"), c.get("args") or {}
        ctl = self.controls.get(key)
        try:
            if cmd == "webrtc":
                if not (self.g2r and self.g2r.ready()):
                    raise RuntimeError("το κουτί δεν έχει ζωντανό βίντεο (λείπει το go2rtc)")
                result = {"sdp": self.g2r.answer(key, str(args.get("sdp") or ""))}
            elif not ctl:
                raise RuntimeError("η κάμερα δεν ελέγχεται μέσω ONVIF από αυτό το κουτί")
            else:
                result = ctl.run(cmd, args)
                if cmd in ("caps", "ptz_set_preset"):
                    self.caps[key] = result["caps"] if cmd == "caps" else ctl.capabilities()
                    result = {**(result or {}), "caps": self.caps_for(key)}
                elif cmd == "ircut" and key in self.caps:  # report the new day/night mode right away
                    self.caps[key] = {**self.caps[key], "ircut": args.get("mode")}
                    result = {**(result or {}), "caps": self.caps_for(key)}
            body = {"id": c["id"], "ok": True, "result": result}
            log.info("[%s] command %s ok", key, cmd)
        except Exception as e:  # noqa: BLE001
            body = {"id": c["id"], "ok": False, "error": redact(e)[:300]}
            log.warning("[%s] command %s failed: %s", key, cmd, e)
        try:
            self.up.post("/api/edge/command", body, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("command result not delivered: %s", e)

    def caps_for(self, key):
        """What the console may ask of this camera: ONVIF control plus real-time video when go2rtc runs."""
        caps = dict(self.caps.get(key) or {})
        w = self.by_key(key)
        if self.g2r and self.g2r.ready() and w and not isinstance(w, GpioWorker) and w.rtsp(w.channel_of(key)):
            caps["webrtc"] = True
        return caps

    def heartbeat(self):
        cams = []
        for w in self.workers:
            online = w.connected if not isinstance(w, PushCamera) else (time.time() - getattr(w, "last", 0) < 3600 or getattr(w, "last", 0) == 0)
            for key, name in w.keys():
                caps = self.caps_for(key)
                cams.append({"key": key, "name": name, "online": bool(online), **({"caps": caps} if caps else {})})
        res = self.up.post("/api/edge/heartbeat", {"version": VERSION, "cameras": cams, "hostname": socket.gethostname()})
        self.up.online = True
        skew = abs(res.get("server_time", time.time()) - time.time())
        if skew > 10:
            log.warning("clock differs from server by %.0fs: enable NTP (needed by ONVIF too)", skew)
        self.up.flush_spool()
        return res

    def control_loop(self):
        """Heartbeat every 30s. Between them a light poll: every 2s while the site has an open incident, someone watches
        live or sends camera commands; otherwise every 10s (idle_poll_seconds), so a command reaches the camera quickly."""
        every = float(self.cfg.get("heartbeat_seconds", 30))
        idle = float(self.cfg.get("idle_poll_seconds", 10))
        last = last_poll = 0
        while True:
            try:
                if time.time() - last >= every:
                    res = self.heartbeat()
                    last = last_poll = time.time()
                elif self.fast or time.time() - last_poll >= idle:
                    res = self.up.post("/api/edge/poll", {}, timeout=10)
                    last_poll = time.time()
                else:
                    res = None
                if res:
                    self.apply(res)
            except Exception as e:  # noqa: BLE001
                self.up.online = False
                self.fast = False
                last = time.time()
                log.warning("server unreachable: %s", e)
            time.sleep(2)

    def run(self):
        if self.cfg.get("local_webhook_port"):
            WebhookHub(self, int(self.cfg["local_webhook_port"])).start()
        for w in self.workers:
            w.start()
        if self.g2r and not self.g2r.start():  # before the recorders, which read the cameras through it
            log.info("go2rtc not found: live view sends about one picture per second")
        self._recorders()
        self._controls()
        threading.Thread(target=self.frame_loop, daemon=True, name="frames").start()
        log.info("edge agent %s started with %d sources", VERSION, len(self.workers))
        self.control_loop()


RESTART_CMD = None  # set by the Windows program: how to start the agent again


def restart_windows():
    """Ends this agent's ffmpeg / go2rtc, starts a new agent (which waits for this one to exit) and exits."""
    ps = f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={os.getpid()}' | ForEach-Object {{ taskkill /F /T /PID $_.ProcessId }}"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=60, **NOWIN)
    # PYINSTALLER_RESET_ENVIRONMENT: the new program must unpack itself afresh, not reuse this one's folder (deleted on exit)
    subprocess.Popen(RESTART_CMD, close_fds=True, cwd=APP_DIR, creationflags=0x00000008 | 0x00000200 | 0x08000000,
                     env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    logging.shutdown()
    os._exit(0)


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


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
    ap.add_argument("--selftest", action="store_true", help="load the config and build the agent, then exit (used by self-update)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log", help="write the log to this file (kept under 3 x 2 MB) instead of the console")
    a = ap.parse_args()
    handlers = None
    if a.log:
        from logging.handlers import RotatingFileHandler
        handlers = [RotatingFileHandler(a.log, maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
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
    if a.selftest:
        print(f"ok {VERSION} · {len(agent.workers)} sources")
        return
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
