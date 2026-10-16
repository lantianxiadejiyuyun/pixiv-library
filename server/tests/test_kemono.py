import copy
import sys
from types import SimpleNamespace

import pytest

from app.integrations import kemono
from app.integrations.base import ConnectorError, JobCancelled
from app.integrations.kemono import KemonoConnector, media_url


POST = {
    "id": "post-1", "user": "creator-1", "service": "fanbox", "title": "测试作品",
    "content": "", "file": {"name": "first.jpg", "path": "/ab/cd/first.jpg"},
    "attachments": [{"name": "second.png", "path": "/ef/01/second.png"}], "embed": {},
    "tags": ["测试"],
}
IDENT = "fanbox:creator-1:post-1"


@pytest.fixture
def configured(monkeypatch):
    connector = KemonoConnector({"token": "must-not-be-sent"})
    post = copy.deepcopy(POST)
    calls = []
    media = []

    def request(path):
        calls.append(path)
        if path.endswith("/profile"):
            return {"id": "creator-1", "service": "fanbox", "name": "测试作者"}
        return {"post": post}

    class Images:
        def __init__(self, hosts, **kwargs):
            assert hosts == kemono.MEDIA_HOSTS
            assert "cookies" not in kwargs
            assert "Authorization" not in kwargs.get("headers", {})

        def fetch(self, url, **kwargs):
            media.append(url)
            return b"image-fixture"

        def close(self):
            pass

    monkeypatch.setattr(connector, "_request", request)
    monkeypatch.setattr(kemono, "SafeHTTP", Images)
    monkeypatch.setattr(kemono, "fetch_preview_cover", lambda url, hosts: media.append(url) or ("preview.jpg", b"preview"))
    return connector, post, calls, media


def test_public_identity_formats_and_credentials():
    connector = KemonoConnector({})
    connector.validate_credentials()
    assert connector.normalize_id(IDENT) == IDENT
    assert connector.normalize_id("https://kemono.cr/fanbox/user/creator-1/post/post-1#1") == IDENT
    assert connector.normalize_author("https://kemono.cr/fanbox/user/creator-1") == "fanbox:creator-1"
    assert connector.normalize_author("boosty:abc-def") == "boosty:abc-def"
    assert connector.supports_import and connector.supports_subscriptions and not connector.supports_sync


@pytest.mark.parametrize("value", [
    "https://kemono.cr.evil.invalid/fanbox/user/a/post/b", "https://evil.kemono.cr/fanbox/user/a/post/b",
    "http://kemono.cr/fanbox/user/a/post/b", "https://kemono.cr/fanbox/user/%2e%2e/post/b",
    "https://secret@kemono.cr/fanbox/user/a/post/b", "discord:a:b", "fanbox:../a:b", "fanbox:a:b:c", None,
])
def test_rejects_unsupported_or_untrusted_identities(value):
    with pytest.raises(ConnectorError):
        KemonoConnector({}).normalize_id(value)


@pytest.mark.parametrize("path", ["https://example.invalid/a.jpg", "//localhost/a.jpg", "/a/../b.jpg", "/a/%2e%2e/b.jpg", "/a.jpg?x=1", "/a\\b.jpg", "/a.jpg#x", None])
def test_media_paths_are_not_arbitrary_fetches(path):
    with pytest.raises(ConnectorError):
        media_url(path)


def test_metadata_fetches_only_one_preview(configured):
    connector, _post, calls, media = configured
    work = connector.fetch(IDENT, download=False)
    assert work.metadata_only and not work.files
    assert work.remote_page_count == 2
    assert work.author == "测试作者" and work.author_id == "fanbox:creator-1"
    assert work.preview_cover == ("preview.jpg", b"preview")
    assert media == ["https://img.kemono.cr/thumbnail/data/ab/cd/first.jpg"]
    assert calls == ["fanbox/user/creator-1/post/post-1", "fanbox/user/creator-1/profile"]


def test_image_order_counts_and_stage_progress(configured):
    connector, _post, _calls, media = configured
    progress = []
    work = connector.fetch(IDENT, checkpoint=progress.append)
    assert [name for name, _ in work.files] == ["00001.jpg", "00002.png"]
    assert work.item_type == "manga" and work.remote_page_count == 2
    assert not work.metadata_only and work.rating == "unknown"
    assert media == ["https://kemono.cr/data/ab/cd/first.jpg", "https://kemono.cr/data/ef/01/second.png"]
    assert progress == sorted(progress) and len(progress) >= 4


def test_article_body_is_inert_text_and_preserves_paragraphs(configured):
    connector, post, _calls, media = configured
    post.update(file={}, attachments=[], content="<p>第一段 &amp; 引号</p><script>secret()</script><style>.hidden{}</style><p>第二段<br>下一行</p>")
    work = connector.fetch(IDENT)
    assert work.item_type == "article" and work.remote_page_count == 1
    assert work.files == [("zz-content.txt", "第一段 & 引号\n\n第二段\n下一行".encode())]
    assert media == []


def test_inline_images_are_saved_once_and_long_text_is_not_truncated(configured):
    connector, post, _calls, media = configured
    text = "文章正文" * 6000
    post["content"] = f'<p>{text}</p><img src="/data/ab/cd/first.jpg"><img src="https://n1.kemono.cr/data/ab/cd/inline.jpg">'
    work = connector.fetch(IDENT)
    assert len(media) == 3 and work.remote_page_count == 4
    assert [name for name, _ in work.files] == ["00001.jpg", "00002.png", "00003.jpg", "zz-content.txt"]
    assert work.files[-1][1].decode() == text
    assert len(work.description) == 20000


@pytest.mark.parametrize("changes", [
    {"attachments": [{"name": "files.zip", "path": "/ab/cd/files.zip"}]},
    {"file": {"name": "movie.avi", "path": "/ab/cd/movie.avi"}},
    {"content": '<p>文章</p><img src="http://127.0.0.1/secret">'},
    {"content": '<iframe src="https://example.invalid/embed"></iframe>'},
    {"embed": {"url": "https://example.invalid/media"}},
])
def test_unsupported_media_never_marks_partial_post_complete(configured, changes):
    connector, post, _calls, media = configured
    post.update(changes)
    with pytest.raises(ConnectorError, match="未标记为已下载"):
        connector.fetch(IDENT)
    assert media == []
    assert connector.fetch(IDENT, download=False).metadata_only


@pytest.mark.parametrize("changes", [
    {"id": "other"}, {"service": []}, {"attachments": None}, {"file": None}, {"content": []},
    {"attachments": [{}]}, {"tags": [1]},
])
def test_malformed_post_is_rejected_safely(configured, changes):
    connector, post, _calls, _media = configured
    post.update(changes)
    with pytest.raises(ConnectorError):
        connector.fetch(IDENT)


def test_blank_primary_file_is_not_an_attachment(configured):
    connector, post, _calls, media = configured
    post.update(file={"name": "", "path": ""}, attachments=[], content="文章")
    assert len(connector.fetch(IDENT).files) == 1
    assert media == []


def test_empty_post_is_not_downloaded(configured):
    connector, post, _calls, _media = configured
    post.update(file={}, attachments=[], content="")
    with pytest.raises(ConnectorError, match="没有可保存"):
        connector.fetch(IDENT)


def test_cancellation_propagates_before_any_media_request(configured):
    connector, _post, _calls, media = configured

    def stop(_):
        raise JobCancelled()

    with pytest.raises(JobCancelled):
        connector.fetch(IDENT, checkpoint=stop)
    assert media == []


def test_total_download_size_is_bounded(configured, monkeypatch):
    connector, _post, _calls, _media = configured
    monkeypatch.setattr(kemono, "MAX_TOTAL_BYTES", 12)
    with pytest.raises(ConnectorError, match="512 MiB"):
        connector.fetch(IDENT)


def test_author_discovery_and_more_flag(monkeypatch):
    connector = KemonoConnector({})
    calls = []
    monkeypatch.setattr(connector, "_request", lambda path: calls.append(path) or {"props": {"count": 75, "limit": 50}, "results": [POST]})
    assert connector.discover("author", "https://kemono.cr/fanbox/user/creator-1") == ([IDENT], True)
    assert calls == ["fanbox/user/creator-1/posts?o=0"]


def test_search_and_tag_queries_are_encoded(monkeypatch):
    connector = KemonoConnector({})
    calls = []
    monkeypatch.setattr(connector, "_request", lambda path: calls.append(path) or {"count": 1, "posts": [POST]})
    assert connector.discover("search", "a & b") == ([IDENT], False)
    assert connector.discover("tag", "风景") == ([IDENT], False)
    assert calls == ["posts?o=0&q=a+%26+b", "posts?o=0&tag=%E9%A3%8E%E6%99%AF"]


@pytest.mark.parametrize("payload", [
    {"props": {"count": 1}, "results": []},
    {"props": {"count": "1"}, "results": [POST]},
    {"props": {"count": 0}, "results": [POST]},
    {"props": {"count": 2}, "results": [POST, POST]},
    {"props": {"count": 1}, "results": [{**POST, "user": "wrong-author"}]},
    {"props": {}, "results": [POST]},
])
def test_discovery_rejects_incomplete_or_mixed_author_pages(monkeypatch, payload):
    connector = KemonoConnector({})
    monkeypatch.setattr(connector, "_request", lambda path: payload)
    with pytest.raises(ConnectorError):
        connector.discover("author", "fanbox:creator-1")


def test_configured_endpoint_has_no_account_credentials(monkeypatch):
    calls = []
    guards = []
    monkeypatch.setitem(sys.modules, "app.endpoint_config", SimpleNamespace(
        get_endpoint=lambda platform: "https://api.example.invalid/mirror/v1/",
        guard_http_client=lambda client, endpoint: guards.append(endpoint)))

    class Transport:
        def __init__(self, hosts, **kwargs):
            assert hosts == ("api.example.invalid",)
            assert "cookies" not in kwargs
            assert "Authorization" not in kwargs["headers"]
            assert kwargs["headers"]["Accept"] == "text/css"

        def fetch(self, url, **kwargs):
            calls.append(url)
            assert kwargs["limit"] == 8 * 1024 * 1024
            return b'{"post": {}}'

        def close(self):
            pass

    monkeypatch.setattr(kemono, "SafeHTTP", Transport)
    assert KemonoConnector({"token": "private"})._request("fanbox/user/a/post/b") == {"post": {}}
    assert calls == ["https://api.example.invalid/mirror/v1/fanbox/user/a/post/b"]
    assert guards == ["https://api.example.invalid/mirror/v1"]


def test_bad_json_error_does_not_expose_response_or_credentials(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.endpoint_config", SimpleNamespace(
        get_endpoint=lambda platform: "https://kemono.cr/api/v1", guard_http_client=lambda *args: None))
    class Transport:
        def __init__(self, *args, **kwargs):
            pass
        def fetch(self, *args, **kwargs):
            return b"<html>secret response</html>"
        def close(self):
            pass
    monkeypatch.setattr(kemono, "SafeHTTP", Transport)
    with pytest.raises(ConnectorError) as error:
        KemonoConnector({})._request("test")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("suffix", ["mp4", "webm", "m4v", "mov"])
def test_video_attachments_saved_with_bounded_download_limit(configured, monkeypatch, suffix):
    connector, post, _calls, _media = configured
    post.update(file={"name": f"movie.{suffix}", "path": f"/ab/cd/movie.{suffix}"}, attachments=[])
    requests = []
    class Videos:
        def __init__(self, *args, **kwargs):
            pass
        def fetch(self, url, **kwargs):
            requests.append((url, kwargs["limit"]))
            return b"video-fixture"
        def close(self):
            pass
    monkeypatch.setattr(kemono, "SafeHTTP", Videos)
    work = connector.fetch(IDENT)
    assert work.item_type == "article" and work.remote_page_count == 1
    assert work.files == [(f"00001.{suffix}", b"video-fixture")]
    from app.config import settings
    assert requests == [(f"https://kemono.cr/data/ab/cd/movie.{suffix}", min(settings.max_file_bytes, kemono.MAX_TOTAL_BYTES))]


def test_top_level_videos_merge_without_duplicate_or_missing_files(configured, monkeypatch):
    connector, post, _calls, media = configured
    post["attachments"] = [{"name": "video.mp4", "path": "/ab/cd/video.mp4"}]
    post["content"] = '<p>视频说明</p><video src="/data/ab/cd/inline.webm"></video>'
    original = connector._request
    def request(path):
        data = original(path)
        if "post" in data:
            data["videos"] = [{"name": "video.mp4", "path": "/ab/cd/video.mp4", "server": "https://n1.kemono.cr", "extension": ".mp4"},
                              {"name": "extra.mov", "path": "/ab/cd/extra.mov"}]
        return data
    monkeypatch.setattr(connector, "_request", request)
    work = connector.fetch(IDENT)
    assert [name for name, _ in work.files] == ["00001.jpg", "00002.mp4", "00003.mov", "00004.webm", "zz-content.txt"]
    assert len(media) == 4 and work.remote_page_count == 5


def test_video_metadata_does_not_fetch_video_as_cover(configured):
    connector, post, _calls, media = configured
    post.update(file={"name": "movie.mp4", "path": "/ab/cd/movie.mp4"}, attachments=[])
    work = connector.fetch(IDENT, download=False)
    assert work.preview_cover is None and work.files == [] and media == []


@pytest.mark.parametrize("redirect", ["https://api.example.com/outside", "https://child.api.example.com/mirror/v1/post"])
def test_api_redirect_cannot_escape_configured_origin_or_prefix(monkeypatch, redirect):
    import httpx
    from app import endpoint_config, queue_control
    from app.integrations import base

    calls = []
    monkeypatch.setattr(endpoint_config, "get_endpoint", lambda platform: "https://api.example.com/mirror/v1")
    monkeypatch.setattr(endpoint_config, "public_dns", lambda host: None)
    monkeypatch.setattr(base.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443))])
    monkeypatch.setattr(queue_control, "wait_for_request", lambda: None)

    def send(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": redirect})

    class Transport(base.SafeHTTP):
        def __init__(self, hosts, **kwargs):
            self.hosts = tuple(hosts)
            self.proxy = None
            self.client = httpx.Client(transport=httpx.MockTransport(send), follow_redirects=False)

    monkeypatch.setattr(kemono, "SafeHTTP", Transport)
    with pytest.raises(ConnectorError, match="超出已配置"):
        KemonoConnector({})._request("fanbox/user/a/post/b")
    assert calls == ["https://api.example.com/mirror/v1/fanbox/user/a/post/b"]


def test_empty_video_element_cannot_be_marked_complete(configured):
    connector, post, _calls, media = configured
    post["content"] = "<p>说明</p><video controls></video>"
    with pytest.raises(ConnectorError, match="未标记为已下载"):
        connector.fetch(IDENT)
    assert media == []
