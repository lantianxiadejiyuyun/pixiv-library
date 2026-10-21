from __future__ import annotations
from collections.abc import Mapping
import html
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse, parse_qs, urlsplit, urlunsplit
from .base import Connector, ConnectorError, JobCancelled, RemoteWork, SafeHTTP, validate_url, fetch_preview_cover, pace_sdk_requests, _SDKCancellation


class PixivConnector(Connector):
    supports_import = supports_sync = supports_subscriptions = True
    supports_watchlist = True
    MAX_SERIES_WORKS = 1000
    MAX_SERIES_PAGES = 50
    message = "同步主账号关注，检查作者作品和公开收藏的最新一页；详情与封面排队，不沿收藏作品扩展作者。私密收藏限本人，动图暂不支持。"

    def capabilities(self):
        return super().capabilities() + [{"key": "authors", "label": "作者主页", "status": "available"},
            {"key": "bookmarks", "label": "分页收藏", "status": "available"}]

    def validate_credentials(self):
        if not isinstance(self.credentials.get("refresh_token"), str) or not self.credentials["refresh_token"].strip():
            raise ConnectorError("Pixiv 需要 refresh_token；user_id 可选")

    def _api(self):
        if hasattr(self, '_live_api'):
            return self._live_api
        self.validate_credentials()
        try:
            from pixivpy3 import AppPixivAPI
            from app.network import requests_proxies
            from app.endpoint_config import get_endpoint
            if not hasattr(self, "_api_endpoint"):
                self._api_endpoint = get_endpoint("pixiv")
            api = AppPixivAPI(timeout=40, proxies=requests_proxies())
            api.requests.trust_env = False
            self._bind_endpoint(api, self._api_endpoint)
            pace_sdk_requests(api.requests, "send", cancellation_boundary=(api, "requests_call"),
                rate_limit_message="Pixiv 请求受到限流（HTTP 429），已记录平台等待时间；请稍后重试，建议降低并发或开启请求限速")
            auth = api.auth(refresh_token=self.credentials["refresh_token"])
            # PixivPy returns OAuth's {response: {user: ...}} envelope.
            # Never use the optional manually entered user_id as the owner.
            token = auth.get("response") if isinstance(auth.get("response"), Mapping) else auth
            user = token.get("user") or {}
            identity = str(user.get("id") or getattr(api, "user_id", "") or "")
            self._authenticated_user_id = identity if re.fullmatch(r"[1-9]\d{0,19}", identity) else ""
            self._user_id = self._authenticated_user_id
            self._live_api = api
            return api
        except ImportError as exc:
            raise ConnectorError("请安装 server/requirements.txt 中的 pixivpy3") from exc
        except (JobCancelled, ConnectorError):
            raise
        except Exception as exc:
            raise ConnectorError("Pixiv 登录失败，请检查 refresh_token 和网络") from exc

    @staticmethod
    def _bind_endpoint(api, endpoint):
        """SDK set_api_proxy also rewrites OAuth; only rewrite App API sends."""
        from app.endpoint_config import DEFAULTS, assert_endpoint_request
        original = api.requests.send
        custom = endpoint != DEFAULTS["pixiv"][1]

        def send(request, **kwargs):
            try:
                parsed = urlsplit(request.url)
                if (parsed.scheme != "https" or parsed.port not in (None, 443)
                        or parsed.username is not None or parsed.password is not None or parsed.fragment):
                    raise ConnectorError("Pixiv SDK 请求超出允许的接口范围")
                if parsed.hostname == "app-api.pixiv.net":
                    if custom:
                        root = urlsplit(endpoint)
                        request.url = urlunsplit((root.scheme, root.netloc, root.path.rstrip("/") + parsed.path, parsed.query, ""))
                        request.headers.pop("Host", None)
                        assert_endpoint_request(request.url, endpoint)
                elif parsed.hostname != "oauth.secure.pixiv.net" or parsed.path != "/auth/token":
                    raise ConnectorError("Pixiv SDK 请求超出允许的接口范围")
                # Refuse redirects before Requests can copy bearer/cookie state
                # or resend the OAuth refresh-token POST to a new origin.
                kwargs["allow_redirects"] = False
                response = original(request, **kwargs)
                if response.status_code in (301, 302, 303, 307, 308):
                    response.close()
                    raise ConnectorError("Pixiv 接口返回重定向，已停止；请检查接口地址")
                return response
            except ConnectorError as exc:
                # Preserve safe errors through pixivpy's broad Exception catch;
                # pace_sdk_requests restores them at the requests_call boundary.
                raise _SDKCancellation(exc) from None

        api.requests.send = send

    @staticmethod
    def _check(response):
        # pixivpy3 returns JsonDict: its __getattr__ returns None for missing
        # attributes, so hasattr(..., "model_dump") incorrectly returns True.
        # Accept mapping responses directly, including remote keys named after
        # methods; convert typed models only when a real callable exists.
        if not isinstance(response, Mapping):
            model_dump = getattr(response, "model_dump", None)
            if callable(model_dump):
                response = model_dump()
        if not isinstance(response, Mapping) or not response or response.get("error"):
            raise ConnectorError("Pixiv 请求失败：作品可能不可访问、登录过期或触发限流")
        return response

    def normalize_id(self, value):
        value = value.strip()
        if re.fullmatch(r"(?:novel:)?[1-9]\d{0,19}", value):
            return value
        validate_url(value, ("pixiv.net",))
        parsed = urlparse(value)
        match = re.fullmatch(r"/(?:[a-z]{2}/)?artworks/(\d+)/?", parsed.path)
        if match:
            return match[1]
        if parsed.path == "/novel/show.php":
            novel_id = parse_qs(parsed.query).get("id", [""])[0]
            if novel_id.isdigit():
                return "novel:" + novel_id
        raise ConnectorError("请输入 Pixiv 作品链接、作品 ID，或 novel:小说ID")

    @staticmethod
    def _positive_id(value):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ConnectorError("Pixiv 返回的作品或系列编号无效")
        value = str(value)
        if not re.fullmatch(r"[1-9]\d{0,19}", value):
            raise ConnectorError("Pixiv 返回的作品或系列编号无效")
        return value

    def normalize_series(self, value, kind=None):
        if kind not in (None, "novel", "manga") or not isinstance(value, str):
            raise ConnectorError("Pixiv 系列类型必须是 novel 或 manga")
        value = value.strip()
        if re.fullmatch(r"[1-9]\d{0,19}", value):
            if kind is None:
                raise ConnectorError("数字系列 ID 需要明确选择小说或漫画系列")
            return value, kind
        validate_url(value, ("pixiv.net",))
        parsed = urlparse(value)
        if parsed.hostname not in ("pixiv.net", "www.pixiv.net") or parsed.query or parsed.fragment:
            raise ConnectorError("请输入不带查询参数的 Pixiv 官方系列链接")
        path = re.sub(r"^/[a-z]{2}(?:-[a-zA-Z]{2,8})?(?=/)", "", parsed.path)
        novel = re.fullmatch(r"/novel/series/([1-9]\d{0,19})/?", path)
        manga = re.fullmatch(r"/user/([1-9]\d{0,19})/series/([1-9]\d{0,19})/?", path)
        if novel:
            ident, actual_kind = novel[1], "novel"
        elif manga:
            ident, actual_kind = manga[2], "manga"
        else:
            raise ConnectorError("请输入 Pixiv 小说系列或漫画系列链接")
        if kind is not None and kind != actual_kind:
            raise ConnectorError("Pixiv 系列链接与选择的类型不一致")
        return ident, actual_kind

    @classmethod
    def _metadata_work(cls, work, *, novel, metadata_only=True, source_series=None):
        if isinstance(work, Mapping) and work.get("visible") is False:
            raise ConnectorError("Pixiv 作品对当前账号不可见；请检查该账号的 R18/R18G 浏览设置和作品访问权限，也可能是作品已删除或转为私密")
        if not isinstance(work, Mapping) or not isinstance(work.get("title"), str) or not work["title"].strip():
            raise ConnectorError("Pixiv 返回的作品数据不完整")
        ident = cls._positive_id(work.get("id"))
        count = work.get("page_count")
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ConnectorError("Pixiv 返回的作品页数无效")
        series = work.get("series")
        if source_series is None and isinstance(series, Mapping) and series:
            source_series = {"remote_id": cls._positive_id(series.get("id")),
                "title": str(series.get("title") or ""), "kind": "novel" if novel else "manga"}
        elif source_series is not None and isinstance(series, Mapping) and series:
            if cls._positive_id(series.get("id")) != source_series["remote_id"]:
                raise ConnectorError("Pixiv 系列目录包含其他系列的作品，未保存混合目录")
        author = work.get("user") or {}
        caption = work.get("caption") or ""
        from app.source_dates import source_timestamp
        return RemoteWork(remote_id="novel:" + ident if novel else ident, title=work["title"],
            source_url=f"https://www.pixiv.net/novel/show.php?id={ident}" if novel else f"https://www.pixiv.net/artworks/{ident}",
            item_type="novel" if novel else ("manga" if work.get("type") == "manga" else "illustration"),
            author=str(author.get("name") or "") if isinstance(author, Mapping) else "",
            author_id=cls._positive_id(author["id"]) if isinstance(author, Mapping) and author.get("id") else None,
            description=html.unescape(re.sub("<[^>]*>", "", caption)) if isinstance(caption, str) else "",
            tags=[t["name"] for t in (work.get("tags") or []) if isinstance(t, Mapping) and isinstance(t.get("name"), str) and t["name"]],
            rating="adult" if work.get("x_restrict", 0) else "unknown", metadata_only=metadata_only,
            remote_page_count=count, source_series=dict(source_series) if source_series else None,
            bookmark_count=work.get("total_bookmarks") if type(work.get("total_bookmarks")) is int and 0 <= work["total_bookmarks"] <= 9223372036854775807 else None,
            is_bookmarked=work.get("is_bookmarked") if type(work.get("is_bookmarked")) is bool else None,
            source_published_at=source_timestamp(work.get("create_date")))

    @classmethod
    def _unavailable_series_entry(cls, entry):
        # The App API includes restricted/deleted entries in series directories:
        # visible=false, an empty title, and sometimes no usable page metadata.
        # This explicit flag is distinct from a malformed visible work. Never
        # invent a title/ID or enqueue a download for such an inaccessible member.
        if not isinstance(entry, Mapping) or entry.get("visible") is not False:
            return False
        if not isinstance(entry.get("title"), str):
            raise ConnectorError("Pixiv 返回的系列占位作品格式无效")
        ident = entry.get("id")
        if not (type(ident) is int and ident == 0):
            cls._positive_id(ident)
        count = entry.get("page_count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise ConnectorError("Pixiv 返回的系列占位作品格式无效")
        return True

    def fetch(self, value, *, download=True, checkpoint=lambda value: None, include_preview=True):
        remote_id = self.normalize_id(value)
        api = self._api()
        novel = remote_id.startswith("novel:")
        ident = remote_id.split(":")[-1]
        data = self._check(api.novel_detail(ident) if novel else api.illust_detail(ident))
        work = data.get("novel" if novel else "illust") or (data if data.get("id") else None)
        if not isinstance(work, Mapping) or not work:
            raise ConnectorError("Pixiv 返回的作品数据不完整")
        if download and work.get("type") == "ugoira":
            raise ConnectorError("动图尚未实现帧序列与时间轴保存，请先导入本地文件")
        result = self._metadata_work(work, novel=novel, metadata_only=not download)
        if result.remote_id != remote_id:
            raise ConnectorError("Pixiv 返回的作品编号与请求不一致")
        if not download or novel:
            checkpoint(10)
            previews = work.get("image_urls") or {}
            if not isinstance(previews, Mapping):
                previews = {}
            if include_preview:
                result.preview_cover = fetch_preview_cover(previews.get("medium") or previews.get("large") or previews.get("square_medium"),
                    ("pximg.net",), headers={"Referer": "https://www.pixiv.net/"})
            if not download:
                return result
        checkpoint(10)
        if novel:
            from pixivpy3 import PixivError
            try:
                body = self._check(api.webview_novel(ident))
            except PixivError as exc:
                # The SDK exception may contain the entire remote HTML body.
                raise ConnectorError("Pixiv 小说正文读取失败，请检查网络、权限或更新适配器") from exc
            if body.get("id") is not None and self._positive_id(body["id"]) != ident:
                raise ConnectorError("Pixiv 小说正文编号与请求不一致")
            # pixivpy exposes the parsed Pixiv novel object through webview_novel.
            nested = body.get("novel")
            text = body.get("text") or body.get("novel_text") or (nested.get("text") if isinstance(nested, Mapping) else None)
            if not isinstance(text, str) or not text:
                raise ConnectorError("Pixiv 小说正文格式已变化，请更新适配器；未保存空正文")
            if len(text.encode("utf-8")) > 16 * 1024 * 1024:
                raise ConnectorError("小说正文超过 16 MiB 限制")
            result.files.append((f"{ident}.txt", text.encode("utf-8")))
            self._novel_images(api, body, text, result, checkpoint)
        else:
            pages = work.get("meta_pages") or []
            if not isinstance(pages, list) or any(not isinstance(p, Mapping) or not isinstance(p.get("image_urls"), Mapping) for p in pages):
                raise ConnectorError("Pixiv 返回的多 P 图片目录格式无效")
            urls = [p["image_urls"].get("original") for p in pages]
            if not urls and result.remote_page_count == 1:
                single = work.get("meta_single_page") or {}
                urls = [single.get("original_image_url")] if isinstance(single, Mapping) else []
            if (result.remote_page_count is None or not urls or len(urls) != result.remote_page_count
                    or len(urls) > 2000 or any(not isinstance(url, str) or not url for url in urls)):
                raise ConnectorError("Pixiv 原图链接与作品页数不一致或超过 2000 页限制；未保存不完整作品")
            if len(set(urls)) != len(urls):
                raise ConnectorError("Pixiv 多 P 图片目录包含重复链接；未保存不完整作品")
            suffixes = []
            for url in urls:
                validate_url(url, ("pximg.net",))
                suffix = PurePosixPath(urlparse(url).path).suffix.lower()
                if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    raise ConnectorError("Pixiv 返回不支持的图片格式")
                suffixes.append(suffix)
            client = SafeHTTP(("pximg.net",), headers={"Referer": "https://www.pixiv.net/"})
            try:
                total = 0
                for index, url in enumerate(urls):
                    checkpoint(10 + int(80 * index / len(urls)))
                    content = client.fetch(url)
                    total += len(content)
                    if total > 512 * 1024 * 1024:
                        raise ConnectorError("作品超过 512 MiB 导入限制")
                    result.files.append((f"{index + 1:05d}{suffixes[index]}", content))
            finally:
                client.close()
        return result

    def fetch_novel_images(self, value, text, *, checkpoint=lambda value: None, existing_keys=()):
        """Backfill the immutable local text, never replace it with a newer edit."""
        ident = self.normalize_id(value)
        if not ident.startswith("novel:"):
            raise ConnectorError("仅 Pixiv 小说支持补抓正文插图")
        api = self._api()
        from pixivpy3 import PixivError
        try:
            body = self._check(api.webview_novel(ident.split(":")[-1]))
        except PixivError as exc:
            raise ConnectorError("Pixiv 小说插图目录读取失败") from exc
        if body.get("id") is not None and self._positive_id(body["id"]) != ident.split(":")[-1]:
            raise ConnectorError("Pixiv 小说正文编号与请求不一致")
        result = RemoteWork(remote_id=ident, title="插图补全", source_url="", item_type="novel")
        self._novel_images(api, body, text, result, checkpoint, existing_keys=existing_keys)
        return result

    def _novel_images(self, api, body, text, result, checkpoint, *, existing_keys=()):
        from app.novel_text import image_keys
        keys = [key for key in image_keys(text) if key not in existing_keys]
        if not keys:
            return
        images = body.get("images") or {}
        if not isinstance(images, Mapping):
            raise ConnectorError("Pixiv 小说插图目录格式无效")
        resolved, artwork = {}, {}
        for key in keys:
            checkpoint(12)
            kind, ident = key.split(":")
            if kind == "uploadedimage":
                entry = images.get(ident)
                urls = entry.get("urls") if isinstance(entry, Mapping) else None
                url = urls.get("original") if isinstance(urls, Mapping) else None
            else:
                ident, _, page = ident.partition("-")
                page = int(page or "1") - 1
                if ident not in artwork:
                    entry = self._check(api.illust_detail(ident)).get("illust")
                    if not isinstance(entry, Mapping) or self._positive_id(entry.get("id")) != ident:
                        raise ConnectorError("小说引用的 Pixiv 插画不可用")
                    pages = entry.get("meta_pages") or []
                    artwork[ident] = [p.get("image_urls", {}).get("original") for p in pages] if isinstance(pages, list) and pages else [(entry.get("meta_single_page") or {}).get("original_image_url")]
                urls = artwork[ident]
                url = urls[page] if page < len(urls) else None
            if not isinstance(url, str) or not url:
                raise ConnectorError("小说部分正文插图不可用；原正文已保留，可在重试队列重试")
            validate_url(url, ("pximg.net",))
            suffix = PurePosixPath(urlparse(url).path).suffix.lower()
            if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                raise ConnectorError("Pixiv 小说插图格式不支持")
            resolved[key] = (url, suffix)
        client = SafeHTTP(("pximg.net",), headers={"Referer": "https://www.pixiv.net/"})
        try:
            total, downloaded = 0, {}
            for index, (key, (url, suffix)) in enumerate(resolved.items()):
                checkpoint(15 + int(70 * index / len(resolved)))
                if url not in downloaded:
                    content = client.fetch(url)
                    total += len(content)
                    if total > 512 * 1024 * 1024:
                        raise ConnectorError("小说插图超过 512 MiB 限制")
                    from app.download_staging import stage
                    downloaded[url] = stage(content)
                name = "novel-" + key.replace(":", "-") + suffix
                list.append(result.files, (name, downloaded[url]))
                result.inline_images[name] = key
        finally:
            client.close()

    @classmethod
    def normalize_author(cls, value):
        value = value.strip()
        if value == "me":
            return {"remote_id": "me", "section": "works", "category": "illustration", "restrict": "public"}
        if re.fullmatch(r"[1-9]\d{0,19}", value):
            return {"remote_id": value, "section": "works", "category": "illustration", "restrict": "public"}
        validate_url(value, ("pixiv.net",))
        parsed = urlparse(value)
        path = re.sub(r"^/[a-z]{2}(?:-[A-Za-z]{2,8})?(?=/)", "", parsed.path)
        match = re.fullmatch(r"/users/([1-9]\d{0,19})(?:/(artworks|illustrations|manga|novels|bookmarks/(?:artworks|novels)))?/?", path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.hostname not in ("www.pixiv.net", "pixiv.net") or not match or parsed.fragment
                or set(params) - {"rest", "mode"} or any(len(v) != 1 for v in params.values())
                or params.get("rest", ["show"])[0] not in ("show", "hide")
                or params.get("mode", ["all"])[0] not in ("all", "illust", "manga")):
            raise ConnectorError("请输入 Pixiv 作者主页、作品或收藏页链接")
        suffix = match[2] or ""
        return {"remote_id": match[1], "section": "bookmarks" if suffix.startswith("bookmarks/") else "works",
                "category": "novel" if suffix.endswith("novels") else "manga" if suffix == "manga" or params.get("mode") == ["manga"] else "illustration",
                "restrict": "private" if params.get("rest") == ["hide"] else "public"}

    def fetch_author(self, target, checkpoint=lambda value: None):
        api = self._api()
        ident = self._positive_id(getattr(self, "_authenticated_user_id", "") or getattr(self, "_user_id", "")) if target == "me" else self._positive_id(target)
        checkpoint(10)
        response = self._check(api.user_detail(ident))
        user, profile = response.get("user"), response.get("profile") or {}
        if not isinstance(user, Mapping) or self._positive_id(user.get("id")) != ident or not isinstance(user.get("name"), str) or not user["name"].strip():
            raise ConnectorError("Pixiv 作者资料格式不完整")
        statistics = {key: value for key, value in profile.items() if key in (
            "total_illusts", "total_manga", "total_novels", "total_illust_bookmarks", "total_novel_bookmarks") and type(value) is int and value >= 0} if isinstance(profile, Mapping) else {}
        images = user.get("profile_image_urls") or {}
        avatar = fetch_preview_cover(images.get("medium"), ("pximg.net",), headers={"Referer": "https://www.pixiv.net/"}) if isinstance(images, Mapping) else None
        return {"remote_id": ident, "name": user["name"][:512],
                "description": html.unescape(re.sub("<[^>]*>", "", str(user.get("comment") or "")))[:20000],
                "statistics": statistics, "avatar": avatar,
                "is_self": ident == getattr(self, "_authenticated_user_id", "")}

    @classmethod
    def author_cursor(cls, next_url, ident, section, category, restrict):
        if next_url is None:
            return None
        if not isinstance(next_url, str):
            raise ConnectorError("Pixiv 作者分页链接无效")
        validate_url(next_url, ("app-api.pixiv.net",))
        parsed = urlparse(next_url)
        novel = category == "novel"
        path = ("/v1/user/bookmarks/novel" if novel else "/v1/user/bookmarks/illust") if section == "bookmarks" else ("/v1/user/novels" if novel else "/v1/user/illusts")
        key = "max_bookmark_id" if section == "bookmarks" else "offset"
        params = parse_qs(parsed.query, keep_blank_values=True)
        allowed = {"user_id", "filter", key} | ({"restrict"} if section == "bookmarks" else ({"type"} if not novel else set()))
        if (parsed.hostname != "app-api.pixiv.net" or parsed.path != path or parsed.fragment or set(params) - allowed
                or any(len(v) != 1 for v in params.values()) or params.get("user_id") != [ident]
                or params.get("filter", ["for_ios"]) != ["for_ios"]
                or section == "bookmarks" and params.get("restrict", [restrict]) != [restrict]
                or section == "works" and not novel and params.get("type", ["manga" if category == "manga" else "illust"]) != ["manga" if category == "manga" else "illust"]
                or key not in params):
            raise ConnectorError("Pixiv 作者分页链接越界，已停止读取")
        return {key: cls._positive_id(params[key][0])}

    def fetch_author_page(self, ident, section, category, restrict, cursor=None, checkpoint=lambda value: None):
        ident = self._positive_id(ident)
        if section not in ("works", "bookmarks") or category not in ("illustration", "manga", "novel") or restrict not in ("public", "private"):
            raise ConnectorError("Pixiv 作者目录类型无效")
        if section == "works" and restrict != "public":
            raise ConnectorError("作者作品目录不支持私密收藏参数")
        api = self._api()
        if restrict == "private" and ident != getattr(self, "_authenticated_user_id", ""):
            raise ConnectorError("只能读取当前登录 Pixiv 账号自己的私密收藏")
        cursor = cursor or {}
        key = "max_bookmark_id" if section == "bookmarks" else "offset"
        if set(cursor) - {key}:
            raise ConnectorError("Pixiv 作者分页游标无效")
        args = {key: self._positive_id(cursor[key])} if key in cursor else {}
        checkpoint(20)
        novel = category == "novel"
        if section == "bookmarks":
            response = api.user_bookmarks_novel(ident, restrict=restrict, **args) if novel else api.user_bookmarks_illust(ident, restrict=restrict, **args)
        else:
            response = api.user_novels(ident, **args) if novel else api.user_illusts(ident, type="manga" if category == "manga" else "illust", **args)
        response = self._check(response)
        entries = response.get("novels" if novel else "illusts")
        if not isinstance(entries, list) or len(entries) > 500:
            raise ConnectorError("Pixiv 作者作品列表格式无效")
        works, unavailable = [], 0
        for entry in entries:
            if self._unavailable_series_entry(entry):
                unavailable += 1
                continue
            if not isinstance(entry, Mapping):
                raise ConnectorError("Pixiv 作者作品格式无效")
            if not novel and entry.get("type") not in ("illust", "manga", "ugoira"):
                raise ConnectorError("Pixiv 返回未知的图像作品类型，未保存错误分类")
            if not novel and entry.get("type") == "ugoira":
                unavailable += 1
                continue
            work = self._metadata_work(entry, novel=novel)
            if section == "works" and work.author_id != ident:
                raise ConnectorError("Pixiv 作者目录混入其他作者，未保存")
            if work.item_type == category:
                works.append(work)
            elif section == "works":
                raise ConnectorError("Pixiv 作者目录作品类型不一致")
        next_cursor = self.author_cursor(response.get("next_url"), ident, section, category, restrict)
        if next_cursor is not None and next_cursor == cursor:
            raise ConnectorError("Pixiv 作者分页游标重复，未保存重复页")
        return {"works": works, "next_cursor": next_cursor, "raw_count": len(entries), "unavailable": unavailable}

    def follows(self, checkpoint=lambda value: None):
        api = self._api()
        user_id = getattr(self, "_authenticated_user_id", "")
        if not user_id:
            raise ConnectorError("Pixiv 登录响应缺少账号身份，请重新授权主账号后同步关注")
        results = []
        for restrict in ("public", "private"):
            params = {"user_id": user_id, "restrict": restrict}
            seen = set()
            for page in range(100):
                checkpoint(min(90, page))
                response = self._check(api.user_following(**params))
                results.extend({"target_type": "author", "target": str(p["user"]["id"]),
                                "label": p["user"]["name"]} for p in response.get("user_previews", []))
                next_url = response.get("next_url")
                if not next_url:
                    break
                validate_url(next_url, ("app-api.pixiv.net",))
                parsed = urlparse(next_url)
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (parsed.hostname != "app-api.pixiv.net" or parsed.path != "/v1/user/following" or parsed.fragment
                        or set(query) - {"user_id", "restrict", "offset", "filter"}
                        or any(len(values) != 1 for values in query.values())
                        or query.get("user_id") != [user_id] or query.get("restrict", [restrict]) != [restrict]
                        or query.get("filter", ["for_ios"]) != ["for_ios"]
                        or not re.fullmatch(r"[0-9]{1,20}", query.get("offset", [""])[0])):
                    raise ConnectorError("Pixiv 关注分页超出主账号范围，已停止同步")
                offset = int(query["offset"][0])
                if offset in seen:
                    raise ConnectorError("Pixiv 关注分页重复，已停止同步")
                seen.add(offset)
                params = {"user_id": user_id, "restrict": restrict, "offset": offset}
            else:
                raise ConnectorError("关注列表超过单次 100 页限制；尚未标记同步完成")
        return results

    def watchlist(self, checkpoint=lambda value: None):
        """Read both official App API watchlists, committing only complete snapshots."""
        api = self._api()
        if not getattr(self, "_authenticated_user_id", ""):
            raise ConnectorError("Pixiv 追更列表缺少登录身份，请重新授权主账号")
        result = []
        for index, kind in enumerate(("novel", "manga")):
            params, offsets, seen = {"filter": "for_ios"}, set(), set()
            for page in range(51):
                checkpoint(min(85, index * 45 + page))
                raw = api.no_auth_requests_call("GET", "https://app-api.pixiv.net/v1/watchlist/" + kind,
                                                params=params, req_auth=True)
                doc = self._check(api.parse_result(raw))
                entries = doc.get("series")
                if not isinstance(entries, list) or len(entries) > 100 or "next_url" not in doc:
                    raise ConnectorError("Pixiv 追更列表格式不完整，原列表已保留")
                for entry in entries:
                    if not isinstance(entry, Mapping):
                        raise ConnectorError("Pixiv 追更系列格式无效")
                    ident = self._positive_id(entry.get("id"))
                    if ident in seen:
                        raise ConnectorError("Pixiv 追更分页出现重复系列，原列表已保留")
                    seen.add(ident)
                    if len(seen) > (1000 if kind == "novel" else 500):
                        raise ConnectorError("Pixiv 追更列表超过单次同步容量")
                    user, count = entry.get("user"), entry.get("published_content_count")
                    if (not isinstance(entry.get("title"), str) or not entry["title"].strip()
                            or len(entry["title"]) > 512 or not isinstance(user, Mapping)
                            or not isinstance(user.get("name"), str) or type(count) is not int or not 0 <= count <= 1000000):
                        raise ConnectorError("Pixiv 追更系列资料不完整，原列表已保留")
                    latest = entry.get("latest_content_id")
                    stamp = entry.get("last_published_content_datetime")
                    if stamp is not None:
                        from datetime import datetime
                        try:
                            if len(stamp) > 64: raise ValueError()
                            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        except (ValueError, TypeError, AttributeError):
                            raise ConnectorError("Pixiv 追更更新时间格式无效") from None
                    result.append({"remote_id": ident, "kind": kind, "title": entry["title"],
                        "author": user["name"][:191], "author_id": self._positive_id(user.get("id")),
                        "content_count": count, "latest_content_id": self._positive_id(latest) if latest is not None else None,
                        "remote_updated_at": stamp})
                following = doc.get("next_url")
                if following is None: break
                validate_url(following, ("app-api.pixiv.net",))
                parsed = urlsplit(following)
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (parsed.hostname != "app-api.pixiv.net" or parsed.path != "/v1/watchlist/" + kind
                        or parsed.fragment or parsed.username or parsed.password or parsed.port not in (None, 443)
                        or set(query) - {"offset", "limit", "filter"} or any(len(v) != 1 for v in query.values())
                        or query.get("filter", ["for_ios"]) != ["for_ios"]
                        or not re.fullmatch(r"[0-9]{1,8}", query.get("offset", [""])[0])
                        or not re.fullmatch(r"[1-9][0-9]?|100", query.get("limit", ["30"])[0])):
                    raise ConnectorError("Pixiv 追更分页越界，已停止同步")
                offset = int(query["offset"][0])
                if offset in offsets or offset <= params.get("offset", 0):
                    raise ConnectorError("Pixiv 追更分页重复，已停止同步")
                offsets.add(offset)
                params = {"offset": offset, "limit": int(query.get("limit", ["30"])[0]), "filter": "for_ios"}
            else:
                raise ConnectorError("Pixiv 追更分页超过限制，未替换原列表")
        return result

    def discover(self, target_type, target, content_scope="images", *, include_author_bookmarks=False):
        if content_scope not in ("images", "novels", "all"):
            raise ConnectorError("Pixiv 内容范围必须是 images、novels 或 all")
        if target_type not in ("author", "tag", "search"):
            raise ConnectorError("Pixiv 定时订阅支持 author、tag、search")
        if target_type == "author":
            if not isinstance(target, str) or not re.fullmatch(r"[1-9]\d{0,19}", target):
                raise ConnectorError("Pixiv 作者订阅使用数字作者 ID")
        api = self._api()
        identities, has_more = [], False
        self.bookmark_observations = {}
        self.discovery_origins = {}
        def collect(response, novel=False, origin="works"):
            nonlocal has_more
            response = self._check(response)
            works = response.get("novels" if novel else "illusts")
            if not isinstance(works, list):
                raise ConnectorError("Pixiv 返回的作品列表格式无效")
            for work in works:
                if not isinstance(work, Mapping):
                    raise ConnectorError("Pixiv 返回的作品列表格式无效")
                ident = self._positive_id(work.get("id"))
                identity = "novel:" + ident if novel else ident
                identities.append(identity)
                self.discovery_origins.setdefault(identity, set()).add(origin)
                if type(work.get("is_bookmarked")) is bool:
                    self.bookmark_observations[identity] = work["is_bookmarked"]
            has_more = has_more or bool(response.get("next_url"))
        if content_scope in ("images", "all"):
            if target_type == "author":
                # SDK defaults to type=illust, which omits the author's manga.
                collect(api.user_illusts(target, type="illust"))
                collect(api.user_illusts(target, type="manga"))
                if include_author_bookmarks:
                    collect(api.user_bookmarks_illust(target, restrict="public"), origin="bookmarks")
            else:
                collect(api.search_illust(target, search_target="exact_match_for_tags" if target_type == "tag" else "title_and_caption", sort="date_desc"))
        if content_scope in ("novels", "all"):
            if target_type == "author":
                collect(api.user_novels(target), novel=True)
                if include_author_bookmarks:
                    collect(api.user_bookmarks_novel(target, restrict="public"), novel=True, origin="bookmarks")
            else:
                collect(api.search_novel(target, search_target="exact_match_for_tags" if target_type == "tag" else "keyword", sort="date_desc"), novel=True)
        return list(dict.fromkeys(identities)), has_more

    @classmethod
    def _series_cursor(cls, next_url, ident, kind):
        if next_url is None:
            return None
        if not isinstance(next_url, str) or not next_url:
            raise ConnectorError("Pixiv 返回的系列分页链接无效")
        validate_url(next_url, ("app-api.pixiv.net",))
        parsed = urlparse(next_url)
        path = "/v2/novel/series" if kind == "novel" else "/v1/illust/series"
        identity_key = "series_id" if kind == "novel" else "illust_series_id"
        query = parse_qs(parsed.query, keep_blank_values=True)
        cursor_keys = {"last_order"} if kind == "novel" else {"last_order", "offset"}
        if (parsed.hostname != "app-api.pixiv.net" or parsed.path != path or parsed.fragment
                or set(query) - (cursor_keys | {identity_key, "filter"})
                or any(len(values) != 1 for values in query.values())
                or query.get(identity_key) != [ident]
                or ("filter" in query and query["filter"] != ["for_ios"])):
            raise ConnectorError("Pixiv 系列分页链接越界，已停止读取")
        present = set(query) & cursor_keys
        if len(present) != 1:
            raise ConnectorError("Pixiv 系列分页游标无效")
        key = present.pop()
        return {key: cls._positive_id(query[key][0])}

    def fetch_series(self, remote_id, kind, checkpoint=lambda value: None):
        ident, kind = self.normalize_series(remote_id, kind)
        checkpoint(0)
        api = self._api()
        works, seen_ids, seen_cursors = [], set(), set()
        unavailable = False
        cursor = {}
        title = None
        for page in range(self.MAX_SERIES_PAGES):
            checkpoint(min(85, int(85 * len(works) / self.MAX_SERIES_WORKS)))
            if kind == "novel":
                response = self._check(api.novel_series(ident, **cursor))
            else:
                # pixivpy3 has no wrapper for this App API. Reuse its authenticated
                # transport (including proxy, pacing and cancellation), never a
                # caller-controlled URL. Published implementations:
                # github.com/C13H12N4O2/Pixiv_Crawler/blob/master/Pixiv_Crawler.py
                # github.com/FlanChanXwO/pixiv-cli/tree/main/internal/services/pixiv/endpoint/artwork/series
                raw = api.no_auth_requests_call("GET", "https://app-api.pixiv.net/v1/illust/series",
                    params={"illust_series_id": ident, **cursor}, req_auth=True)
                response = self._check(api.parse_result(raw))
            detail = response.get("novel_series_detail" if kind == "novel" else "illust_series_detail")
            entries = response.get("novels" if kind == "novel" else "illusts")
            if (not isinstance(detail, Mapping) or not isinstance(detail.get("title"), str)
                    or not detail["title"].strip() or not isinstance(entries, list) or "next_url" not in response):
                raise ConnectorError("Pixiv 系列目录格式已变化，未保存不完整目录")
            if detail.get("id") is not None and self._positive_id(detail["id"]) != ident:
                raise ConnectorError("Pixiv 返回的系列编号与请求不一致")
            if title is None:
                title = detail["title"]
            source_series = {"remote_id": ident, "title": title, "kind": kind}
            for entry in entries:
                if self._unavailable_series_entry(entry):
                    unavailable = True
                    continue
                work = self._metadata_work(entry, novel=kind == "novel", source_series=source_series)
                if work.remote_id in seen_ids:
                    continue
                if len(works) >= self.MAX_SERIES_WORKS:
                    return {**source_series, "complete": False, "works": works}
                works.append(work)
                seen_ids.add(work.remote_id)
            following = self._series_cursor(response.get("next_url"), ident, kind)
            if following is None:
                return {**source_series, "complete": not unavailable, "works": works}
            marker = tuple(following.items())
            if marker in seen_cursors:
                raise ConnectorError("Pixiv 系列分页出现循环，未保存不完整目录")
            seen_cursors.add(marker)
            if len(works) >= self.MAX_SERIES_WORKS:
                return {**source_series, "complete": False, "works": works}
            cursor = following
        return {"remote_id": ident, "title": title or "", "kind": kind, "complete": False, "works": works}
