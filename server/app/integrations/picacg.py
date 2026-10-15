"""Authenticated PicACG reader, independently implementing the published protocol.

Protocol definitions: https://github.com/l2studio/picacomic-api/blob/main/src/api.ts
and src/type.ts (Apache-2.0). No registration or remote favorites mutation.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlparse

from .base import Connector, ConnectorError, RemoteWork, SafeHTTP, validate_url, fetch_preview_cover, metadata_page_count

API_ROOT = "https://picaapi.picacomic.com/"
# Published protocol identifiers; these are not user account credentials.
API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
SIGNATURE_KEY = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
IMAGE_HOSTS = ("picacomic.com",)
MAX_PAGES = 3000


def signed_headers(endpoint, method, *, token="", timestamp=None, nonce=None):
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or uuid.uuid4().hex
    signing_input = (endpoint.lstrip("/") + timestamp + nonce + method.upper() + API_KEY).lower()
    signature = hmac.new(SIGNATURE_KEY.encode(), signing_input.encode(), hashlib.sha256).hexdigest()
    headers = {"api-key": API_KEY, "accept": "application/vnd.picacomic.com.v1+json", "app-channel": "2", "app-version": "2.2.1.2.3.3", "app-uuid": "defaultUuid", "app-platform": "android", "app-build-version": "44", "user-agent": "okhttp/3.8.1", "image-quality": "original", "content-type": "application/json; charset=UTF-8", "time": timestamp, "nonce": nonce, "signature": signature}
    if token:
        headers["authorization"] = token
    return headers


def media_url(media):
    path = media.get("path")
    if not isinstance(path, str) or not path or ".." in PurePosixPath(path).parts or "\\" in path:
        raise ConnectorError("哔咔图片路径格式不受支持")
    server = media.get("fileServer") or ""
    if not isinstance(server, str):
        raise ConnectorError("哔咔图片服务器格式不受支持")
    if path.startswith("tobeimg/"):
        if server.rstrip("/") in {"https://storage1.picacomic.com", "https://storage-b.picacomic.com"}:
            encoded = PurePosixPath(path).stem
            try:
                url = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True).decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise ConnectorError("哔咔图片链接解码失败") from exc
        else:
            url = (server or "https://img.picacomic.com").rstrip("/") + "/" + path[len("tobeimg/"):]
    elif path.startswith("tobs/"):
        url = (server or "https://storage-b.picacomic.com").rstrip("/") + "/static/" + path[len("tobs/"):]
    else:
        url = (server or "https://storage1.picacomic.com").rstrip("/") + "/static/" + path.lstrip("/")
    try:
        return validate_url(url, IMAGE_HOSTS)
    except (ConnectorError, ValueError) as exc:
        raise ConnectorError("哔咔返回了尚未支持的图片 CDN 地址，需更新适配器") from exc


class PicACGConnector(Connector):
    supports_import = supports_sync = supports_subscriptions = True
    message = "支持账号密码或 token 登录、漫画章节下载、收藏导入及本地标签／搜索订阅。作者关注接口未确认；需真实账号验证网络与接口兼容性。"

    def __init__(self, credentials):
        super().__init__(credentials)
        self._access_token = credentials.get("token", "")

    def validate_credentials(self):
        token = self.credentials.get("token")
        email, password = self.credentials.get("email"), self.credentials.get("password")
        if not (isinstance(token, str) and token.strip()) and not (isinstance(email, str) and email.strip() and isinstance(password, str) and password):
            raise ConnectorError("哔咔需要 token，或 email 与 password；账号凭据仅加密保存在本地")

    def normalize_id(self, value):
        value = value.strip()
        if re.fullmatch(r"[a-fA-F0-9]{24}", value):
            return value.lower()
        validate_url(value, ("picacomic.com",))
        match = re.fullmatch(r"/comics/([a-fA-F0-9]{24})/?", urlparse(value).path)
        if not match:
            raise ConnectorError("请输入哔咔 24 位漫画 ID，或 picacomic.com/comics/作品ID 链接")
        return match[1].lower()

    def _request(self, endpoint, *, method="GET", body=None, auth=True):
        if auth:
            self._ensure_token()
        headers = signed_headers(endpoint, method, token=self._access_token if auth else "")
        client = SafeHTTP(("picaapi.picacomic.com",), headers=headers)
        try:
            response = json.loads(client.fetch(API_ROOT + endpoint, method=method, json=body, limit=8 * 1024 * 1024))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ConnectorError("哔咔返回了无法解析的响应，请检查平台网络") from exc
        finally:
            client.close()
        if not isinstance(response, dict) or response.get("code") not in (200, 201) or response.get("error"):
            raise ConnectorError("哔咔请求被拒绝，请检查 token、账号权限或更新适配器")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ConnectorError("哔咔返回数据格式已变化，请更新适配器")
        return data

    def _ensure_token(self):
        self.validate_credentials()
        if self._access_token:
            return
        payload = self._request("auth/sign-in", method="POST", body={"email": self.credentials["email"], "password": self.credentials["password"]}, auth=False)
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise ConnectorError("哔咔登录未返回有效 token")
        self._access_token = token

    def _pages(self, endpoint, key, checkpoint, *, max_batches=100):
        values = []
        for page in range(1, max_batches + 1):
            checkpoint(min(85, page))
            response = self._request(endpoint + ("&" if "?" in endpoint else "?") + f"page={page}")
            group = response.get(key)
            if not isinstance(group, dict) or not isinstance(group.get("docs"), list):
                raise ConnectorError("哔咔分页响应格式不受支持")
            docs = group["docs"]
            try:
                pages, total = int(group["pages"]), int(group["total"])
            except (KeyError, ValueError, TypeError) as exc:
                raise ConnectorError("哔咔分页计数缺失，无法确认内容完整") from exc
            if pages > max_batches or total > MAX_PAGES:
                raise ConnectorError("哔咔内容超过单次分页限制，尚未标记完成")
            if any(not isinstance(value, dict) for value in docs):
                raise ConnectorError("哔咔分页数据不完整")
            values.extend(docs)
            if len(values) > MAX_PAGES:
                raise ConnectorError("哔咔内容超过 3000 条单次限制")
            if page >= pages:
                if len(values) != total:
                    raise ConnectorError("哔咔分页数量不匹配，未保存不完整内容")
                return values
            if not docs:
                raise ConnectorError("哔咔分页意外为空，未保存不完整内容")
        raise ConnectorError("哔咔分页超过限制")

    def fetch(self, value, *, download=True, checkpoint=lambda value: None):
        ident = self.normalize_id(value)
        comic = self._request(f"comics/{ident}").get("comic")
        if not isinstance(comic, dict) or not isinstance(comic.get("title"), str):
            raise ConnectorError("哔咔没有返回有效作品详情")
        tags = comic.get("tags", []) + ["分类:" + str(c) for c in comic.get("categories", [])]
        result = RemoteWork(remote_id=ident, title=comic["title"], source_url=f"https://picaapi.picacomic.com/comics/{ident}", author=str(comic.get("author") or ""), description=str(comic.get("description") or ""), tags=[t[:191] for t in tags if isinstance(t, str)], rating="unknown", metadata_only=not download)
        result.remote_page_count = metadata_page_count(comic.get("pagesCount"))
        if not download:
            checkpoint(10)
            thumb = comic.get("thumb")
            if isinstance(thumb, dict):
                try:
                    cover_url = media_url(thumb)
                except ConnectorError:
                    cover_url = None
                result.preview_cover = fetch_preview_cover(cover_url, IMAGE_HOSTS, headers={"user-agent": "okhttp/3.8.1"})
            return result
        if comic.get("allowDownload") is False:
            raise ConnectorError("该作品在平台上不允许下载；可通过关闭自动下载的订阅保存元数据")
        episodes = self._pages(f"comics/{ident}/eps", "eps", checkpoint)
        try:
            episodes.sort(key=lambda ep: int(ep["order"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ConnectorError("哔咔章节顺序格式不受支持") from exc
        if not episodes or len({str(ep["order"]) for ep in episodes}) != len(episodes):
            raise ConnectorError("哔咔章节为空或重复，未保存不完整作品")
        image_client = SafeHTTP(IMAGE_HOSTS, headers={"user-agent": "okhttp/3.8.1"})
        total_bytes = 0
        try:
            for chapter, episode in enumerate(episodes):
                order = int(episode["order"])
                if order < 0 or order > 100000:
                    raise ConnectorError("哔咔章节序号无效")
                pages = self._pages(f"comics/{ident}/order/{order}/pages", "pages", checkpoint)
                if not pages:
                    raise ConnectorError("哔咔章节没有可下载页面")
                for index, page in enumerate(pages):
                    checkpoint(10 + int(80 * (chapter + index / len(pages)) / len(episodes)))
                    if len(result.files) >= MAX_PAGES:
                        raise ConnectorError("哔咔作品超过 3000 页限制")
                    media = page.get("media")
                    if not isinstance(media, dict):
                        raise ConnectorError("哔咔页面缺少图片信息")
                    url = media_url(media)
                    content = image_client.fetch(url)
                    total_bytes += len(content)
                    if total_bytes > 512 * 1024 * 1024:
                        raise ConnectorError("哔咔作品超过 512 MiB 导入限制")
                    suffix = PurePosixPath(str(media.get("originalName") or urlparse(url).path)).suffix.lower()
                    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
                        suffix = ".jpg"  # Core validates actual image content.
                    result.files.append((f"e{order:05d}_p{index + 1:05d}{suffix}", content))
        finally:
            image_client.close()
        expected = comic.get("pagesCount")
        if isinstance(expected, int) and expected > 0 and len(result.files) != expected:
            raise ConnectorError("哔咔作品页数与详情不一致，未保存不完整作品")
        return result

    def favorites(self, checkpoint=lambda value: None):
        values = self._pages("users/favourite?s=ua", "comics", checkpoint)
        return list(dict.fromkeys(self.normalize_id(str(value.get("_id") or "")) for value in values))

    def discover(self, target_type, target):
        if target_type not in ("tag", "search"):
            raise ConnectorError("哔咔支持本地标签／搜索订阅；作者关注接口尚未确认")
        group = self._request("comics/advanced-search?page=1", method="POST", body={"keyword": target, "categories": [], "s": "dd"}).get("comics")
        if not isinstance(group, dict) or not isinstance(group.get("docs"), list):
            raise ConnectorError("哔咔搜索响应格式不受支持")
        entries = group["docs"]
        if target_type == "tag":
            entries = [entry for entry in entries if target in entry.get("tags", [])]
        identities = [self.normalize_id(str(value.get("_id") or "")) for value in entries]
        return list(dict.fromkeys(identities)), int(group.get("pages", 1)) > 1

    def capabilities(self):
        return [{"key": "import", "label": "漫画与章节下载", "status": "available"}, {"key": "sync", "label": "收藏导入", "status": "available"}, {"key": "subscriptions", "label": "本地标签／搜索订阅", "status": "available"}, {"key": "author_follows", "label": "平台作者关注", "status": "unavailable"}]
