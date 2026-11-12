"""First-run data: demo organisations, cameras, docks, users. Passwords are random and written to data/credentials.txt."""
import os
import secrets
import time

import db
import logic

DEFAULT_SOP = [
    "Έλεγχος εικόνας και κλιπ συναγερμού",
    "Ανάληψη και ενημέρωση ιδιοκτήτη/υπευθύνου",
    "Αν είναι ύποπτο: αποστολή drone",
    "Μήνυμα από μεγάφωνο drone",
    "Αν επιβεβαιωθεί παρουσία: ειδοποίηση αστυνομίας (100/112)",
    "Κλείσιμο με αποτέλεσμα και σημείωση",
]
DEFAULT_PRESETS = [
    "Προσοχή. Ο χώρος παρακολουθείται. Απομακρυνθείτε αμέσως.",
    "Βρίσκεστε σε ιδιωτικό χώρο. Η παρουσία σας καταγράφεται.",
    "Η αστυνομία έχει ειδοποιηθεί και βρίσκεται καθ' οδόν.",
]
NIGHT = [{"days": [1, 2, 3, 4, 5, 6, 7], "from": "21:00", "to": "07:00"}]

DEMO = [
    ("Μάρκος ΑΕ · Αποθήκες", "Λεωφ. Βουλιαγμένης 100, Γλυφάδα", 37.8735, 23.7581,
     ["Είσοδος", "Parking", "Ράμπα φόρτωσης", "Περίφραξη Β", "Περίφραξη Ν", "Αποθήκη 1", "Αποθήκη 2", "Γραφεία"],
     [("Κώστας Μάρκου", "Ιδιοκτήτης", "6900000001", "owner@example.com"), ("Δημήτρης Μάρκου", "Υπεύθυνος", "6900000002", "")]),
    ("Super Market Ήλιος", "Κηφισίας 210, Κηφισιά", 38.0745, 23.8105,
     ["Κεντρική είσοδος", "Πίσω πόρτα", "Parking", "Αποθήκη", "Ταμεία"],
     [("Ελένη Παππά", "Διευθύντρια", "6900000003", "manager@example.com")]),
    ("Βιοτεχνία Λαμπρόπουλος", "ΒΙΠΕ Ασπρόπυργου, Οδός 4", 38.0592, 23.5905,
     ["Πύλη", "Αυλή", "Χώρος παραγωγής", "Περίφραξη Α", "Περίφραξη Δ", "Δεξαμενές"],
     [("Γιάννης Λαμπρόπουλος", "Ιδιοκτήτης", "6900000004", "")]),
]
DOCKS = [("Dock Γλυφάδας", 37.8690, 23.7530, 6), ("Dock Κηφισιάς", 38.0700, 23.8050, 6),
         ("Dock Ασπρόπυργου", 38.0560, 23.5960, 6)]


def run():
    if db.q1("SELECT id FROM users LIMIT 1"):
        return None
    center = db.insert("orgs", {"kind": "center", "name": "Κέντρο λήψης σημάτων", "address": "", "lat": 37.98, "lon": 23.73,
                                "webhook_token": secrets.token_urlsafe(12), "sop": DEFAULT_SOP, "speaker_presets": DEFAULT_PRESETS})
    edges = []
    for name, addr, lat, lon, cams, contacts in DEMO:
        oid = db.insert("orgs", {"parent_id": center, "kind": "customer", "name": name, "address": addr, "lat": lat, "lon": lon,
                                 "arming_mode": "armed", "schedule": NIGHT, "sop": DEFAULT_SOP,
                                 "speaker_presets": DEFAULT_PRESETS, "ack_timeout": 60,
                                 "webhook_token": secrets.token_urlsafe(12)})
        tok = secrets.token_urlsafe(24)
        eid = db.insert("edges", {"org_id": oid, "name": f"Κουτί · {name.split(' ·')[0]}", "token": tok})
        edges.append((name, tok))
        for i, cn in enumerate(cams):
            ang = i * 6.283 / len(cams)
            db.insert("cameras", {"org_id": oid, "edge_id": eid, "ext_key": f"192.168.1.{64 + i}:1", "name": cn,
                                  "lat": lat + 0.0006 * __import__('math').sin(ang), "lon": lon + 0.0008 * __import__('math').cos(ang),
                                  "last_seen": time.time(), "online": 1})
        for j, (cn, role, phone, mail) in enumerate(contacts):
            db.insert("contacts", {"org_id": oid, "name": cn, "role": role, "phone": phone, "email": mail, "priority": j + 1})
    for name, lat, lon, r in DOCKS:
        db.insert("docks", {"name": name, "lat": lat, "lon": lon, "radius_km": r, "adapter": "sim"})

    creds = {}
    first_customer = db.q1("SELECT id FROM orgs WHERE kind='customer' ORDER BY id LIMIT 1")["id"]
    for username, name, role, org in [("admin", "Διαχειριστής", "admin", None), ("operator", "Ελεγκτής", "operator", None),
                                      ("pelatis", "Πελάτης Μάρκος ΑΕ", "customer", first_customer)]:
        pw = secrets.token_urlsafe(8)
        api = secrets.token_urlsafe(24)
        db.insert("users", {"username": username, "name": name, "role": role, "org_id": org,
                            "pw_hash": logic.hash_pw(pw), "api_token": api})
        creds[username] = (pw, api)

    lines = ["Cam to Drone · στοιχεία πρώτης εκκίνησης", "=" * 44, ""]
    for u, (pw, api) in creds.items():
        lines += [f"Χρήστης: {u}", f"  Κωδικός: {pw}", f"  API token (Postman): {api}", ""]
    lines += ["Κουτιά (edge tokens):"] + [f"  {n}: {t}" for n, t in edges]
    lines += ["", "Webhook ανά επιχείρηση: δείτε Διαχείριση → Οργανισμοί."]
    path = os.path.join(db.DATA, "credentials.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
