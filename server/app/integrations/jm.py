from __future__ import annotations
from io import BytesIO
import re
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from .base import Connector, ConnectorError, RemoteWork, SafeHTTP, validate_url, fetch_preview_cover, pace_sdk_requests, metadata_page_count, _SDKCancellation


class JMConnector(Connector):
    supports_import = True
    message = "支持账号密码登录、按 ID 获取作品／章节及本地图片还原；高级方式可导入 AVS 会话。收藏与作者／标签订阅待实现；真实账号与网络需联调。"

    def validate_credentials(self):
        if not isinstance(self.credentials.get("AVS"), str) or not self.credentials["AVS"]:
            raise ConnectorError("JM 账号凭据需要 AVS cookie；公开作品也可不添加账号")

    def normalize_id(self, value):
        value = value.strip()
        if re.fullmatch(r"(?:JM)?[1-9]\d{0,15}", value, re.I):
            return value.upper().removeprefix("JM")
        validate_url(value, ("18comic.vip", "18comic.org", "jmcomic.me", "jm-comic.org"))
        match = re.fullmatch(r"/album/(\d+)/?", urlparse(value).path)
        if not match:
            raise ConnectorError("请输入 JM 作品 ID 或受支持的 /album/ 作品链接")
        return match[1]

    def fetch(self, value, *, download=True, checkpoint=lambda value: None):
        ident = self.normalize_id(value)
        try:
            from jmcomic import JmOption, JmImageTool, JmModuleConfig, JmcomicText
            from curl_cffi import CurlOpt
            from PIL import Image
        except ImportError as exc:
            raise ConnectorError("请安装 server/requirements.txt 中的 jmcomic") from exc
        # A single explicitly configured HTML origin; never SDK domain failover.
        from app.endpoint_config import DEFAULTS, get_endpoint, assert_endpoint_request
        from app.network import requests_proxies
        endpoint = get_endpoint("jm")
        domain = urlparse(endpoint).hostname
        proxies = requests_proxies()
        option = JmOption.construct({"log": False, "client": {
            "impl": "html", "domain": {"html": [domain]}, "retry_times": 1,
            # libcurl honors NO_PROXY even with an explicit proxy. Clear it so an
            # operator's selected proxy cannot be bypassed by ambient settings.
            "postman": {"type": "curl_cffi", "meta_data": {"timeout": 40, "allow_redirects": False,
                "proxies": proxies, "curl_options": {CurlOpt.NOPROXY: ""},
                "cookies": {"AVS": self.credentials["AVS"]} if self.credentials.get("AVS") else {}}}}})
        client = option.new_jm_client()
        if endpoint != DEFAULTS["jm"][1]:
            original_get = client.postman.get
            def guarded_get(url, **kwargs):
                try:
                    assert_endpoint_request(url, endpoint)
                    kwargs["allow_redirects"] = False
                    response = original_get(url, **kwargs)
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise ConnectorError("JM 接口返回重定向，已停止；请检查接口地址")
                    return response
                except ConnectorError as exc:
                    raise _SDKCancellation(exc) from None
            client.postman.get = guarded_get
        # Gate the real transport before the SDK inspects status codes. The
        # boundary restores cancellation outside its broad retry handler, so
        # every actual attempt is paced without retrying a pause/cancellation.
        pace_sdk_requests(client.postman, "get", cancellation_boundary=(client, "request_with_retry"))
        validate_url(endpoint + "/", (domain,), resolve=not bool(proxies["https"]))
        album = client.get_album_detail(ident)
        work = RemoteWork(remote_id=ident, title=album.name,
            source_url=f"https://18comic.vip/album/{ident}",
            author=", ".join(getattr(album, "authors", []) or []),
            tags=list(getattr(album, "tags", []) or []),
            description=getattr(album, "description", "") or "", metadata_only=not download)
        work.remote_page_count = metadata_page_count(getattr(album, "page_count", None))
        if not download:
            checkpoint(10)
            hosts = tuple(JmModuleConfig.DOMAIN_IMAGE_LIST)
            if hosts:
                # The installed SDK documents this dedicated album thumbnail;
                # it does not fetch a chapter or require image unscrambling.
                cover_url = JmcomicText.get_album_cover_url(ident, image_domain=hosts[0], size="_3x4")
                work.preview_cover = fetch_preview_cover(cover_url, hosts, headers={"Referer": "https://18comic.vip/"})
            return work
        if int(getattr(album, "page_count", 0)) > 2000:
            raise ConnectorError("JM 作品超过 2000 页导入限制")
        # Restrict images to the library's reviewed CDN list, never derive hosts from input.
        hosts = tuple(JmModuleConfig.DOMAIN_IMAGE_LIST)
        images = SafeHTTP(hosts, headers={"Referer": "https://18comic.vip/"})
        total = 0
        try:
            with TemporaryDirectory(prefix="library-jm-") as directory:
                for chapter in album:
                    checkpoint(10)
                    photo = client.get_photo_detail(chapter.photo_id, False)
                    for entry in photo:
                        index = len(work.files)
                        if index >= 2000:
                            raise ConnectorError("JM 作品超过 2000 页导入限制")
                        checkpoint(min(90, 10 + index // 5))
                        raw = images.fetch(entry.img_url)
                        path = Path(directory) / "page.png"
                        with warnings.catch_warnings():
                            warnings.simplefilter("error", Image.DecompressionBombWarning)
                            with Image.open(BytesIO(raw)) as image:
                                JmImageTool.decode_and_save(JmImageTool.get_num_by_detail(entry), image, str(path))
                        data = path.read_bytes()
                        total += len(data)
                        if total > 512 * 1024 * 1024:
                            raise ConnectorError("JM 作品超过 512 MiB 导入限制")
                        work.files.append((f"{index + 1:05d}.png", data))
        finally:
            images.close()
        if not work.files:
            raise ConnectorError("JM 未返回图片，未创建空的离线作品")
        expected = int(getattr(album, "page_count", 0))
        if expected and len(work.files) != expected:
            raise ConnectorError("JM 下载页数与作品详情不一致；未保存不完整作品")
        return work
