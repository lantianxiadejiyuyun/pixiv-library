"""Synthetic platform responses at real HTTP/SDK boundaries; no live accounts."""
import io
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from PIL import Image
import pytest
import requests

from app import network, queue_control
from app.integrations import base, ehentai, jm, picacg, pixiv
from app.integrations.base import JobCancelled, SafeHTTP


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (8, 12), "white").save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def transport(monkeypatch):
    trace, outgoing, cooldowns = [], [], []
    routes = {}
    real_client, real_validate = httpx.Client, base.validate_url
    monkeypatch.setattr(network, "httpx_proxy", lambda: None)
    monkeypatch.setattr(network, "requests_proxies", lambda: {"http": "", "https": "", "all": ""})
    monkeypatch.setattr(base, "validate_url", lambda url, hosts, resolve=False: real_validate(url, hosts))
    monkeypatch.setattr(jm, "validate_url", lambda url, hosts, resolve=False: real_validate(url, hosts))
    monkeypatch.setattr(queue_control, "wait_for_request", lambda: trace.append("gate"))
    monkeypatch.setattr(queue_control, "defer_requests", cooldowns.append)

    def dispatch(method, url, headers):
        trace.append("http")
        outgoing.append((method, url, dict(headers)))
        if url in routes:
            value = routes[url]
            return value(method, url, headers) if callable(value) else value
        path = urlsplit(url).path
        if path in routes:
            value = routes[path]
            return value(method, url, headers) if callable(value) else value
        raise AssertionError("Unexpected synthetic platform request; real network is forbidden")

    def http_handler(request):
        value = dispatch(request.method, str(request.url), request.headers)
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json=value) if isinstance(value, dict) else httpx.Response(200, content=value)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(http_handler), **kwargs))

    def requests_send(adapter, request, **kwargs):
        value = dispatch(request.method, request.url, request.headers)
        response = requests.Response()
        response.status_code = value.status_code if isinstance(value, httpx.Response) else 200
        response.url, response.request = request.url, request
        response.headers.update(value.headers if isinstance(value, httpx.Response) else {"Content-Type": "application/json"})
        response._content = value.content if isinstance(value, httpx.Response) else json.dumps(value).encode()
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", requests_send)
    return SimpleNamespace(routes=routes, outgoing=outgoing, trace=trace, cooldowns=cooldowns)


def assert_paced(transport):
    assert transport.trace == [event for _ in transport.outgoing for event in ("gate", "http")]


def pixiv_metadata(transport, *, novel=False):
    transport.routes["/auth/token"] = {"response": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "user": {"id": 101}}}
    item = {"id": 123, "title": "Synthetic work", "type": "illust", "page_count": 1, "user": {"name": "Example"},
        "image_urls": {"medium": "https://i.pximg.net/cover.png", "large": "https://i.pximg.net/unused-large.png"},
        "meta_single_page": {"original_image_url": "https://i.pximg.net/original.png"}}
    transport.routes["/v2/novel/detail" if novel else "/v1/illust/detail"] = {"novel" if novel else "illust": item}
    transport.routes["/cover.png"] = image_bytes()
    return pixiv.PixivConnector({"refresh_token": "synthetic-refresh"})


@pytest.mark.parametrize("novel", [False, True])
def test_pixiv_metadata_fetches_only_one_cover_never_original_or_novel_text(transport, novel):
    connector = pixiv_metadata(transport, novel=novel)
    result = connector.fetch("novel:123" if novel else "123", download=False)
    assert result.metadata_only and result.files == []
    assert result.preview_cover == ("preview.png", image_bytes())
    assert len(transport.outgoing) == 3  # auth, detail, one medium cover
    assert transport.outgoing[-1][1] == "https://i.pximg.net/cover.png"
    assert "authorization" not in transport.outgoing[-1][2]
    assert_paced(transport)


def test_eh_metadata_uses_documented_thumb_without_gallery_index(transport):
    transport.routes["/api.php"] = {"gmetadata": [{"title": "Synthetic gallery", "filecount": "500",
        "thumb": "https://ehgt.org/cover.png", "tags": []}]}
    transport.routes["/cover.png"] = image_bytes()
    result = ehentai.EHentaiConnector({"ipb_member_id": "test-id", "ipb_pass_hash": "private-cookie"}).fetch("123:abcdef0123", download=False)
    assert result.metadata_only and result.files == [] and result.preview_cover[1] == image_bytes()
    assert len(transport.outgoing) == 2
    assert "cookie" not in transport.outgoing[-1][2]
    assert_paced(transport)


def test_picacg_metadata_uses_thumb_without_episode_or_page_requests(transport):
    ident = "a" * 24
    transport.routes[f"/comics/{ident}"] = {"code": 200, "data": {"comic": {"title": "Synthetic comic",
        "allowDownload": False, "thumb": {"path": "cover.png", "fileServer": "https://storage1.picacomic.com"}}}}
    transport.routes["/static/cover.png"] = image_bytes()
    result = picacg.PicACGConnector({"token": "private-token"}).fetch(ident, download=False)
    assert result.metadata_only and result.files == [] and result.preview_cover[1] == image_bytes()
    assert len(transport.outgoing) == 2
    assert transport.outgoing[0][2]["authorization"] == "private-token"
    assert "authorization" not in transport.outgoing[-1][2] and "cookie" not in transport.outgoing[-1][2]
    assert_paced(transport)


def jm_sdk(monkeypatch, transport):
    import jmcomic
    from curl_cffi import requests as curl_requests
    class Album:
        name, authors, tags, description, page_count = "Synthetic comic", [], [], "", 1
        def __iter__(self):
            yield SimpleNamespace(photo_id="124")
    def get(url, **kwargs):
        transport.trace.append("http")
        transport.outgoing.append(("GET", url, kwargs.get("headers", {})))
        assert url in {"https://18comic.vip/album/123", "https://18comic.vip/photo/124"}
        return SimpleNamespace(status_code=200, headers={})
    def album(sdk, ident):
        sdk.get("/album/" + ident)
        return Album()
    def chapter(sdk, ident, fetch_album):
        sdk.get("/photo/" + ident)
        return [SimpleNamespace(img_url="https://" + jmcomic.JmModuleConfig.DOMAIN_IMAGE_LIST[0] + "/page.png", scramble_id=0, aid=1, img_file_name="1")]
    monkeypatch.setattr(curl_requests, "get", get)
    monkeypatch.setattr(jmcomic.JmHtmlClient, "get_album_detail", album)
    monkeypatch.setattr(jmcomic.JmHtmlClient, "get_photo_detail", chapter)
    transport.routes["/media/albums/123_3x4.jpg"] = image_bytes()
    transport.routes["/page.png"] = image_bytes()
    return jm.JMConnector({})


def test_jm_metadata_uses_dedicated_sdk_cover_without_chapter(monkeypatch, transport):
    result = jm_sdk(monkeypatch, transport).fetch("123", download=False)
    assert result.metadata_only and result.files == [] and result.preview_cover[1] == image_bytes()
    assert len(transport.outgoing) == 2
    assert transport.outgoing[-1][1].endswith("/media/albums/123_3x4.jpg")
    assert_paced(transport)


@pytest.mark.parametrize("platform", ["pixiv", "ehentai", "picacg", "jm"])
def test_content_requests_are_paced_and_do_not_refetch_cover(platform, monkeypatch, transport):
    if platform == "pixiv":
        connector = pixiv_metadata(transport)
        transport.routes["/original.png"] = image_bytes()
        ident = "123"
    elif platform == "ehentai":
        transport.routes["/api.php"] = {"gmetadata": [{"title": "Synthetic", "filecount": "1", "thumb": "https://ehgt.org/unused-cover.png"}]}
        transport.routes["/g/123/abcdef0123/"] = b'<a href="/s/abc/123-1">page</a>'
        transport.routes["/s/abc/123-1"] = b'<img id="img" src="https://ehgt.org/page.png">'
        transport.routes["/page.png"] = image_bytes()
        connector, ident = ehentai.EHentaiConnector({}), "123:abcdef0123"
    elif platform == "picacg":
        ident = "a" * 24
        transport.routes[f"/comics/{ident}"] = {"code": 200, "data": {"comic": {"title": "Synthetic", "pagesCount": 1}}}
        transport.routes[f"/comics/{ident}/eps"] = {"code": 200, "data": {"eps": {"docs": [{"order": 1}], "pages": 1, "total": 1}}}
        transport.routes[f"/comics/{ident}/order/1/pages"] = {"code": 200, "data": {"pages": {"docs": [{"media": {"path": "page.png"}}], "pages": 1, "total": 1}}}
        transport.routes["/static/page.png"] = image_bytes()
        connector = picacg.PicACGConnector({"token": "test-token"})
    else:
        connector, ident = jm_sdk(monkeypatch, transport), "123"
    result = connector.fetch(ident, download=True)
    assert not result.metadata_only and len(result.files) == 1 and result.preview_cover is None
    assert len(transport.outgoing) >= 3
    assert not any("cover" in url or "/media/albums/" in url for _, url, _ in transport.outgoing)
    assert_paced(transport)


@pytest.mark.parametrize("at_request", [1, 2, 3])
def test_pixiv_sdk_and_cover_cancellation_preserve_original_exception(monkeypatch, transport, at_request):
    connector = pixiv_metadata(transport)
    calls = 0
    cancellation = queue_control.QueuePaused()
    def gate():
        nonlocal calls
        calls += 1
        if calls == at_request:
            raise cancellation
    monkeypatch.setattr(queue_control, "wait_for_request", gate)
    with pytest.raises(JobCancelled) as error:
        connector.fetch("123", download=False)
    assert error.value is cancellation
    assert len(transport.outgoing) == at_request - 1


def test_jm_sdk_cancellation_is_not_retried_or_wrapped(monkeypatch, transport):
    connector = jm_sdk(monkeypatch, transport)
    cancellation = queue_control.QueuePaused()
    def gate():
        raise cancellation
    monkeypatch.setattr(queue_control, "wait_for_request", gate)
    with pytest.raises(JobCancelled) as error:
        connector.fetch("123", download=False)
    assert error.value is cancellation and transport.outgoing == []


def test_cover_failure_preserves_basic_metadata_and_sets_rate_limit_cooldown(transport):
    connector = pixiv_metadata(transport)
    transport.routes["/cover.png"] = httpx.Response(429, headers={"Retry-After": "120"}, text="private-body")
    result = connector.fetch("123", download=False)
    assert result.title == "Synthetic work" and result.preview_cover is None and result.metadata_only
    assert transport.cooldowns == [120]
    assert "private-body" not in repr(result)


def test_pixiv_sdk_rate_limit_respects_retry_after_without_secret_error(transport):
    connector = pixiv_metadata(transport)
    transport.routes["/auth/token"] = httpx.Response(429, headers={"Retry-After": "75"}, text="private-refresh-token")
    with pytest.raises(base.ConnectorError) as error:
        connector.fetch("123", download=False)
    assert transport.cooldowns == [75]
    assert "private-refresh" not in str(error.value)


def test_safe_http_redirects_are_individually_paced(transport):
    transport.routes["/redirect.png"] = httpx.Response(302, headers={"Location": "/cover.png"})
    transport.routes["/cover.png"] = image_bytes()
    client = SafeHTTP(("pximg.net",))
    try:
        assert client.fetch("https://i.pximg.net/redirect.png") == image_bytes()
    finally:
        client.close()
    assert len(transport.outgoing) == 2
    assert_paced(transport)


def test_jm_retry_runs_gate_and_cancellation_exits_before_second_request(monkeypatch, transport):
    from curl_cffi import requests as curl_requests
    connector = jm_sdk(monkeypatch, transport)
    calls, gates = [], []
    def fail_once(url, **kwargs):
        calls.append(url)
        raise OSError("synthetic network error")
    cancellation = queue_control.QueuePaused()
    def gate():
        gates.append(True)
        if len(gates) == 2:
            raise cancellation
    monkeypatch.setattr(curl_requests, "get", fail_once)
    monkeypatch.setattr(queue_control, "wait_for_request", gate)
    with pytest.raises(JobCancelled) as error:
        connector.fetch("123", download=False)
    assert error.value is cancellation
    assert len(calls) == 1 and len(gates) == 2


def test_jm_rate_limit_is_seen_at_transport_before_sdk_status_validation(monkeypatch, transport):
    import jmcomic
    from curl_cffi import requests as curl_requests
    connector = jm_sdk(monkeypatch, transport)
    def limited(url, **kwargs):
        transport.trace.append("http")
        transport.outgoing.append(("GET", url, {}))
        return SimpleNamespace(status_code=429, headers={}, text="Too Many Requests", url=url)
    def detail(sdk, ident):
        return sdk.get_jm_html("/album/" + ident)
    monkeypatch.setattr(curl_requests, "get", limited)
    monkeypatch.setattr(jmcomic.JmHtmlClient, "get_album_detail", detail)
    with pytest.raises(jmcomic.JmcomicException):
        connector.fetch("123", download=False)
    assert transport.cooldowns == [60]
    assert len(transport.outgoing) == 1
    assert_paced(transport)


def test_preview_rejects_external_url_without_request(transport):
    assert base.fetch_preview_cover("https://127.0.0.1/private.png", ("pximg.net",)) is None
    assert transport.outgoing == []


@pytest.mark.parametrize("header,expected", [(None, 60), ("invalid", 60), ("NaN", 60), ("Infinity", 60), ("-30", 60), ("7200", 3600), ("30", 30)])
def test_rate_limit_cooldown_is_bounded_and_invalid_headers_are_safe(transport, header, expected):
    base.defer_rate_limit(header)
    assert transport.cooldowns == [expected]


def test_rate_limit_accepts_http_date(transport):
    base.defer_rate_limit(format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True))
    assert len(transport.cooldowns) == 1
    assert 118 <= transport.cooldowns[0] <= 120
