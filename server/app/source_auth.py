"""Platform account login. Only server-issued sessions leave this module.

Protocol references: l2studio/picacomic-api src/api.ts; jmcomic's
JmHtmlClient.login; seven332/EhViewer EhEngine.signIn and SignInParser.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from .integrations.base import ConnectorError, validate_url
from .integrations.picacg import API_ROOT, signed_headers


class SourceLoginError(ConnectorError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _client():
    # Read the current server-wide proxy for every new login, not at import time.
    from .network import httpx_proxy
    try:
        return httpx.Client(proxy=httpx_proxy(), timeout=35, follow_redirects=False, trust_env=False,
                            headers={"User-Agent": "Mozilla/5.0 QiyeLibrary/0.1"})
    except (ValueError, ImportError) as exc:
        raise SourceLoginError("无法启用平台登录连接，请检查代理地址和 SOCKS 依赖", 502) from exc


def _response(client, method, url, *, hosts, **kwargs):
    validate_url(url, hosts)
    try:
        with client.stream(method, url, **kwargs) as response:
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 2 * 1024 * 1024:
                    raise SourceLoginError("平台登录响应超过限制，请更新适配器", 502)
            # Detach the bounded body from the stream before closing it.
            body = bytes(content)
            status, headers = response.status_code, response.headers
            cookies = {cookie.name: cookie.value for cookie in response.cookies.jar}
    except httpx.HTTPError as exc:
        raise SourceLoginError("无法连接平台登录服务，请检查代理、节点和网络后重试", 502) from exc
    if status in (401, 403):
        raise SourceLoginError("平台拒绝登录，请检查账号密码；若需验证码，请先在平台完成验证后使用高级会话导入")
    if status == 429:
        raise SourceLoginError("平台登录请求过于频繁，请稍后重试")
    if status >= 500:
        raise SourceLoginError("平台登录服务暂时不可用，请稍后重试", 502)
    if status >= 400:
        raise SourceLoginError("平台未接受登录请求，请检查账号密码或更新适配器")
    return status, headers, cookies, body


def _json(body):
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise SourceLoginError("平台登录响应不是有效数据，可能需要浏览器验证", 502) from exc
    if not isinstance(value, dict):
        raise SourceLoginError("平台登录响应格式已变化，请更新适配器", 502)
    return value


def _pica_data(body):
    value = _json(body)
    if value.get("code") not in (200, 201) or value.get("error"):
        raise SourceLoginError("哔咔登录失败，请检查账号密码或账号状态")
    if not isinstance(value.get("data"), dict):
        raise SourceLoginError("哔咔登录响应缺少有效数据", 502)
    return value["data"]


def login_picacg(username, password):
    with _client() as client:
        status, _, _, body = _response(client, "POST", API_ROOT + "auth/sign-in", hosts=("picaapi.picacomic.com",),
            headers=signed_headers("auth/sign-in", "POST"), json={"email": username, "password": password})
        if status != 200:
            raise SourceLoginError("哔咔登录服务返回意外跳转，请更新适配器", 502)
        token = _pica_data(body).get("token")
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise SourceLoginError("哔咔未返回有效登录令牌", 502)
        status, _, _, body = _response(client, "GET", API_ROOT + "users/profile", hosts=("picaapi.picacomic.com",),
            headers=signed_headers("users/profile", "GET", token=token))
        user = _pica_data(body).get("user")
        if (status != 200 or not isinstance(user, dict) or not isinstance(user.get("_id"), str)
                or not user["_id"] or len(user["_id"]) > 128):
            raise SourceLoginError("哔咔登录会话验证失败，未保存账号")
        return {"token": token}


def login_jm(username, password):
    # Use exactly the origin also used by JMConnector, since AVS is domain-specific.
    with _client() as client:
        status, _, cookies, body = _response(client, "POST", "https://18comic.vip/login", hosts=("18comic.vip",),
            headers={"Origin": "https://18comic.vip", "Referer": "https://18comic.vip/login", "X-Requested-With": "XMLHttpRequest"},
            data={"username": username, "password": password, "id_remember": "on", "login_remember": "on", "submit_login": ""})
        if status != 200:
            raise SourceLoginError("JM 登录服务返回意外跳转，请在平台完成验证后重试")
        result = _json(body)
        if result.get("status") != 1:
            raise SourceLoginError("JM 登录失败，请检查账号密码；若平台要求验证码，请使用高级会话导入")
        session = cookies.get("AVS")
        if not session or len(session) > 8192:
            raise SourceLoginError("JM 未返回有效 AVS 登录会话，未保存账号")
        return {"AVS": session}


class LoginForm(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.hidden = {}
        self.feed(document)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "input" and attributes.get("type", "").lower() == "hidden" and attributes.get("name"):
            name, value = attributes["name"], attributes.get("value", "")
            if len(name) < 100 and len(value) < 4096:
                self.hidden[name] = value


def _challenge(document):
    return bool(re.search(r'class=["\'][^"\']*(?:g-recaptcha|cf-turnstile)|<title>\s*just a moment|challenge-platform', document, re.I))


def login_ehentai(username, password):
    origin = "https://forums.e-hentai.org"
    login_page = origin + "/index.php?act=Login&CODE=00"
    login_post = origin + "/index.php?act=Login&CODE=01"
    with _client() as client:
        status, _, _, body = _response(client, "GET", login_page, hosts=("forums.e-hentai.org",))
        document = body.decode("utf-8", errors="replace")
        if status != 200 or _challenge(document):
            raise SourceLoginError("E-Hentai 需要浏览器或验证码验证，请先在平台完成验证后使用高级会话导入")
        form = LoginForm(document).hidden
        # Form field names are defined by the forum's login protocol.
        form.update({"UserName": username, "PassWord": password, "submit": "Log me in", "CookieDate": "1", "temporary_https": "off"})
        status, headers, _, body = _response(client, "POST", login_post, hosts=("forums.e-hentai.org",),
            headers={"Origin": origin, "Referer": login_page}, data=form)
        if status in (301, 302, 303):
            # Never replay the password to a redirect destination.
            target = urljoin(login_post, headers.get("location", ""))
            for _ in range(3):
                validate_url(target, ("forums.e-hentai.org", "e-hentai.org"))
                status, headers, _, body = _response(client, "GET", target, hosts=("forums.e-hentai.org", "e-hentai.org"))
                if status not in (301, 302, 303):
                    break
                target = urljoin(target, headers.get("location", ""))
        document = body.decode("utf-8", errors="replace")
        if _challenge(document):
            raise SourceLoginError("E-Hentai 要求验证码，账号未保存；请完成平台验证后使用高级会话导入")
        returned = {cookie.name: cookie.value for cookie in client.cookies.jar
                    if cookie.domain.lstrip(".") in {"forums.e-hentai.org", "e-hentai.org"}}
        member_id, pass_hash = returned.get("ipb_member_id", ""), returned.get("ipb_pass_hash", "")
        if (status != 200 or not re.fullmatch(r"[1-9][0-9]{0,19}", member_id)
                or not re.fullmatch(r"[a-fA-F0-9]{32}", pass_hash)):
            raise SourceLoginError("E-Hentai 登录失败，未获得有效登录会话；请检查账号密码或验证码要求")
        # The server must either explicitly confirm login, or return its authenticated
        # forum navigation after redirect. Cookies alone are not treated as success.
        if not re.search(r"You are now logged in as:|act=Login(?:&amp;|&)CODE=03", document, re.I):
            raise SourceLoginError("E-Hentai 未确认登录成功，未保存账号；平台页面可能已变化")
        return {"ipb_member_id": member_id, "ipb_pass_hash": pass_hash}


def login_platform(platform, username, password):
    handlers = {"picacg": login_picacg, "jm": login_jm, "ehentai": login_ehentai}
    if platform == "pixiv":
        raise SourceLoginError("Pixiv 使用官方 OAuth 授权，请点击 Pixiv 官方授权登录")
    if platform not in handlers:
        raise SourceLoginError("未知来源平台")
    return handlers[platform](username, password)
