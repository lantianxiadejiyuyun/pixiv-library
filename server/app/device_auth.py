"""Opaque mobile credentials, narrow route grants and one-time pairing.

Only SHA-256 digests are persisted. Neither a device token nor a pairing token
can grant administrator privileges, including when a cookie is also supplied.
"""
from collections import deque
from datetime import timedelta
import hashlib
import ipaddress
import json
import re
import secrets
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update

from .database import SessionLocal
from .models import DevicePairing, DeviceSession, new_id, utcnow

PAIRING_SECONDS = 300
SESSION_DAYS = 90
MAX_ACTIVE_DEVICES = 50
_PAIRING_TOKEN = re.compile(r"ql_pair_[A-Za-z0-9_-]{43}\Z")
_DEVICE_TOKEN = re.compile(r"ql_device_[A-Za-z0-9_-]{64}\Z")
_ID = r"[^/]+"
_READ_PATHS = tuple(re.compile(pattern + r"\Z") for pattern in (
    r"/api/(?:health|dashboard|items|tags)", r"/api/translation/status",
    r"/api/library/collections", r"/api/favorite-groups", r"/api/platform-endpoints",
    r"/api/characters", rf"/api/characters/{_ID}",
    r"/api/app-updates/android", r"/api/app-updates/android/[a-f0-9]{64}/apk",
    rf"/api/items/{_ID}", rf"/api/items/{_ID}/(?:cover|export|translation)",
    rf"/api/assets/{_ID}/(?:content|thumbnail|text-sections)",
    rf"/api/series/{_ID}",
    r"/api/pixiv/authors", rf"/api/pixiv/authors/{_ID}(?:/(?:avatar|feed))?", rf"/api/pixiv/tasks/{_ID}",
))
_WRITE_PATHS = {"PUT": re.compile(rf"(?:/api/items/{_ID}/(?:progress|favorite|score)|/api/favorite-groups/{_ID}|/api/platform-endpoints/(?:pixiv|ehentai|picacg|jm|kemono))\Z"),
                "DELETE": re.compile(rf"(?:/api/favorite-groups/{_ID}|/api/history)\Z"),
                "POST": re.compile(rf"(?:/api/items/{_ID}/(?:download|translate|visit|cover-refresh)|/api/favorite-groups|/api/devices/disconnect|/api/pixiv/authors/open|/api/pixiv/authors/{_ID}/feed|/api/characters|/api/characters/search|/api/characters/{_ID}/profile)\Z")}
_rate_lock = Lock()
_rates = {}


def token_digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def limit_attempts(action, identity, limit, *, global_limit):
    """Bound attempts without retaining raw tokens, URLs or unbounded client keys."""
    now = monotonic()
    key = (action, hashlib.sha256(identity.encode()).hexdigest())
    global_key = (action, "global")
    with _rate_lock:
        for current in list(_rates):
            bucket = _rates[current]
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if not bucket:
                del _rates[current]
        if key not in _rates and len(_rates) >= 1024:
            raise HTTPException(429, "绑定尝试过多，请稍后重试", headers={"Retry-After": "60"})
        bucket, global_bucket = _rates.setdefault(key, deque()), _rates.setdefault(global_key, deque())
        if len(bucket) >= limit or len(global_bucket) >= global_limit:
            raise HTTPException(429, "绑定尝试过多，请稍后重试", headers={"Retry-After": "60"})
        bucket.append(now)
        global_bucket.append(now)


def normalize_server_url(value):
    value = value.strip()
    message = "服务器地址须为根地址；公网使用 HTTPS，局域网可使用 HTTP，不得包含账号、查询参数或片段"
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or any(char in value for char in ("\\", "?", "#")):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or port == 0 or "%" in host):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii").lower()
            if (len(host) > 253 or host.endswith(".") or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split("."))
                    or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host)):
                raise ValueError()
            local = "." not in host or host.endswith((".local", ".lan", ".home.arpa"))
        else:
            if address.is_unspecified or address.is_multicast:
                raise ValueError()
            local = address.is_loopback or address.is_link_local or any(address in network for network in (
                ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7")))
            host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        if parsed.scheme == "http" and not local:
            raise ValueError()
        suffix = f":{port}" if port is not None and port != (443 if parsed.scheme == "https" else 80) else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except (ValueError, UnicodeError) as exc:
        raise ValueError(message) from exc


def create_pairing(db, server_url):
    moment = utcnow()
    token = "ql_pair_" + secrets.token_urlsafe(32)
    values = {"id": new_id(), "slot": 1, "token_hash": token_digest(token), "created_at": moment,
              "expires_at": moment + timedelta(seconds=PAIRING_SECONDS), "consumed_at": None}
    # A single unique slot atomically replaces any previous authorization even
    # if two browser tabs create QR codes concurrently. Expired history is bounded.
    db.execute(delete(DevicePairing).where(or_(DevicePairing.expires_at <= moment, DevicePairing.consumed_at.is_not(None))))
    cutoff = moment - timedelta(days=SESSION_DAYS)
    db.execute(delete(DeviceSession).where(or_(DeviceSession.expires_at < cutoff, DeviceSession.revoked_at < cutoff)))
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(DevicePairing).values(**values)
        db.execute(statement.on_conflict_do_update(index_elements=["slot"], set_=values))
    else:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(DevicePairing).values(**values)
        db.execute(statement.on_duplicate_key_update(**values))
    db.commit()
    return {"id": values["id"], "server_url": server_url, "expires_at": values["expires_at"].isoformat() + "Z",
            "qr_payload": json.dumps({"type": "qiye-library", "version": 1, "server_url": server_url,
                                      "pairing_token": token}, ensure_ascii=False, separators=(",", ":"))}


def exchange_pairing(db, pairing_token, device_name):
    invalid = HTTPException(400, "绑定码无效或已过期，请在网页重新生成")
    if not _PAIRING_TOKEN.fullmatch(pairing_token):
        raise invalid
    moment = utcnow()
    # Conditional UPDATE is the one-time claim on both MySQL and SQLite; row
    # locks serialize competing exchanges before the device row is inserted.
    claimed = db.execute(update(DevicePairing).where(DevicePairing.token_hash == token_digest(pairing_token),
        DevicePairing.consumed_at.is_(None), DevicePairing.expires_at > moment).values(consumed_at=moment))
    if claimed.rowcount != 1:
        db.rollback()
        raise invalid
    count = db.scalar(select(func.count(DeviceSession.id)).where(DeviceSession.revoked_at.is_(None), DeviceSession.expires_at > moment))
    if count >= MAX_ACTIVE_DEVICES:
        db.rollback()
        raise HTTPException(409, "已达到 50 台有效设备，请在网页撤销不再使用的设备")
    token = "ql_device_" + secrets.token_urlsafe(48)
    device = DeviceSession(token_hash=token_digest(token), device_name=device_name,
                           created_at=moment, last_used_at=None, expires_at=moment + timedelta(days=SESSION_DAYS))
    db.add(device)
    db.commit()
    return {"device_id": device.id, "device_name": device.device_name, "access_token": token,
            "expires_at": device.expires_at.isoformat() + "Z", "server_name": "栖页"}


def permitted(method, path):
    if method == "HEAD":
        return re.fullmatch(rf"/api/assets/{_ID}/content", path) is not None
    if method == "GET":
        return any(pattern.fullmatch(path) for pattern in _READ_PATHS)
    return bool(_WRITE_PATHS.get(method) and _WRITE_PATHS[method].fullmatch(path))


def authenticate_device(request):
    if getattr(request.state, "device_id", None):
        return request.state.device_id
    values = request.headers.getlist("authorization")
    if len(values) != 1 or not values[0].startswith("Bearer ") or not _DEVICE_TOKEN.fullmatch(values[0][7:]):
        raise HTTPException(401, "设备凭据无效，请重新扫码绑定", headers={"WWW-Authenticate": "Bearer"})
    moment = utcnow()
    with SessionLocal() as db:
        device = db.scalar(select(DeviceSession).where(DeviceSession.token_hash == token_digest(values[0][7:]),
            DeviceSession.revoked_at.is_(None), DeviceSession.expires_at > moment))
        if device is None:
            raise HTTPException(401, "设备授权已失效，请重新扫码绑定", headers={"WWW-Authenticate": "Bearer"})
        if not permitted(request.method, request.url.path):
            raise HTTPException(403, "此设备仅获授权阅读书库、保存进度及请求下载；管理操作请使用网页登录")
        if device.last_used_at is None or device.last_used_at < moment - timedelta(seconds=60):
            changed = db.execute(update(DeviceSession).where(DeviceSession.id == device.id,
                DeviceSession.revoked_at.is_(None), DeviceSession.expires_at > moment).values(last_used_at=moment))
            if changed.rowcount != 1:
                raise HTTPException(401, "设备授权已失效，请重新扫码绑定")
            db.commit()
        request.state.device_id = device.id
        return device.id
