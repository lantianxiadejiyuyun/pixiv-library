"""Public Kemono posts, using the site's documented v1 API.

Protocol: https://kemono.cr/documentation/api (OpenAPI 3.1, API version 1.3).
Only passive reads are implemented: no importer, account, or favorite mutations.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urlencode, urlparse

from .base import Connector, ConnectorError, RemoteWork, SafeHTTP, fetch_preview_cover, validate_url


SITE_ROOT = "https://kemono.cr"
MEDIA_HOSTS = ("kemono.cr",)
SERVICES = frozenset({"patreon", "fanbox", "fantia", "afdian", "boosty", "gumroad", "subscribestar", "dlsite"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".m4v", ".mov"})
MAX_FILES = 2000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_IDENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MEDIA_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,1024}\Z")


def _parts(service, user, post=None):
    values = (user,) if post is None else (user, post)
    if not isinstance(service, str) or service not in SERVICES or any(not isinstance(v, str) or not _IDENT.fullmatch(v) for v in values):
        raise ConnectorError("Kemono 编号格式无效；请使用支持的创作者主页或作品链接")
    result = ":".join((service, *values))
    if len(result) > 191:
        raise ConnectorError("Kemono 作品编号过长")
    return result


def _media_path(path):
    # Only inert file paths returned by Kemono, never arbitrary URLs, queries,
    # percent-encoded traversal, or a second network location.
    if (not isinstance(path, str) or not _MEDIA_PATH.fullmatch(path)
            or path.startswith("//") or any(p in (".", "..") for p in path.split("/"))):
        raise ConnectorError("Kemono 文件路径格式不受支持")
    return path


def media_url(path, *, preview=False):
    path = _media_path(path)
    return ("https://img.kemono.cr/thumbnail/data" if preview else SITE_ROOT + "/data") + path


class _PostDocument(HTMLParser):
    """Convert remote markup into inert text; collect image paths separately."""
    _BLOCK = frozenset({"p", "div", "section", "article", "li", "ul", "ol", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "tr"})
    _HIDDEN = frozenset({"script", "style", "template"})

    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = []
        self.videos = []
        self.video_sources = []
        self.unsupported_media = False
        self.hidden = []
        self.feed(content)
        self.close()
        if any(count == 0 for count in self.video_sources):
            self.unsupported_media = True

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in ("iframe", "audio", "object", "embed"):
            self.unsupported_media = True
        if tag in ("video", "source"):
            src = dict(attrs).get("src")
            if tag == "video":
                self.video_sources.append(0)
            if src:
                self.videos.append(src)
                if self.video_sources:
                    self.video_sources[-1] += 1
                else:
                    self.unsupported_media = True
        if tag == "img":
            values = dict(attrs)
            src = values.get("src") or values.get("data-src")
            if src:
                self.images.append(src)
            else:
                self.unsupported_media = True
        if tag in self._BLOCK:
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if self.hidden[-1] == tag:
                self.hidden.pop()
            return
        if tag == "video" and self.video_sources:
            if self.video_sources.pop() == 0:
                self.unsupported_media = True
        if tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    @property
    def text(self):
        value = "".join(self.parts).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", value).strip()


def _inline_path(src):
    if src.startswith("/data/") and not src.startswith("//"):
        return _media_path(src[len("/data"):])
    if src.startswith("//"):
        src = "https:" + src
    try:
        validate_url(src, MEDIA_HOSTS)
        parsed = urlparse(src)
    except (ValueError, ConnectorError) as exc:
        raise ConnectorError("Kemono 正文包含外部图片，尚未支持完整离线保存") from exc
    if parsed.query or parsed.fragment or not parsed.path.startswith("/data/"):
        raise ConnectorError("Kemono 正文图片路径不受支持")
    return _media_path(parsed.path[len("/data"):])


class KemonoConnector(Connector):
    supports_import = supports_subscriptions = True
    message = "支持公开作品的封面、图片、MP4／WebM／M4V／MOV 视频和文章正文，以及作者／标签／搜索订阅。压缩包、外部嵌入内容和 Discord 暂不支持；不需要登录。"

    def validate_credentials(self):
        # Public endpoints never receive stored account credentials or cookies.
        return None

    def normalize_id(self, value):
        if not isinstance(value, str):
            raise ConnectorError("请输入 Kemono 作品链接或 service:作者ID:作品ID")
        value = value.strip()
        parts = value.split(":")
        if len(parts) == 3 and "/" not in value:
            return _parts(*parts)
        try:
            validate_url(value, ("kemono.cr",))
            parsed = urlparse(value)
        except (ValueError, ConnectorError) as exc:
            raise ConnectorError("请输入 Kemono 官方作品链接或 service:作者ID:作品ID") from exc
        match = re.fullmatch(r"/([^/]+)/user/([^/]+)/post/([^/]+)/?", parsed.path)
        if not match or parsed.hostname not in ("kemono.cr", "www.kemono.cr"):
            raise ConnectorError("请输入 Kemono 官方作品链接或 service:作者ID:作品ID")
        return _parts(*match.groups())

    def normalize_author(self, value):
        if not isinstance(value, str):
            raise ConnectorError("请输入 Kemono 作者主页链接或 service:作者ID")
        value = value.strip()
        parts = value.split(":")
        if len(parts) == 2 and "/" not in value:
            return _parts(*parts)
        try:
            validate_url(value, ("kemono.cr",))
            parsed = urlparse(value)
        except (ValueError, ConnectorError) as exc:
            raise ConnectorError("请输入 Kemono 作者主页链接或 service:作者ID") from exc
        match = re.fullmatch(r"/([^/]+)/user/([^/]+)/?", parsed.path)
        if not match or parsed.hostname not in ("kemono.cr", "www.kemono.cr"):
            raise ConnectorError("请输入 Kemono 作者主页链接或 service:作者ID")
        return _parts(*match.groups())

    def _request(self, endpoint):
        from app.endpoint_config import get_endpoint, guard_http_client
        root = get_endpoint("kemono").rstrip("/")
        host = urlparse(root).hostname
        if not host:
            raise ConnectorError("Kemono API 地址无效")
        # Kemono's own frontend sends Accept: text/css for JSON API responses.
        # application/json is rejected by the current public API (HTTP 403).
        client = SafeHTTP((host,), headers={"Accept": "text/css", "User-Agent": "QiyeLibrary/0.1"})
        try:
            guard_http_client(client, root)
            response = json.loads(client.fetch(root + "/" + endpoint, limit=8 * 1024 * 1024))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ConnectorError("Kemono 返回了无法解析的响应，请检查 API 地址或平台网络") from exc
        finally:
            client.close()
        if not isinstance(response, dict) or response.get("error"):
            raise ConnectorError("Kemono 返回的数据格式无效或作品无法访问")
        return response

    def fetch_author(self, target, checkpoint=lambda value: None):
        ident = self.normalize_author(target)
        service, user = ident.split(":")
        checkpoint(5)
        response = self._request(f"{service}/user/{user}/profile")
        if (_parts(response.get("service"), response.get("id")) != ident
                or not isinstance(response.get("name"), str) or not response["name"].strip()):
            raise ConnectorError("Kemono 作者资料不完整或编号不一致")
        return {"remote_id": ident, "name": response["name"][:512], "description": "",
                "statistics": {}, "avatar": None, "is_self": False}

    def fetch(self, value, *, download=True, checkpoint=lambda value: None):
        ident = self.normalize_id(value)
        service, user, post_id = ident.split(":")
        checkpoint(2)
        response = self._request(f"{service}/user/{user}/post/{post_id}")
        post = response.get("post")
        if (not isinstance(post, dict) or _parts(post.get("service"), post.get("user"), post.get("id")) != ident
                or not isinstance(post.get("title"), str) or not isinstance(post.get("content", ""), str)):
            raise ConnectorError("Kemono 返回的作品资料不完整或编号不一致")
        attachments = post.get("attachments")
        primary = post.get("file")
        if not isinstance(attachments, list) or not isinstance(primary, dict) or any(not isinstance(v, dict) for v in attachments):
            raise ConnectorError("Kemono 文件目录不完整")
        if len(attachments) > MAX_FILES:
            raise ConnectorError("Kemono 作品超过 2000 个文件限制")
        document = _PostDocument(post.get("content", ""))
        extra_files = []
        # Enriched video/attachment entries carry the same path as post.file or
        # post.attachments, but can also contain media absent from those arrays.
        for key in ("attachments", "videos"):
            values = response.get(key) or []
            if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
                raise ConnectorError("Kemono 附件目录不完整")
            extra_files.extend(values)
        raw_files = ([primary] if primary.get("path") or primary.get("name") else []) + attachments + extra_files
        files = []
        seen = set()
        unsupported = document.unsupported_media or bool(post.get("embed"))
        for entry in raw_files:
            path = _media_path(entry.get("path"))
            suffix = PurePosixPath(path).suffix.lower()
            if suffix not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
                unsupported = True
                continue
            if path not in seen:
                seen.add(path)
                files.append((path, suffix))
        for src in document.images + document.videos:
            try:
                path = _inline_path(src)
                suffix = PurePosixPath(path).suffix.lower()
                if suffix not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
                    raise ConnectorError("Kemono 正文包含不支持的图片格式")
            except ConnectorError:
                unsupported = True
                continue
            if path not in seen:
                seen.add(path)
                files.append((path, suffix))
        if len(files) + bool(document.text) > MAX_FILES:
            raise ConnectorError("Kemono 作品超过 2000 个文件限制")
        if download and unsupported:
            raise ConnectorError("Kemono 作品包含压缩包、未支持的媒体格式或外部嵌入内容；可先保存详情，未标记为已下载")
        author = self.fetch_author(f"{service}:{user}", checkpoint=checkpoint)
        tags = post.get("tags") or []
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ConnectorError("Kemono 标签格式不受支持")
        images = [entry for entry in files if entry[1] in IMAGE_SUFFIXES]
        has_video = any(suffix in VIDEO_SUFFIXES for _, suffix in files)
        result = RemoteWork(remote_id=ident, title=post["title"].strip()[:512] or f"Kemono {post_id}",
            source_url=f"{SITE_ROOT}/{service}/user/{user}/post/{post_id}",
            item_type="article" if document.text or has_video else "illustration" if len(files) == 1 else "manga",
            author=author["name"], author_id=f"{service}:{user}", description=document.text[:20000],
            tags=list(dict.fromkeys([f"服务:{service}", *(tag[:191] for tag in tags if tag.strip())]))[:200],
            rating="unknown", metadata_only=not download, remote_page_count=(len(files) + bool(document.text)) or None)
        checkpoint(10)
        if not download:
            if images:
                result.preview_cover = fetch_preview_cover(media_url(images[0][0], preview=True), MEDIA_HOSTS)
            return result
        if not files and not document.text:
            raise ConnectorError("Kemono 作品没有可保存的图片、视频或文章正文")
        client = SafeHTTP(MEDIA_HOSTS, headers={"Referer": SITE_ROOT + "/"})
        from app.config import settings
        text_bytes = document.text.encode("utf-8")
        total = len(text_bytes)
        try:
            for index, (path, suffix) in enumerate(files):
                checkpoint(10 + int(80 * index / len(files)))
                limit = min(settings.max_file_bytes, MAX_TOTAL_BYTES) if suffix in VIDEO_SUFFIXES else min(settings.max_file_bytes, 32 * 1024 * 1024)
                content = client.fetch(media_url(path), limit=limit)
                total += len(content)
                if total > MAX_TOTAL_BYTES:
                    raise ConnectorError("Kemono 作品超过 512 MiB 导入限制")
                result.files.append((f"{index + 1:05d}{suffix}", content))
        finally:
            client.close()
        if text_bytes:
            result.files.append(("zz-content.txt", text_bytes))
        return result

    def discover(self, target_type, target):
        if target_type == "author":
            author = self.normalize_author(target)
            service, user = author.split(":")
            response = self._request(f"{service}/user/{user}/posts?o=0")
            entries = response.get("results")
            props = response.get("props")
            if not isinstance(props, dict):
                raise ConnectorError("Kemono 作者分页信息缺失")
            count = props.get("count")
        elif target_type in ("tag", "search"):
            if not isinstance(target, str) or not target.strip() or len(target) > 200 or (target_type == "search" and len(target.strip()) < 3):
                raise ConnectorError("Kemono 搜索需至少 3 个字符，标签／搜索最多 200 个字符")
            response = self._request("posts?" + urlencode({"o": 0, "tag" if target_type == "tag" else "q": target.strip()}))
            entries = response.get("posts")
            count = response.get("count")
            author = None
        else:
            raise ConnectorError("Kemono 支持作者、标签和搜索订阅")
        if (not isinstance(entries, list) or len(entries) > 50 or type(count) is not int or count < len(entries)
                or count > 0 and not entries):
            raise ConnectorError("Kemono 返回的分页数据不完整")
        identities = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConnectorError("Kemono 返回的作品列表不完整")
            ident = _parts(entry.get("service"), entry.get("user"), entry.get("id"))
            if author is not None and ident.rsplit(":", 1)[0] != author:
                raise ConnectorError("Kemono 作者目录返回了其他作者的作品")
            identities.append(ident)
        if len(set(identities)) != len(identities):
            raise ConnectorError("Kemono 返回的作品列表包含重复编号")
        return identities, count > len(entries)

    def capabilities(self):
        return super().capabilities() + [
            {"key": "metadata", "label": "公开作品详情与封面", "status": "available"},
            {"key": "article", "label": "文章正文与图片", "status": "available"},
        ]
