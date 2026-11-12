#!/usr/bin/env python3
"""Creates seed.sql (demo data + users with random passwords) and credentials-cloud.txt for the Cloudflare D1 database.
Both files contain secrets and are git-ignored."""
import hashlib
import json
import math
import os
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
os.environ.setdefault("C2D_DATA", os.path.join(HERE, ".seed-tmp"))
from seed import DEMO, DOCKS, DEFAULT_SOP, DEFAULT_PRESETS, NIGHT, NOFLY, NOFLY_NOTE  # noqa: E402


def h(pw):
    salt = secrets.token_hex(16)
    return f"pbkdf2${salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex()}$100000"


def v(x):
    if x is None:
        return "NULL"
    if isinstance(x, (int, float)):
        return repr(x)
    if isinstance(x, (list, dict)):
        x = json.dumps(x, ensure_ascii=False)
    return "'" + str(x).replace("'", "''") + "'"


def ins(table, d):
    return f"INSERT INTO {table}({','.join(d)}) VALUES({','.join(v(x) for x in d.values())});"


sql, creds, edges = [], {}, []
now = time.time()
sql.append(ins("orgs", {"id": 1, "kind": "center", "name": "Κέντρο λήψης σημάτων", "lat": 37.98, "lon": 23.73,
                        "webhook_token": secrets.token_urlsafe(12), "sop": DEFAULT_SOP, "speaker_presets": DEFAULT_PRESETS}))
oid, cid, eid = 1, 0, 0
for name, addr, lat, lon, cams, contacts in DEMO:
    oid += 1
    eid += 1
    sql.append(ins("orgs", {"id": oid, "parent_id": 1, "kind": "customer", "name": name, "address": addr, "lat": lat, "lon": lon,
                            "arming_mode": "armed", "schedule": NIGHT, "sop": DEFAULT_SOP, "speaker_presets": DEFAULT_PRESETS,
                            "ack_timeout": 60, "webhook_token": secrets.token_urlsafe(12)}))
    tok = secrets.token_urlsafe(24)
    edges.append((name, tok))
    sql.append(ins("edges", {"id": eid, "org_id": oid, "name": f"Κουτί · {name.split(' ·')[0]}", "token": tok}))
    for i, cn in enumerate(cams):
        a = i * 6.283 / len(cams)
        sql.append(ins("cameras", {"org_id": oid, "edge_id": eid, "ext_key": f"192.168.1.{64 + i}:1", "name": cn,
                                   "lat": round(lat + 0.0006 * math.sin(a), 6), "lon": round(lon + 0.0008 * math.cos(a), 6),
                                   "last_seen": now, "online": 1}))
    for j, (cn, role, phone, mail) in enumerate(contacts):
        sql.append(ins("contacts", {"org_id": oid, "name": cn, "role": role, "phone": phone, "email": mail, "priority": j + 1}))
for name, lat, lon, r in DOCKS:
    sql.append(ins("docks", {"name": name, "lat": lat, "lon": lon, "radius_km": r, "adapter": "sim"}))
for name, lat, lon, r, lvl in NOFLY:
    sql.append(ins("nofly", {"name": name, "lat": lat, "lon": lon, "radius_m": r, "level": lvl, "notes": NOFLY_NOTE}))
for username, name, role, org in [("admin", "Διαχειριστής", "admin", None), ("operator", "Ελεγκτής", "operator", None),
                                  ("pelatis", "Πελάτης Μάρκος ΑΕ", "customer", 2)]:
    pw, api = secrets.token_urlsafe(9), secrets.token_urlsafe(24)
    sql.append(ins("users", {"username": username, "name": name, "role": role, "org_id": org, "pw_hash": h(pw), "api_token": api}))
    creds[username] = (pw, api)

with open(os.path.join(HERE, "seed.sql"), "w", encoding="utf-8") as f:
    f.write("\n".join(sql) + "\n")
lines = ["Cam to Drone (Cloudflare) · στοιχεία εισόδου", "=" * 44, ""]
for u, (pw, api) in creds.items():
    lines += [f"Χρήστης: {u}", f"  Κωδικός: {pw}", f"  API token (Postman): {api}", ""]
lines += ["Κουτιά (edge tokens):"] + [f"  {n}: {t}" for n, t in edges]
with open(os.path.join(HERE, "credentials-cloud.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print("seed.sql and credentials-cloud.txt written")
