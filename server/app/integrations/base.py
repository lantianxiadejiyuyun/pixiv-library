from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
import ipaddress
import math
from pathlib import PurePosixPath
import socket
from urllib.parse import urlparse, urljoin

import httpx


class ConnectorError(ValueError):
    """Safe, user-facing error. Never include remote response bodies or credentials."""


class JobCancelled(Exception):
    pass


def metadata_page_count(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 7:
        value = int(value)
    return value if type(value) is int and 1 <= value <= 1000000 else None


@dataclass
class RemoteWork:
    remote_id: str
    title: str
    source_url: str
    item_type: str = "manga"
    author: str = ""
    description: str = ""
    language: str = ""
    tags: list[str] = field(default_factory=list)
    rating: str = "unknown"
    files: list[tuple[str, bytes]] = field(default_factory=list)
    preview_cover: tuple[str, bytes] | None = None
    metadata_only: bool = False
    remote_page_count: int | None = None
    source_series: dict | None = None
    author_id: str | None = None
    bookmark_count: int | None = None
    is_bookmarked: bool | None = None
    source_published_at: datetime | None = None


def validate_url(url: str, hosts: tuple[str, ...], resolve=False) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or "\\" in url
            or not any(hostname == h or hostname.endswith("." + h) for h in hosts)):
        raise ConnectorError("来源链接必须是该平台的 HTTPS 地址")
    if resolve:
        try:
            addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ConnectorError("无法解析平台域名，请检查网络连接") from exc
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ConnectorError("平台域名解析到非公网地址，已拒绝请求")
    return url


class SafeHTTP:
    """Only fetch HTTPS from trusted platform hosts; check each redirect, bound bodies."""
    def __init__(self, hosts, headers=None, cookies=None):
        from app.network import httpx_proxy
        self.hosts = tuple(hosts)
        self.proxy = httpx_proxy()
        self.client = httpx.Client(timeout=40, follow_redirects=False, headers=headers,
                                   cookies=cookies, trust_env=False, proxy=self.proxy)

    def close(self):
        self.client.close()

    def fetch(self, url, *, method="GET", json=None, limit=32 * 1024 * 1024):
        from app.queue_control import wait_for_request
        for _ in range(4):
            validate_url(url, self.hosts, resolve=not bool(self.proxy))
            wait_for_request()
            try:
                with self.client.stream(method, url, json=json) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if method != "GET":
                            raise ConnectorError("平台 API 返回意外重定向，请更新适配器")
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code in (401, 403):
                        raise ConnectorError("平台拒绝访问，请检查账号凭据及账号权限")
                    if response.status_code == 429:
                        defer_rate_limit(response.headers.get("retry-after"))
                        raise ConnectorError("平台限流，请稍后重试")
                    if response.status_code >= 400:
                        raise ConnectorError(f"平台请求失败（HTTP {response.status_code}）")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise ConnectorError("远程文件超过单文件大小限制")
                    return bytes(content)
            except httpx.HTTPError as exc:
                raise ConnectorError("平台连接失败，请检查网络后重试") from exc
        raise ConnectorError("平台重定向次数过多")


def defer_rate_limit(retry_after=None):
    """Respect Retry-After without retaining remote headers or response bodies."""
    from app.queue_control import defer_requests
    delay = 60.0
    is_date = False
    if isinstance(retry_after, str):
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                is_date = True
            except (ValueError, TypeError, OverflowError):
                pass
    if not math.isfinite(delay) or (delay < 0 and not is_date):
        delay = 60.0
    delay = min(3600, max(0, delay))
    defer_requests(delay)


class _SDKCancellation(BaseException):
    """Cross an SDK's broad Exception handler without losing queue semantics."""
    def __init__(self, original):
        self.original = original


def pace_sdk_requests(target, method_name, *, cancellation_boundary=None, rate_limit_message=None):
    """Gate SDK transport calls, including retries, in the job's pacing context.

    When transport calls sit inside a broad SDK handler, cancellation_boundary
    restores queue exceptions only after exiting that handler. This preserves
    cancellation even for SDK-internal redirects while pacing each real send.
    """
    original = getattr(target, method_name, None)
    if not callable(original):
        raise ConnectorError("平台 SDK 请求接口已变化，请更新适配器")

    @wraps(original)
    def paced(*args, **kwargs):
        from app.queue_control import wait_for_request
        try:
            wait_for_request()
        except JobCancelled as exc:
            if cancellation_boundary is not None:
                raise _SDKCancellation(exc) from None
            raise
        response = original(*args, **kwargs)
        if getattr(response, "status_code", None) == 429:
            defer_rate_limit(response.headers.get("retry-after"))
            if rate_limit_message:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                error = ConnectorError(rate_limit_message)
                if cancellation_boundary is not None:
                    raise _SDKCancellation(error) from None
                raise error
        return response

    setattr(target, method_name, paced)
    if cancellation_boundary is not None:
        boundary, boundary_name = cancellation_boundary
        boundary_method = getattr(boundary, boundary_name)

        @wraps(boundary_method)
        def restore_cancel(*args, **kwargs):
            try:
                return boundary_method(*args, **kwargs)
            except _SDKCancellation as exc:
                raise exc.original from None

        setattr(boundary, boundary_name, restore_cancel)


def fetch_preview_cover(url, hosts, *, headers=None):
    """Fetch one optional preview image, never account cookies or full chapters.

    An unavailable cover does not discard useful metadata. Cancellation and
    queue pauses must propagate; callers persist no cover when retrieval fails.
    Image decoding/validation belongs to the library's preview storage helper.
    """
    if not isinstance(url, str) or not url:
        return None
    client = SafeHTTP(hosts, headers=headers)
    try:
        content = client.fetch(url, limit=8 * 1024 * 1024)
        suffix = PurePosixPath(urlparse(url).path).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"):
            suffix = ".jpg"
        return ("preview" + suffix, content)
    except JobCancelled:
        raise
    except (ConnectorError, ValueError):
        return None
    finally:
        client.close()


class Connector:
    supports_import = False
    supports_sync = False
    supports_subscriptions = False
    message = "适配器尚未完成；可先从本地导入已保存内容"

    def __init__(self, credentials):
        self.credentials = credentials

    def validate_credentials(self):
        if not self.credentials:
            raise ConnectorError("请填写账号凭据")

    def normalize_id(self, value):
        raise ConnectorError(self.message)

    def fetch(self, value, *, download=True, checkpoint=lambda value: None):
        raise ConnectorError(self.message)

    def follows(self, checkpoint=lambda value: None):
        raise ConnectorError("此平台关注同步尚未实现")

    def discover(self, target_type, target):
        raise ConnectorError("此平台定时订阅尚未实现")

    def capabilities(self):
        return [{"key": key, "label": label, "status": "available" if enabled else "unavailable"}
                for key, label, enabled in [("import", "内容导入", self.supports_import),
                    ("sync", "关注导入", self.supports_sync),
                    ("subscriptions", "定时订阅", self.supports_subscriptions)]]
