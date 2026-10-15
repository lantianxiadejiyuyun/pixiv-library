"""One explicit, encrypted proxy configuration for all platform traffic.

Controller requests always connect directly. A configured proxy handles target DNS
so Clash fake-IP / remote DNS works; HTTPS host allowlists still apply to downloads.
"""
from urllib.parse import quote, urlsplit, urlunsplit

from .database import SessionLocal
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import NetworkSetting

DEFAULTS = {"enabled": "false", "proxy_url": "", "proxy_username": "",
            "proxy_password": "", "controller_url": "", "controller_secret": ""}
SETTING_KEY = "outbound_proxy"


def read_config(db=None):
    if db is None:
        with SessionLocal() as session:
            return read_config(session)
    row = db.get(NetworkSetting, SETTING_KEY)
    return {**DEFAULTS, **(decrypt_credentials(row.value_encrypted) if row else {})}


def public_config(config):
    return {"enabled": config["enabled"] == "true", "proxy_url": config["proxy_url"],
            "proxy_username": config["proxy_username"],
            "has_proxy_password": bool(config["proxy_password"]),
            "controller_url": config["controller_url"],
            "has_controller_secret": bool(config["controller_secret"])}


def validate_endpoint(value, *, controller=False):
    value = value.strip()
    if not value:
        return ""
    message = "控制器地址应为 http(s)://主机:端口" if controller else "代理地址应为 http(s):// 或 socks5(h)://主机:端口；认证信息请填独立输入框"
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in (("http", "https") if controller else ("http", "https", "socks5", "socks5h"))
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value)
                or not parsed.port or not 1 <= parsed.port <= 65535):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    # Operator-selected LAN and loopback hosts are intentional for Clash/NAS.
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def save_config(db, values):
    current = read_config(db)
    current.update({k: v for k, v in values.items() if v is not None})
    current["enabled"] = "true" if values["enabled"] else "false"
    current["proxy_url"] = validate_endpoint(current["proxy_url"])
    current["controller_url"] = validate_endpoint(current["controller_url"], controller=True)
    if current["enabled"] == "true" and not current["proxy_url"]:
        raise ValueError("启用代理前请填写代理地址")
    if current["proxy_password"] and not current["proxy_username"]:
        raise ValueError("设置代理密码时需要同时填写用户名")
    if any(ord(c) < 32 or ord(c) == 127 for c in current["controller_secret"]):
        raise ValueError("控制器密钥不能含换行或控制字符")
    row = db.get(NetworkSetting, SETTING_KEY)
    if row is None:
        row = NetworkSetting(key=SETTING_KEY)
        db.add(row)
    row.value_encrypted = encrypt_credentials(current)
    db.commit()
    return current


def httpx_proxy(config=None):
    from .managed_proxy import outbound_proxy
    managed = outbound_proxy()
    if managed:
        return managed
    config = read_config() if config is None else config
    if config["enabled"] != "true":
        return None
    address = validate_endpoint(config["proxy_url"])
    if not address:
        raise ValueError("代理已启用但地址缺失，请重新保存代理配置")
    parts = urlsplit(address)
    authority = parts.netloc
    if config["proxy_username"]:
        authority = f"{quote(config['proxy_username'], safe='')}:{quote(config['proxy_password'], safe='')}@{authority}"
    return urlunsplit((parts.scheme, authority, "", "", ""))


def requests_proxies(config=None):
    proxy = httpx_proxy(config)
    # Requests' socks5 resolves locally, unlike HTTPX; always use remote DNS.
    if proxy and proxy.startswith("socks5://"):
        proxy = "socks5h://" + proxy[len("socks5://"):]
    # Empty values also prevent Requests from inheriting shell/OS proxy settings.
    return {"http": proxy or "", "https": proxy or "", "all": ""}
