"""Web Push (RFC 8291 aes128gcm + RFC 8292 VAPID).

Needs the optional package `cryptography` (pip install cryptography). Without it push is reported as disabled
and everything else keeps working.
"""
import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import db

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s):
    s = s.replace("+", "-").replace("/", "_")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw_public(key):
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid():
    """(private key, public key b64url). Created once and kept in settings."""
    cfg = db.setting("vapid")
    if cfg and cfg.get("pem"):
        key = serialization.load_pem_private_key(cfg["pem"].encode(), password=None)
        return key, cfg["public"]
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = b64u(_raw_public(key))
    db.set_setting("vapid", {"pem": pem, "public": pub})
    return key, pub


def public_key():
    return vapid()[1] if AVAILABLE else None


def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload, p256dh, auth):
    ua_pub = b64d(p256dh)
    secret = b64d(auth)
    as_key = ec.generate_private_key(ec.SECP256R1())
    as_pub = _raw_public(as_key)
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = _hkdf(secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + body


def _jwt(endpoint, subject):
    key, pub = vapid()
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}", pub


def send(sub, message, subject="https://localhost"):
    """Returns 'sent', 'gone' (subscription expired, delete it) or 'failed'."""
    data = encrypt(json.dumps(message, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    token, pub = _jwt(sub["endpoint"], subject)
    req = urllib.request.Request(sub["endpoint"], data=data, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "3600", "Urgency": "high",
        "Authorization": f"vapid t={token}, k={pub}"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return "sent"
    except urllib.error.HTTPError as e:
        return "gone" if e.code in (404, 410) else "failed"
    except Exception:  # noqa: BLE001
        return "failed"
