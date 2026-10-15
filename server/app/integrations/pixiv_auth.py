"""Pixiv mobile OAuth PKCE with a manually pasted official callback/code.

Protocol references (not a Pixiv-supported third-party OAuth registration):
https://github.com/eggplants/get-pixivpy-token/blob/master/gppt/oauth.py
https://github.com/eggplants/get-pixivpy-token/blob/master/gppt/token.py
https://gist.github.com/upbit/6edda27cb1644e94183291109b8a5fde

The browser signs in on Pixiv. Its pixiv:// callback cannot return to our web
origin, so the UI must explicitly explain copying the callback from Network
with Preserve log enabled. Pixiv codes expire quickly; our 10-minute session
TTL does not extend the provider's code lifetime. Verifiers only live in this
process; deployments must route both steps to the same backend process.
"""
from __future__ import annotations

import base64
from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
from threading import Lock
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .base import ConnectorError


LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CALLBACK_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
# Public mobile protocol identifiers, not the user's credentials.
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
SESSION_TTL = 600
RATE_WINDOW = 300
MAX_SESSIONS = 64
MAX_SESSIONS_PER_BINDING = 3
MAX_RESPONSE_BYTES = 256 * 1024


class OAuthRateLimitError(ConnectorError):
    """May be mapped to HTTP 429 by the authenticated API layer."""


@dataclass(frozen=True)
class _PendingAuthorization:
    binding_digest: bytes = field(repr=False)
    code_verifier: str = field(repr=False)
    expires_at: float


_sessions: dict[str, _PendingAuthorization] = {}
_attempts: dict[tuple[str, bytes], deque[float]] = defaultdict(deque)
_global_attempts: dict[str, deque[float]] = {"start": deque(), "finish": deque()}
_lock = Lock()


def _binding_digest(binding: str) -> bytes:
    # The API must call require_auth before passing the authenticated cookie.
    if not isinstance(binding, str) or not binding or len(binding) > 2048:
        raise ConnectorError("书库登录会话无效，请重新登录书库")
    return hashlib.sha256(binding.encode()).digest()


def _prune(now: float) -> None:
    for ident, pending in list(_sessions.items()):
        if pending.expires_at <= now:
            del _sessions[ident]
    for key, attempts in list(_attempts.items()):
        while attempts and attempts[0] <= now - RATE_WINDOW:
            attempts.popleft()
        if not attempts:
            del _attempts[key]
    for attempts in _global_attempts.values():
        while attempts and attempts[0] <= now - RATE_WINDOW:
            attempts.popleft()


def _record_attempt(action: str, binding_digest: bytes, now: float) -> None:
    individual_limit, global_limit = (5, 60) if action == "start" else (12, 120)
    global_attempts = _global_attempts[action]
    if len(global_attempts) >= global_limit:
        raise OAuthRateLimitError("Pixiv 登录尝试较多，请五分钟后重试")
    attempts = _attempts[(action, binding_digest)]
    if len(attempts) >= individual_limit:
        raise OAuthRateLimitError("Pixiv 登录尝试过于频繁，请五分钟后重试")
    attempts.append(now)
    global_attempts.append(now)


def start_oauth(binding: str) -> dict:
    """Create one server-side verifier, bound to the caller's library session."""
    digest = _binding_digest(binding)
    now = time.monotonic()
    with _lock:
        _prune(now)
        _record_attempt("start", digest, now)
        if len(_sessions) >= MAX_SESSIONS:
            raise OAuthRateLimitError("Pixiv 待完成登录过多，请稍后重试")
        if sum(hmac.compare_digest(p.binding_digest, digest) for p in _sessions.values()) >= MAX_SESSIONS_PER_BINDING:
            raise OAuthRateLimitError("已有多个 Pixiv 登录等待完成，请先完成已打开的页面或十分钟后重试")
        session_id = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        _sessions[session_id] = _PendingAuthorization(digest, verifier, now + SESSION_TTL)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return {"session_id": session_id,
            "authorization_url": LOGIN_URL + "?" + urlencode({"code_challenge": challenge, "code_challenge_method": "S256", "client": "pixiv-android"}),
            "expires_in": SESSION_TTL}


def extract_code(value: str) -> str:
    """Accept a raw code, code= fragment, or the exact documented callbacks.

    Pasted URLs are parsed locally and are never fetched. Reject duplicate code
    parameters and unrelated origins, avoiding ambiguous clipboard input.
    """
    error = "无法识别授权码，请粘贴 Pixiv 的完整回调链接或其中的 code 值"
    if not isinstance(value, str) or len(value) > 8192:
        raise ConnectorError(error)
    pasted = value.strip()
    if not pasted or any(ord(ch) < 32 for ch in pasted):
        raise ConnectorError(error)
    if "://" in pasted:
        try:
            parsed = urlsplit(pasted)
            if parsed.username or parsed.password or parsed.port is not None or parsed.fragment:
                raise ConnectorError(error)
        except ValueError as exc:
            raise ConnectorError(error) from exc
        valid_deeplink = parsed.scheme == "pixiv" and parsed.netloc == "account" and parsed.path == "/login"
        valid_https = parsed.scheme == "https" and parsed.netloc == "app-api.pixiv.net" and parsed.path == "/web/v1/users/auth/pixiv/callback"
        if not (valid_deeplink or valid_https):
            raise ConnectorError(error)
        query = parsed.query
    elif pasted.startswith("code="):
        query = pasted
    else:
        query = None
    if query is not None:
        try:
            codes = parse_qs(query, keep_blank_values=True, max_num_fields=12).get("code", [])
        except ValueError as exc:
            raise ConnectorError(error) from exc
        if len(codes) != 1:
            raise ConnectorError(error)
        code = codes[0]
    else:
        code = pasted
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,2048}", code):
        raise ConnectorError(error)
    return code


def _http_client():
    from app.network import httpx_proxy
    try:
        return httpx.Client(proxy=httpx_proxy(), timeout=25, follow_redirects=False, trust_env=False,
            headers={"User-Agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)", "app-os": "ios", "app-os-version": "14.6"})
    except (ValueError, ImportError) as exc:
        raise ConnectorError("无法启用 Pixiv 登录连接，请检查代理地址和 SOCKS 依赖，再重新发起授权") from exc


def _exchange(code: str, verifier: str) -> dict[str, str]:
    try:
        with _http_client() as client:
            with client.stream("POST", TOKEN_URL, data={"grant_type": "authorization_code", "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET, "code": code, "code_verifier": verifier,
                "redirect_uri": CALLBACK_URI, "include_policy": "true"}) as response:
                if response.status_code == 429:
                    raise OAuthRateLimitError("Pixiv 暂时限制登录请求，请稍后重新发起授权")
                if 300 <= response.status_code < 400:
                    raise ConnectorError("Pixiv 授权接口返回意外跳转，请重新发起授权")
                if response.status_code != 200:
                    raise ConnectorError("Pixiv 拒绝授权码，可能已过期或已使用；请重新发起授权并尽快粘贴 code")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise ConnectorError("Pixiv 授权响应异常，请重新发起授权")
                payload = json.loads(bytes(content))
    except httpx.HTTPError as exc:
        raise ConnectorError("连接 Pixiv 授权服务失败，请检查书库代理设置，再重新发起授权") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConnectorError("Pixiv 授权服务返回无效数据，请重新发起授权") from exc
    if not isinstance(payload, dict) or payload.get("error") or payload.get("has_error"):
        raise ConnectorError("Pixiv 未接受此授权，请重新发起授权")
    body = payload.get("response", payload)
    if not isinstance(body, dict):
        raise ConnectorError("Pixiv 授权响应缺少账号凭据，请重新发起授权")
    refresh_token = body.get("refresh_token")
    user = body.get("user")
    user_id = str(user.get("id", "")) if isinstance(user, dict) else ""
    if (not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > 8192
            or any(ch.isspace() for ch in refresh_token) or not re.fullmatch(r"[1-9][0-9]{0,19}", user_id)):
        raise ConnectorError("Pixiv 授权响应缺少有效 refresh_token 或用户 ID，请重新发起授权")
    return {"refresh_token": refresh_token, "user_id": user_id}


def finish_oauth(binding: str, session_id: str, code: str) -> dict[str, str]:
    """Consume the session before network I/O: every exchange is one attempt.

    A failure requires a fresh authorization rather than replaying a possibly
    consumed code. A wrong browser binding cannot consume another session.
    """
    digest = _binding_digest(binding)
    now = time.monotonic()
    with _lock:
        _prune(now)
        _record_attempt("finish", digest, now)
        if not isinstance(session_id, str) or len(session_id) > 128:
            raise ConnectorError("Pixiv 登录会话无效或已过期，请重新发起授权")
        pending = _sessions.get(session_id)
        if pending is None:
            raise ConnectorError("Pixiv 登录会话无效、已使用或已过期，请重新发起授权")
        if not hmac.compare_digest(pending.binding_digest, digest):
            raise ConnectorError("请在发起 Pixiv 授权的同一书库登录会话中完成操作")
        parsed_code = extract_code(code)
        del _sessions[session_id]
    return _exchange(parsed_code, pending.code_verifier)
