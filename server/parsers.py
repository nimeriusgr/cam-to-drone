"""Parse what cameras / NVRs push by HTTP (Hikvision ISAPI XML, Dahua, JSON, form, multipart with images)."""
import email
import email.policy
import json
import re
from urllib.parse import parse_qs

XML_FIELDS = ["eventType", "eventState", "eventDescription", "channelID", "dynChannelID", "channelName",
              "dateTime", "ipAddress", "macAddress", "activePostCount", "targetType", "detectionTarget"]


def parse_xml(text):
    out = {}
    for f in XML_FIELDS:
        m = re.search(rf"<(?:\w+:)?{f}>(.*?)</(?:\w+:)?{f}>", text, re.S)
        if m:
            out[f] = m.group(1).strip()
    return out


def _pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def summarize_json(obj):
    if not isinstance(obj, dict):
        return {}
    return {k: v for k, v in {
        "eventType": _pick(obj, "eventType", "event", "type", "Code", "code", "alarmType"),
        "channelName": _pick(obj, "camera", "cameraName", "channelName", "deviceName"),
        "channelID": _pick(obj, "channel", "channelID", "index"),
        "dateTime": _pick(obj, "time", "dateTime", "timestamp", "LocaleTime"),
        "eventState": _pick(obj, "state", "eventState", "action", "Action"),
        "ipAddress": _pick(obj, "ipAddress", "ip"),
        "targetType": _pick(obj, "target", "targetType", "objectType", "ObjectType"),
    }.items() if v is not None}


def parse_dahua_lines(text):
    """Dahua eventManager style: Code=VideoMotion;action=Start;index=0"""
    m = re.search(r"Code=([^;\s]+);action=([^;\s]+);index=(\d+)", text)
    if not m:
        return {}
    return {"eventType": m.group(1), "eventState": "active" if m.group(2).lower() == "start" else "inactive",
            "channelID": str(int(m.group(3)) + 1)}


def parse_body(ctype, body):
    """Returns (summary dict, list of (bytes, ext) images, raw text)."""
    ctype_l = (ctype or "").lower()
    images, raw = [], ""
    if not body:
        return {}, images, raw
    if "multipart" in ctype_l:
        msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
                                       policy=email.policy.default)
        summary = {}
        for part in (msg.iter_parts() if msg.is_multipart() else []):
            pct = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            if pct.startswith("image/"):
                images.append((payload, "png" if "png" in pct else "jpg"))
                continue
            text = payload.decode("utf-8", "replace")
            raw += text[:3000]
            if "<" in text and "eventType" in text:
                summary.update(parse_xml(text))
            else:
                try:
                    summary.update(summarize_json(json.loads(text)))
                except ValueError:
                    summary.update(parse_dahua_lines(text))
        return summary, images, raw
    if ctype_l.startswith("image/"):
        return {}, [(body, "png" if "png" in ctype_l else "jpg")], ""
    text = body.decode("utf-8", "replace")
    raw = text[:4000]
    if "json" in ctype_l or text.lstrip().startswith(("{", "[")):
        try:
            return summarize_json(json.loads(text)), images, raw
        except ValueError:
            return {}, images, raw
    if "xml" in ctype_l or text.lstrip().startswith("<"):
        return parse_xml(text), images, raw
    if "x-www-form-urlencoded" in ctype_l:
        form = {k: v[0] for k, v in parse_qs(text).items()}
        return summarize_json(form) or form, images, raw
    return parse_dahua_lines(text), images, raw
