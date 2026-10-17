import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from threading import Lock
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .config import settings

COOKIE_NAME = "qiye_session"
_attempts: dict[str, deque[float]] = defaultdict(deque)
_attempt_lock = Lock()


def issue_session() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + settings.session_seconds, "nonce": secrets.token_hex(16)}).encode()).decode().rstrip("=")
    signature = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 1024:
        return False
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return isinstance(data.get("exp"), int) and data["exp"] > time.time()
    except (ValueError, TypeError, UnicodeError):
        return False


def require_auth(request: Request) -> None:
    if request.headers.get("authorization") is not None:
        from .device_auth import authenticate_device
        authenticate_device(request)
        return
    if not is_authenticated(request):
        raise HTTPException(401, "请先登录书库")


def require_admin_cookie(request: Request) -> None:
    if request.headers.get("authorization") is not None:
        raise HTTPException(403, "此操作需要在网页使用管理员登录")
    if not is_authenticated(request):
        raise HTTPException(401, "请先登录书库")


def verify_origin(request: Request) -> None:
    """Protect cookie-authenticated mutations, including login, against CSRF."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if "*" in settings.allowed_origins:
        return
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "不允许跨站写入请求")
    origin = request.headers.get("origin")
    if not origin:
        # Non-browser scripts may omit Origin. Browsers cannot add custom cookie
        # credentials across sites with the Strict SameSite cookie.
        return
    allowed = {str(request.base_url).rstrip("/")} | set(settings.allowed_origins)
    if origin.rstrip("/") not in allowed:
        raise HTTPException(403, "请求来源未获允许，请设置 LIBRARY_ALLOWED_ORIGINS")


def check_password(password: str, request: Request) -> None:
    address = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _attempt_lock:
        queue = _attempts[address]
        while queue and queue[0] < now - 300:
            queue.popleft()
        if len(queue) >= 12:
            raise HTTPException(429, "登录尝试过多，请五分钟后重试")
        if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(settings.admin_password.encode()).digest()):
            queue.append(now)
            raise HTTPException(401, "密码错误")
        _attempts.pop(address, None)
