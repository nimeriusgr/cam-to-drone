"""
Cam to Drone · Windows setup and runner for the edge agent (laptop / mini PC on the same network as the cameras).

  Cam2Drone-Setup.exe            window: connect to the centre, find the cameras, install, start
  Cam2DroneEdge.exe --run        the agent itself (started at logon, runs in the background)

Installs per user (no administrator rights): %LOCALAPPDATA%\\Cam2Drone, autostart through HKCU\\...\\Run.
The agent code is edge/agent.py, unchanged; go2rtc.exe and ffmpeg.exe are bundled in bin/.
"""
import concurrent.futures
import ctypes
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agent as BUILTIN  # noqa: E402  (edge/agent.py, the copy inside this program)

APP = "Cam2Drone"
HOME = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP)
CUSTOM_HOME = "--home" in sys.argv[1:-1]  # another install folder (tests)
if CUSTOM_HOME:
    HOME = os.path.abspath(sys.argv[sys.argv.index("--home") + 1])


def load_agent():
    """The agent sent by the centre ("Ενημέρωση" button) is saved as HOME\agent.py; use it when it is newer."""
    import importlib.util
    path = os.path.join(HOME, "agent.py")
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M)
        if m and BUILTIN.version_tuple(m.group(1)) > BUILTIN.version_tuple(BUILTIN.VERSION):
            spec = importlib.util.spec_from_file_location("agent_update", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    except Exception:  # noqa: BLE001  (missing or broken: the built-in copy runs)
        pass
    return BUILTIN


A = load_agent()
EXE = os.path.join(HOME, "Cam2DroneEdge.exe")
CONFIG = os.path.join(HOME, "config.json")
LOG = os.path.join(HOME, "agent.log")
PIDFILE = os.path.join(HOME, "agent.pid")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LOCK_PORT = 47811
DEFAULT_SERVER = "https://cam2drone.pages.dev"
NOWIN = 0x08000000


# ------------------------------------------------------------------ background agent
def run_agent():
    lock = socket.socket()
    for k in range(40):  # one agent per computer; after an update the previous one needs a moment to exit
        try:
            lock.bind(("127.0.0.1", LOCK_PORT))
            break
        except OSError:
            if k == 39:
                return
            time.sleep(0.5)
    os.makedirs(HOME, exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    sys.argv = [sys.argv[0], "--config", CONFIG, "--log", LOG]
    A.RESTART_CMD = [EXE, "--run"] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__), "--run"]
    if CUSTOM_HOME:
        A.RESTART_CMD += ["--home", HOME]
    try:
        A.main()
    except Exception:  # noqa: BLE001
        if A is BUILTIN:
            raise
        # the updated agent does not start: put it aside and run the built-in one
        try:
            os.replace(os.path.join(HOME, "agent.py"), os.path.join(HOME, "agent.py.bad"))
        except OSError:
            pass
        BUILTIN.RESTART_CMD = A.RESTART_CMD
        BUILTIN.log.exception("updated agent failed, running the built-in %s", BUILTIN.VERSION)
        BUILTIN.main()


def running_pid():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return None
    except OSError:
        try:
            return int(open(PIDFILE).read().strip())
        except (OSError, ValueError):
            return -1
    finally:
        s.close()


def stop_agent():
    pid = running_pid()
    if pid and pid > 0:  # /T also ends go2rtc and ffmpeg started by the agent
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)
    # the one-file program runs as two processes (launcher + agent): end every copy of the installed program,
    # otherwise the launcher keeps the file locked and the new version cannot replace it
    # (not this window itself, nor its own launcher, when it was opened from the installed copy)
    subprocess.run(["taskkill", "/F", "/IM", os.path.basename(EXE), "/FI", f"PID ne {os.getpid()}", "/FI", f"PID ne {os.getppid()}"],
                   capture_output=True, creationflags=NOWIN)
    for _ in range(30):
        if not running_pid():
            break
        time.sleep(0.2)


def start_agent():
    subprocess.Popen([EXE, "--run"], cwd=HOME, close_fds=True, env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                     creationflags=0x00000008 | 0x00000200 | NOWIN)  # detached, own process group, no window


def set_autostart(on=True):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "Cam2DroneEdge", 0, winreg.REG_SZ, f'"{EXE}" --run')
        else:
            try:
                winreg.DeleteValue(k, "Cam2DroneEdge")
            except OSError:
                pass


def install(cfg):
    """Copies this program to %LOCALAPPDATA%\\Cam2Drone, writes the configuration, autostart, (re)starts the agent."""
    os.makedirs(HOME, exist_ok=True)
    stop_agent()
    me = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False) and os.path.normcase(me) != os.path.normcase(EXE):
        for k in range(20):
            try:
                shutil.copy2(me, EXE)
                break
            except PermissionError:  # the old agent may need a moment to exit
                if k == 19:
                    raise RuntimeError("Δεν ήταν δυνατή η αντικατάσταση της παλιάς έκδοσης. Κλείστε το Cam2DroneEdge από τη "
                                       "Διαχείριση Εργασιών (Ctrl+Shift+Esc) και πατήστε ξανά «Εγκατάσταση και εκκίνηση».")
                time.sleep(0.5)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)
    if getattr(sys, "frozen", False):
        set_autostart(True)
        start_agent()


def uninstall():
    stop_agent()
    set_autostart(False)


# ------------------------------------------------------------------ talking to the centre
def check_token(server, token):
    """True if the centre accepts this box token; raises with a readable message otherwise."""
    req = urllib.request.Request(server.rstrip("/") + "/api/edge/heartbeat", method="POST",
                                 data=json.dumps({"version": A.VERSION, "cameras": [], "hostname": socket.gethostname()}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", "User-Agent": f"cam2drone-setup/{A.VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("Το κέντρο δεν αναγνωρίζει αυτόν τον κωδικό κουτιού.")
        raise RuntimeError(f"Το κέντρο απάντησε με σφάλμα {e.code}.")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Δεν υπάρχει σύνδεση με το κέντρο ({e}).")


# ------------------------------------------------------------------ finding cameras
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is sent; picks the interface that goes to the internet
        return s.getsockname()[0]
    except OSError:
        return "192.168.1.2"
    finally:
        s.close()


def sadp(timeout=3.0):
    """Hikvision devices answer SADP even when ONVIF is off."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 37020))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton("239.255.255.250"), socket.inet_aton(local_ip())))
        s.settimeout(0.5)
        msg = f'<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{str(uuid.uuid4()).upper()}</Uuid><Types>inquiry</Types></Probe>'
        s.sendto(msg.encode(), ("239.255.255.250", 37020))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _ = s.recvfrom(65535)
            except socket.timeout:
                continue
            t = data.decode("utf-8", "replace")
            ip = re.search(r"<IPv4Address>([\d.]+)<", t)
            if ip and "<ProbeMatch" in t:
                g = lambda k: (re.search(rf"<{k}>([^<]*)<", t) or [None, ""])[1]  # noqa: E731
                found[ip.group(1)] = {"ip": ip.group(1), "brand": "Hikvision", "model": g("DeviceDescription") or g("DeviceType"),
                                      "activated": g("Activated") != "false"}
    except OSError:
        pass
    finally:
        s.close()
    return found


def port_scan(ports=(554, 8000, 37777), timeout=0.5):
    """Devices on the local /24 with a camera port open (RTSP 554, Hikvision 8000, Dahua 37777)."""
    base = local_ip().rsplit(".", 1)[0]

    def probe(ip):
        for p in ports:
            try:
                with socket.create_connection((ip, p), timeout=timeout):
                    return ip
            except OSError:
                pass
        return None

    with concurrent.futures.ThreadPoolExecutor(96) as ex:
        return {ip for ip in ex.map(probe, [f"{base}.{n}" for n in range(1, 255)]) if ip}


def _get(op, url, timeout=5):
    try:
        with op.open(url, timeout=timeout) as r:
            return r.status, r.read(400_000)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:  # noqa: BLE001
        return 0, b""


def identify(ip, user, password):
    """What is at this address, and whether it gives a video connection right now. Cameras allow a limited number of
    video connections; recorders and viewing software at the site can use them all up. The installer sees it here,
    with the addresses that hold them, while still on site."""
    d = _identify(ip, user, password)
    if d.get("ok") and d.get("kind") in ("hikvision", "dahua") and hasattr(A, "probe_stream"):
        ch = next(iter(d.get("channels") or {}), "1")
        u, pw = quote(user, safe=""), quote(password, safe="")
        url = (f"rtsp://{u}:{pw}@{ip}:554/Streaming/Channels/{ch}02" if d["kind"] == "hikvision"
               else f"rtsp://{u}:{pw}@{ip}:554/cam/realmonitor?channel={ch}&subtype=1")
        state, text = A.probe_stream(url)
        if state == "ok":
            d["note"] = "Εντάξει · βίντεο " + text
        elif state == "refused":
            who = A.hik_sessions(A.http_opener(user, password, f"http://{ip}"), f"http://{ip}") if d["kind"] == "hikvision" else None
            d["warn"] = A.refusal_text(text) + (" · " + who[1] if who else "")
            d["note"] = "Χωρίς ζωντανό βίντεο: " + d["warn"]
    return d


def _identify(ip, user, password):
    """What is at this address: Hikvision / Dahua camera or recorder (with its channels), or a generic ONVIF camera."""
    base = f"http://{ip}"
    op = A.http_opener(user, password, base)
    tag = lambda body, k: (re.search(rf"<{k}>([^<]*)</{k}>", body) or [None, ""])[1].strip()  # noqa: E731
    code, body = _get(op, base + "/ISAPI/System/deviceInfo")
    if code == 200 and b"DeviceInfo" in body:
        t = body.decode("utf-8", "replace")
        d = {"ip": ip, "kind": "hikvision", "brand": "Hikvision", "model": tag(t, "model"), "name": tag(t, "deviceName"), "ok": True}
        dtype = tag(t, "deviceType").upper()
        if any(x in dtype for x in ("NVR", "DVR")) or re.match(r"(DS-7|DS-8|DS-9|IDS-7|IDS-9)", d["model"]):
            c2, b2 = _get(op, base + "/ISAPI/ContentMgmt/InputProxy/channels")
            chans = {}
            if c2 == 200:
                for blk in re.findall(r"<InputProxyChannel[^>]*>(.*?)</InputProxyChannel>", b2.decode("utf-8", "replace"), re.S):
                    cid = tag(blk, "id")
                    if cid:
                        chans[cid] = tag(blk, "name") or f"Κανάλι {cid}"
            if not chans:  # analogue DVR inputs
                c3, b3 = _get(op, base + "/ISAPI/System/Video/inputs/channels")
                for blk in re.findall(r"<VideoInputChannel[^>]*>(.*?)</VideoInputChannel>", b3.decode("utf-8", "replace"), re.S):
                    cid = tag(blk, "id")
                    if cid:
                        chans[cid] = tag(blk, "name") or f"Κανάλι {cid}"
            d["channels"] = chans
            d["recorder"] = True
        return d
    if code == 401:
        return {"ip": ip, "kind": "hikvision", "brand": "Hikvision", "ok": False, "note": "Λάθος χρήστης ή κωδικός"}
    code, body = _get(op, base + "/cgi-bin/magicBox.cgi?action=getDeviceType")
    if code == 200 and body.startswith(b"type="):
        model = body.decode("utf-8", "replace").split("=", 1)[1].strip()
        d = {"ip": ip, "kind": "dahua", "brand": "Dahua", "model": model, "name": "", "ok": True}
        if re.search(r"NVR|XVR|DVR|HCVR", model, re.I):
            c2, b2 = _get(op, base + "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
            chans = {}
            for n, title in re.findall(r"ChannelTitle\[(\d+)\]\.Name=(.*)", b2.decode("utf-8", "replace")):
                chans[str(int(n) + 1)] = title.strip() or f"Κανάλι {int(n) + 1}"
            d["channels"] = chans
            d["recorder"] = True
        return d
    if code == 401:
        return {"ip": ip, "kind": "dahua", "brand": "Dahua", "ok": False, "note": "Λάθος χρήστης ή κωδικός"}
    # anything else that speaks ONVIF
    try:
        req = urllib.request.Request(base + "/onvif/device_service", method="POST", data=b"<?xml version='1.0'?>",
                                     headers={"Content-Type": "application/soap+xml"})
        urllib.request.urlopen(req, timeout=4)
        ok = True
    except urllib.error.HTTPError as e:
        ok = e.code in (400, 401, 500)  # an ONVIF service answers SOAP faults
    except Exception:  # noqa: BLE001
        ok = False
    if ok:
        return {"ip": ip, "kind": "onvif", "brand": "ONVIF", "model": "", "name": "", "ok": True,
                "note": "Ο κωδικός ελέγχεται όταν ξεκινήσει το κουτί"}
    return {"ip": ip, "kind": None, "brand": "", "ok": False, "note": "Δεν αναγνωρίστηκε ως κάμερα"}


def find_devices():
    """Addresses of likely cameras: WS-Discovery (ONVIF), SADP (Hikvision) and a port scan, merged."""
    out = {}
    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        f1, f2, f3 = ex.submit(A.discover, 3), ex.submit(sadp), ex.submit(port_scan)
        for d in f1.result() if not f1.exception() else []:
            out.setdefault(d["ip"], {"ip": d["ip"], "model": d.get("hardware") or d.get("name", "")})
        for ip, d in (f2.result() if not f2.exception() else {}).items():
            out.setdefault(ip, {}).update(d)
        for ip in f3.result() if not f3.exception() else set():
            out.setdefault(ip, {"ip": ip})
    me = local_ip()
    out.pop(me, None)
    return sorted(out.values(), key=lambda d: tuple(int(x) for x in d["ip"].split(".")))


def build_config(server, token, user, password, devices):
    cams = []
    for d in devices:
        key = f"{d['kind'][:3]}-{d['ip'].replace('.', '-')}"
        c = {"key": key, "name": d.get("name") or (f"{d.get('brand', '')} {d['ip']}".strip()), "kind": d["kind"],
             "host": d["ip"], "port": 80, "user": user, "password": password}
        if d.get("channels"):
            c["channels"] = d["channels"]
        cams.append(c)
    return {"server": server.rstrip("/"), "token": token, "verify_tls": True, "heartbeat_seconds": 30, "throttle_seconds": 10,
            "local_webhook_port": 0, "auto_update": False, "webrtc": True,
            "clips": {"enabled": True, "pre_seconds": 10, "post_seconds": 10}, "cameras": cams}


# ------------------------------------------------------------------ window
def gui():
    import tkinter as tk
    from tkinter import messagebox, ttk
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    old = {}
    try:
        old = json.load(open(CONFIG, encoding="utf-8"))
    except (OSError, ValueError):
        pass

    root = tk.Tk()
    root.title("Cam to Drone · Κουτί καμερών")
    root.minsize(760, 560)
    st = ttk.Style()
    try:
        st.theme_use("vista")
    except tk.TclError:
        pass
    pad = {"padx": 10, "pady": 4}
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    ttk.Label(frm, text="Cam to Drone · Κουτί καμερών", font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    status_top = ttk.Label(frm, text="", foreground="#555")
    status_top.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

    box1 = ttk.LabelFrame(frm, text="1. Σύνδεση με το κέντρο", padding=8)
    box1.grid(row=2, column=0, columnspan=3, sticky="ew")
    box1.columnconfigure(1, weight=1)
    v_server = tk.StringVar(value=old.get("server") or DEFAULT_SERVER)
    v_token = tk.StringVar(value=old.get("token", ""))
    ttk.Label(box1, text="Κέντρο").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(box1, textvariable=v_server).grid(row=0, column=1, sticky="ew", **pad)
    ttk.Label(box1, text="Κωδικός κουτιού").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(box1, textvariable=v_token).grid(row=1, column=1, sticky="ew", **pad)
    ttk.Label(box1, text="Τον δίνει το κέντρο (Διαχείριση → Κουτιά → «Νέο κουτί»).", foreground="#666").grid(row=2, column=1, sticky="w", padx=10)

    box2 = ttk.LabelFrame(frm, text="2. Κάμερες", padding=8)
    box2.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
    frm.rowconfigure(3, weight=1)
    box2.columnconfigure(1, weight=1)
    box2.rowconfigure(3, weight=1)
    cams_old = old.get("cameras") or []
    v_user = tk.StringVar(value=cams_old[0].get("user", "admin") if cams_old else "admin")
    v_pw = tk.StringVar(value=cams_old[0].get("password", "") if cams_old else "")
    ttk.Label(box2, text="Χρήστης καμερών").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(box2, textvariable=v_user, width=24).grid(row=0, column=1, sticky="w", **pad)
    ttk.Label(box2, text="Κωδικός καμερών").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(box2, textvariable=v_pw, width=24, show="•").grid(row=1, column=1, sticky="w", **pad)
    bar = ttk.Frame(box2)
    bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 4))
    b_scan = ttk.Button(bar, text="Αναζήτηση καμερών")
    b_scan.pack(side="left", padx=(10, 6))
    v_ip = tk.StringVar()
    ttk.Label(bar, text="ή διεύθυνση IP:").pack(side="left", padx=(12, 4))
    ttk.Entry(bar, textvariable=v_ip, width=16).pack(side="left")
    b_add = ttk.Button(bar, text="Προσθήκη")
    b_add.pack(side="left", padx=6)

    cols = ("use", "ip", "type", "model", "status")
    tree = ttk.Treeview(box2, columns=cols, show="headings", height=9, selectmode="browse")
    for c, t, w in zip(cols, ("✓", "Διεύθυνση", "Τύπος", "Μοντέλο / όνομα", "Κατάσταση"), (40, 120, 150, 210, 230)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="center" if c == "use" else "w", stretch=c in ("model", "status"))
    tree.grid(row=3, column=0, columnspan=3, sticky="nsew", padx=10)
    ttk.Label(box2, text="Διπλό κλικ σε μια γραμμή για να μπει ή να βγει από την παρακολούθηση.", foreground="#666").grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(4, 0))

    bottom = ttk.Frame(frm)
    bottom.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    status = ttk.Label(bottom, text="", wraplength=520)
    status.pack(side="left", fill="x", expand=True)
    b_install = ttk.Button(bottom, text="Εγκατάσταση και εκκίνηση")
    b_install.pack(side="right")
    b_stop = ttk.Button(bottom, text="Διακοπή")
    b_log = ttk.Button(bottom, text="Αρχείο καταγραφής", command=lambda: os.startfile(LOG) if os.path.exists(LOG) else None)

    devices = {}  # ip -> dict (identify result + "use")

    def say(text, color="#222"):
        root.after(0, lambda: status.configure(text=text, foreground=color))

    def refresh_top():
        pid = running_pid()
        if pid:
            status_top.configure(text=f"● Το κουτί λειτουργεί σε αυτόν τον υπολογιστή ({len(cams_old)} κάμερες/καταγραφικά).", foreground="#15803d")
            b_stop.pack(side="right", padx=6)
            b_log.pack(side="right")
        else:
            status_top.configure(text="Συμπληρώστε τα παρακάτω και πατήστε «Εγκατάσταση και εκκίνηση»." if not old
                                 else "○ Το κουτί είναι εγκατεστημένο αλλά δεν λειτουργεί αυτή τη στιγμή.", foreground="#555")
            b_stop.pack_forget()
            if os.path.exists(LOG):
                b_log.pack(side="right", padx=6)

    def row_values(d):
        typ = d.get("brand") or "-"
        if d.get("recorder"):
            typ += f" καταγραφικό ({len(d.get('channels') or {})} κάμ.)"
        st_ = d.get("note") or ("Εντάξει" if d.get("ok") else "")
        return ("✓" if d.get("use") else "", d["ip"], typ, d.get("name") or d.get("model") or "", st_)

    def redraw():
        tree.delete(*tree.get_children())
        for ip in sorted(devices, key=lambda x: tuple(int(p) for p in x.split("."))):
            tree.insert("", "end", iid=ip, values=row_values(devices[ip]))

    def toggle(_e=None):
        ip = tree.focus()
        if ip and ip in devices and devices[ip].get("kind"):
            devices[ip]["use"] = not devices[ip].get("use")
            tree.item(ip, values=row_values(devices[ip]))

    def show_note(_e=None):
        d = devices.get(tree.focus())
        if d and d.get("note"):
            say(f"{d['ip']}: {d['note']}", "#b45309" if d.get("warn") or not d.get("ok") else "#222")

    tree.bind("<<TreeviewSelect>>", show_note)
    tree.bind("<Double-1>", toggle)
    tree.bind("<space>", toggle)

    def check(ips, keep_use=None):
        user, pw = v_user.get().strip(), v_pw.get()
        with concurrent.futures.ThreadPoolExecutor(16) as ex:
            for d in ex.map(lambda ip: identify(ip, user, pw), ips):
                d["use"] = keep_use.get(d["ip"], d.get("ok")) if keep_use is not None else bool(d.get("ok"))
                devices[d["ip"]] = {**devices.get(d["ip"], {}), **d}
        root.after(0, redraw)

    def busy(on):
        for b in (b_scan, b_add, b_install):
            b.configure(state="disabled" if on else "normal")

    def scan():
        busy(True)
        say("Αναζήτηση στο δίκτυο… (περίπου 10 δευτερόλεπτα)")

        def work():
            try:
                found = find_devices()
                for d in found:
                    devices.setdefault(d["ip"], {"ip": d["ip"], "model": d.get("model", ""), "brand": d.get("brand", "")})
                root.after(0, redraw)
                say(f"Βρέθηκαν {len(found)} συσκευές. Έλεγχος με τον κωδικό…")
                check([d["ip"] for d in found])
                ok = sum(1 for d in devices.values() if d.get("ok"))
                warn = [d for d in devices.values() if d.get("warn")]
                if warn:
                    say(f"{ok} κάμερες αναγνωρίστηκαν. Προσοχή: {len(warn)} δεν δίνουν ζωντανό βίντεο τώρα "
                        f"({warn[0]['ip']}: {warn[0]['warn']}). Θα στέλνουν συναγερμούς και εικόνα ανά δευτερόλεπτο.", "#b45309")
                else:
                    say(f"Έτοιμο: {ok} κάμερες/καταγραφικά αναγνωρίστηκαν." if ok else
                        "Δεν αναγνωρίστηκε καμία κάμερα. Ελέγξτε χρήστη/κωδικό ή προσθέστε τη διεύθυνση IP.", "#15803d" if ok else "#b45309")
            finally:
                root.after(0, lambda: busy(False))
        threading.Thread(target=work, daemon=True).start()

    def add():
        ip = v_ip.get().strip()
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            say("Γράψτε μια διεύθυνση όπως 192.168.1.64", "#b91c1c")
            return
        busy(True)
        say(f"Έλεγχος {ip}…")

        def work():
            try:
                check([ip])
                d = devices.get(ip, {})
                say(f"{ip}: {d.get('note') or ('Εντάξει' if d.get('ok') else 'δεν αναγνωρίστηκε')}"
                    + (". Θα στέλνει συναγερμούς και εικόνα ανά δευτερόλεπτο." if d.get("warn") else ""),
                    "#15803d" if d.get("ok") and not d.get("warn") else "#b45309")
            finally:
                root.after(0, lambda: busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_install():
        server, token = v_server.get().strip(), v_token.get().strip()
        chosen = [d for d in devices.values() if d.get("use") and d.get("kind")]
        if not token:
            say("Συμπληρώστε τον κωδικό κουτιού.", "#b91c1c")
            return
        if not chosen and not cams_old:
            say("Επιλέξτε τουλάχιστον μία κάμερα (αναζήτηση ή διεύθυνση IP).", "#b91c1c")
            return
        busy(True)
        say("Έλεγχος σύνδεσης με το κέντρο…")

        def work():
            try:
                check_token(server, token)
                if chosen:
                    cfg = build_config(server, token, v_user.get().strip(), v_pw.get(), chosen)
                else:  # keep the cameras already installed, only the connection changed
                    cfg = {**old, "server": server.rstrip("/"), "token": token}
                    cfg.pop("_path", None)
                say("Εγκατάσταση…")
                install(cfg)
                cams_old[:] = cfg["cameras"]
                time.sleep(2)
                root.after(0, refresh_top)
                say("Έτοιμο. Το κουτί λειτουργεί στο παρασκήνιο και ξεκινά μόνο του με τα Windows. Μπορείτε να κλείσετε αυτό το παράθυρο.", "#15803d")
            except Exception as e:  # noqa: BLE001
                say(str(e), "#b91c1c")
            finally:
                root.after(0, lambda: busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_stop():
        if messagebox.askyesno("Διακοπή", "Να σταματήσει το κουτί; Οι κάμερες δεν θα στέλνουν συναγερμούς μέχρι να ξεκινήσει ξανά."):
            stop_agent()
            refresh_top()

    b_scan.configure(command=scan)
    b_add.configure(command=add)
    b_install.configure(command=do_install)
    b_stop.configure(command=do_stop)
    for c in cams_old:  # show what is installed
        devices[c["host"]] = {"ip": c["host"], "kind": c["kind"], "brand": {"hikvision": "Hikvision", "dahua": "Dahua"}.get(c["kind"], "ONVIF"),
                              "name": c.get("name", ""), "channels": c.get("channels"), "recorder": bool(c.get("channels")),
                              "use": True, "ok": True, "note": "Εγκατεστημένη"}
    redraw()
    refresh_top()
    root.mainloop()


def main():
    if "--run" in sys.argv:
        run_agent()
    elif "--uninstall" in sys.argv:
        uninstall()
    else:
        gui()


if __name__ == "__main__":
    main()
