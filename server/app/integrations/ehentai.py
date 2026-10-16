import json
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urlparse, urljoin, parse_qs
from .base import Connector, ConnectorError, RemoteWork, SafeHTTP, validate_url, fetch_preview_cover, metadata_page_count


class GalleryDocument(HTMLParser):
    """Parse documented gallery/image links without executing page JavaScript."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.image = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag == "img" and values.get("id") == "img":
            self.image = values.get("src")


class EHentaiConnector(Connector):
    supports_import = True
    message = "支持 gdata 元数据与完整画廊页面图片保存；使用站点当前展示清晰度，不调用消耗 GP 的原图／归档接口。My Tags 和收藏同步待实现，账号网络需实测。"

    def validate_credentials(self):
        if not self.credentials.get("ipb_member_id") or not self.credentials.get("ipb_pass_hash"):
            raise ConnectorError("E-Hentai 账号需要 ipb_member_id 和 ipb_pass_hash；ExHentai 可补充 igneous")

    def normalize_id(self, value):
        if re.fullmatch(r"\d+:[a-fA-F0-9]{10}", value.strip()):
            return value.strip().lower()
        validate_url(value, ("e-hentai.org", "exhentai.org"))
        match = re.fullmatch(r"/g/(\d+)/([a-fA-F0-9]{10})/?", urlparse(value).path)
        if not match:
            raise ConnectorError("请输入完整的 E-Hentai 画廊链接（包含 gallery token）")
        return f"{match[1]}:{match[2].lower()}"

    def fetch(self, value, *, download=True, checkpoint=lambda value: None):
        from app.endpoint_config import DEFAULTS, get_endpoint, guard_http_client
        ident = self.normalize_id(value)
        gid, token = ident.split(":")
        endpoint = get_endpoint("ehentai")
        client = SafeHTTP((urlparse(endpoint).hostname,))
        try:
            if endpoint != DEFAULTS["ehentai"][1]:
                guard_http_client(client, endpoint, exact_path=True)
            content = client.fetch(endpoint, method="POST",
                json={"method": "gdata", "gidlist": [[int(gid), token]], "namespace": 1}, limit=2 * 1024 * 1024)
            data = json.loads(content).get("gmetadata", [{}])[0]
        finally:
            client.close()
        if data.get("error") or not data.get("title"):
            raise ConnectorError("E-Hentai 无法读取该画廊元数据")
        tags = data.get("tags", [])
        host = "exhentai.org" if self.credentials.get("igneous") else "e-hentai.org"
        gallery_url = f"https://{host}/g/{gid}/{token}/"
        result = RemoteWork(remote_id=ident, title=data.get("title_jpn") or data["title"],
            source_url=gallery_url, tags=tags,
            author=", ".join(t.partition(":")[2] for t in tags if t.startswith("artist:")),
            description=f"来源页数 {data.get('filecount', '?')} · 上传者 {data.get('uploader', '')} · 站点展示清晰度",
            metadata_only=not download)
        result.remote_page_count = metadata_page_count(data.get("filecount"))
        if not download:
            checkpoint(10)
            # gdata's documented thumb is a standalone CDN preview; no gallery
            # index, image-view page, original-image quota or account cookie.
            result.preview_cover = fetch_preview_cover(data.get("thumb"),
                ("ehgt.org", "hath.network", "e-hentai.org", "exhentai.org"), headers={"Referer": gallery_url})
            return result
        count = int(data.get("filecount", 0))
        if count < 1 or count > 2000:
            raise ConnectorError("E-Hentai 画廊页数无效或超过 2000 页导入限制")
        cookies = {k: self.credentials[k] for k in ("ipb_member_id", "ipb_pass_hash", "igneous") if self.credentials.get(k)}
        gallery = SafeHTTP(("e-hentai.org", "exhentai.org"), cookies=cookies,
            headers={"User-Agent": "QiyeLibrary/0.1", "Referer": gallery_url})
        # A separate client prevents account cookies reaching distributed H@H servers.
        media = SafeHTTP(("hath.network", "ehgt.org", "e-hentai.org", "exhentai.org"),
                         headers={"Referer": gallery_url})
        try:
            pending, visited, page_links = {0}, set(), {}
            while pending:
                page = min(pending)
                pending.remove(page)
                if page in visited:
                    continue
                if page > 100 or len(visited) >= 100:
                    raise ConnectorError("画廊目录超过 100 页限制；未保存不完整作品")
                visited.add(page)
                checkpoint(5 + min(len(visited), 10))
                document = GalleryDocument(gallery.fetch(f"{gallery_url}?p={page}", limit=4 * 1024 * 1024).decode("utf-8", errors="replace"))
                for link in document.links:
                    url = urljoin(gallery_url, link)
                    parsed = urlparse(url)
                    image_match = re.fullmatch(r"/s/[a-fA-F0-9]+/" + re.escape(gid) + r"-(\d+)", parsed.path)
                    if image_match:
                        validate_url(url, (host,))
                        number = int(image_match[1])
                        if 1 <= number <= count:
                            page_links[number] = url
                    elif parsed.path.rstrip("/") == f"/g/{gid}/{token}":
                        validate_url(url, (host,))
                        next_page = parse_qs(parsed.query).get("p", ["0"])[0]
                        if next_page.isdigit() and int(next_page) not in visited:
                            pending.add(int(next_page))
            if set(page_links) != set(range(1, count + 1)):
                raise ConnectorError("画廊目录缺页或需要登录／通过站点验证；未保存不完整作品")
            total = 0
            for number in range(1, count + 1):
                checkpoint(15 + int(75 * (number - 1) / count))
                document = GalleryDocument(gallery.fetch(page_links[number], limit=4 * 1024 * 1024).decode("utf-8", errors="replace"))
                if not document.image:
                    raise ConnectorError(f"第 {number} 页没有可用图片；可能触发限额或站点结构已变化")
                url = urljoin(page_links[number], document.image)
                if urlparse(url).path.endswith(("509.gif", "509s.gif")):
                    raise ConnectorError("E-Hentai 图片配额已用尽，请稍后重试")
                image = media.fetch(url)
                suffix = PurePosixPath(urlparse(url).path).suffix.lower()
                if suffix not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"):
                    raise ConnectorError("画廊返回无法识别的图片格式；未保存不完整作品")
                total += len(image)
                if total > 512 * 1024 * 1024:
                    raise ConnectorError("E-Hentai 作品超过 512 MiB 导入限制")
                result.files.append((f"{number:05d}{suffix}", image))
        finally:
            gallery.close()
            media.close()
        return result

    def capabilities(self):
        return [{"key": "metadata", "label": "画廊元数据", "status": "available"},
                {"key": "import", "label": "完整画廊页面图片", "status": "available"},
                {"key": "sync", "label": "关注／收藏同步", "status": "unavailable"}]
