"""Explicit platform API origins; account credentials never select an endpoint."""
import ipaddress
import re
import socket
from urllib.parse import unquote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import check_password, require_auth
from .database import SessionLocal, get_db
from .integrations.base import ConnectorError
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import NetworkSetting


DEFAULTS = {
    "pixiv": ("Pixiv", "https://app-api.pixiv.net", "作品 API 地址；账号授权仍使用 Pixiv 官方 OAuth 地址。"),
    "picacg": ("哔咔漫画", "https://picaapi.picacomic.com", "漫画 API 地址；请求签名保持平台原有格式。"),
    "ehentai": ("E-Hentai", "https://api.e-hentai.org/api.php", "仅切换画廊元数据 API；画廊页面和图片来源保持不变。"),
    "jm": ("JM 天堂", "https://18comic.vip", "作品 HTML 接口域名，只支持站点根地址。"),
    "kemono": ("Kemono", "https://kemono.cr/api/v1", "作者与作品 API 地址。"),
}
PREFIX = "platform_endpoint:"
router = APIRouter(prefix="/platform-endpoints", dependencies=[Depends(require_auth)])


def _platform(platform):
    if platform not in DEFAULTS:
        raise ConnectorError("不支持的平台接口")
    return DEFAULTS[platform]


def public_dns(host):
    """Check every answer, including when an outbound proxy is configured."""
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        valid = bool(answers) and all(ipaddress.ip_address(answer[4][0]).is_global for answer in answers)
    except (OSError, ValueError):
        raise ConnectorError("无法解析接口域名，请检查地址和网络") from None
    if not valid:
        raise ConnectorError("接口域名必须解析到公网地址")


def validate_endpoint(platform, value, *, resolve=True):
    _platform(platform)
    value = value.strip() if isinstance(value, str) else ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (not value or len(value) > 2048 or parsed.scheme != "https" or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value)
                or host.endswith(".") or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
                or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?", parsed.path)
                or any(part in (".", "..") for part in parsed.path.split("/"))
                or platform == "jm" and parsed.path not in ("", "/")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
    except ValueError:
        raise ConnectorError("接口地址须为公网 HTTPS 地址，不能含账号、端口、查询参数或片段；JM 只支持根地址") from None
    if resolve:
        public_dns(host)
    return urlunsplit(("https", host.lower(), parsed.path.rstrip("/"), "", ""))


def read_config(platform, db=None):
    _platform(platform)
    if db is None:
        with SessionLocal() as session:
            return read_config(platform, session)
    row = db.get(NetworkSetting, PREFIX + platform)
    if row is None:
        return {"custom_url": None, "enabled": False}
    try:
        value = decrypt_credentials(row.value_encrypted)
        if type(value.get("enabled")) is not bool or value.get("custom_url") is not None and not isinstance(value["custom_url"], str):
            raise ValueError()
        custom = validate_endpoint(platform, value["custom_url"], resolve=False) if value.get("custom_url") else None
        if value["enabled"] and not custom:
            raise ValueError()
        return {"custom_url": custom, "enabled": value["enabled"]}
    except (ConnectorError, ValueError, TypeError, AttributeError):
        raise ConnectorError("平台接口配置无法读取，请重新保存该平台的接口地址") from None


def get_endpoint(platform, db=None):
    config = read_config(platform, db)
    return config["custom_url"] if config["enabled"] else DEFAULTS[platform][1]


def public_config(platform, db=None):
    label, default, description = _platform(platform)
    config = read_config(platform, db)
    return {"platform": platform, "label": label, "default_url": default, **config,
            "effective_url": config["custom_url"] if config["enabled"] else default, "description": description}


def assert_endpoint_request(url, endpoint, *, exact_path=False):
    """Before transport: reject origin/path escape and non-public DNS answers."""
    try:
        target, root = urlsplit(str(url)), urlsplit(endpoint)
        path = unquote(target.path)
        prefix = root.path.rstrip("/")
        if (target.scheme != "https" or target.hostname != root.hostname or target.port not in (None, 443)
                or target.username is not None or target.password is not None or target.fragment
                or "\\" in path or "%" in path or any(part in (".", "..") for part in path.split("/"))
                or exact_path and target.path != root.path
                or not exact_path and prefix and path != prefix and not path.startswith(prefix + "/")):
            raise ValueError()
    except ValueError:
        raise ConnectorError("平台请求或重定向超出已配置的接口地址，已停止") from None
    public_dns(root.hostname)


def guard_http_client(client, endpoint, *, exact_path=False):
    """SafeHTTP checks each redirect; this adds exact-origin/prefix enforcement."""
    client.client.event_hooks["request"].append(
        lambda request: assert_endpoint_request(str(request.url), endpoint, exact_path=exact_path))
    return client


class EndpointInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    custom_url: str | None = Field(default=None, max_length=2048)
    enabled: bool
    admin_password: str | None = Field(default=None, max_length=512, repr=False)


@router.get("")
def list_endpoints(db=Depends(get_db)):
    try:
        return {"items": [public_config(platform, db) for platform in DEFAULTS]}
    except ConnectorError as exc:
        raise HTTPException(503, str(exc)) from None


@router.put("/{platform}")
def update_endpoint(platform: str, value: EndpointInput, request: Request, db=Depends(get_db)):
    if platform not in DEFAULTS:
        raise HTTPException(404, "不支持的平台接口")
    if request.headers.get("authorization") is not None:
        if value.admin_password is None:
            raise HTTPException(403, "更改平台接口需要输入当前管理员密码")
        try:
            check_password(value.admin_password, request)
        except HTTPException as exc:
            if exc.status_code == 401:
                raise HTTPException(403, "管理员密码错误；设备登录仍然有效") from None
            raise
    try:
        custom = validate_endpoint(platform, value.custom_url, resolve=value.enabled) if value.custom_url and value.custom_url.strip() else None
        if value.enabled and custom is None:
            raise ConnectorError("启用自定义接口前请填写地址")
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from None
    config = {"custom_url": custom, "enabled": value.enabled}
    encrypted = encrypt_credentials(config)
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(NetworkSetting).values(key=PREFIX + platform, value_encrypted=encrypted)
        db.execute(statement.on_conflict_do_update(index_elements=["key"], set_={"value_encrypted": encrypted}))
    else:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(NetworkSetting).values(key=PREFIX + platform, value_encrypted=encrypted)
        db.execute(statement.on_duplicate_key_update(value_encrypted=encrypted))
    db.commit()
    return public_config(platform, db)
