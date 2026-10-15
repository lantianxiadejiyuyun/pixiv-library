"""Real installed SDK parsing over synthetic transport; no account/network calls."""
import io
import json
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image
import pytest

from test_connector_stages import transport, pixiv_metadata, assert_paced
from app import queue_control
from app.integrations.base import ConnectorError
from app.integrations.pixiv import PixivConnector


def png(color):
    stream = io.BytesIO()
    Image.new("RGB", (8, 12), color).save(stream, "PNG")
    return stream.getvalue()


def work(ident, *, novel=False):
    return {"id": ident, "title": "Synthetic chapter " + str(ident), "type": "novel" if novel else "manga",
            "page_count": 2, "user": {"id": 77, "name": "Synthetic author"}, "tags": [{"name": "Synthetic"}],
            "series": {"id": 42, "title": "Synthetic series"},
            "image_urls": {"medium": "https://i.pximg.net/must-not-fetch-cover.png"}}


@pytest.mark.parametrize("value,kind,expected", [
    ("42", "novel", ("42", "novel")), ("42", "manga", ("42", "manga")),
    ("https://www.pixiv.net/novel/series/42", None, ("42", "novel")),
    ("https://www.pixiv.net/en/novel/series/42/", "novel", ("42", "novel")),
    ("https://www.pixiv.net/user/77/series/42", None, ("42", "manga")),
    ("https://pixiv.net/zh-tw/user/77/series/42/", "manga", ("42", "manga")),
])
def test_normalize_official_series_references(value, kind, expected):
    assert PixivConnector({}).normalize_series(value, kind) == expected
    if value.startswith("https:"):
        with pytest.raises(ConnectorError):
            PixivConnector({}).normalize_id(value)


@pytest.mark.parametrize("value,kind", [
    ("42", None), ("42", "illust"), ("0", "novel"),
    ("https://www.pixiv.net/novel/series/42", "manga"),
    ("https://www.pixiv.net/artworks/42", "manga"),
    ("https://www.pixiv.net/users/77/series/42", "manga"),
    ("https://www.pixiv.net/novel/series/42?next=private", None),
    ("https://www.pixiv.net.evil.invalid/novel/series/42", None),
    ("https://sub.pixiv.net/novel/series/42", None),
])
def test_reject_ambiguous_or_untrusted_series_references(value, kind):
    with pytest.raises(ConnectorError):
        PixivConnector({}).normalize_series(value, kind)


def test_real_sdk_novel_body_preserves_original_text_and_official_series(transport):
    connector = pixiv_metadata(transport, novel=True)
    transport.routes["/v2/novel/detail"]["novel"].update(page_count=2, series={"id": 42, "title": "Synthetic series"})
    original = "[chapter:Opening]\nFirst page\n[newpage]\n[chapter:Ending]\n[[rb:字 > zi]] <script>inert text</script>"
    raw = "<html><script>novel: " + json.dumps({"id": "123", "text": original, "seriesId": "42"}) + ", isOwnWork: false</script></html>"
    transport.routes["/webview/v2/novel"] = httpx.Response(200, text=raw)
    result = connector.fetch("novel:123", download=True)
    assert result.files == [("123.txt", original.encode())]
    assert result.remote_page_count == 2
    assert result.source_series == {"remote_id": "42", "title": "Synthetic series", "kind": "novel"}
    assert not result.metadata_only and result.preview_cover is not None
    assert len(transport.outgoing) == 4
    assert_paced(transport)


def test_real_sdk_malformed_novel_body_has_safe_actionable_error(transport):
    connector = pixiv_metadata(transport, novel=True)
    transport.routes["/webview/v2/novel"] = httpx.Response(200, text="private-remote-html-with-secret")
    with pytest.raises(ConnectorError, match="小说正文读取失败") as error:
        connector.fetch("novel:123")
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_multi_p_keeps_remote_order_and_each_original_without_cover(transport):
    connector = pixiv_metadata(transport)
    details = transport.routes["/v1/illust/detail"]["illust"]
    details.update(page_count=3, type="manga", series={"id": 42, "title": "Synthetic series"},
        meta_pages=[{"image_urls": {"original": f"https://i.pximg.net/{name}.png"}} for name in ("p9", "p0", "p3")])
    for name, color in zip(("p9", "p0", "p3"), ("red", "green", "blue")):
        transport.routes["/" + name + ".png"] = png(color)
    result = connector.fetch("123")
    assert result.remote_page_count == 3 and result.source_series["kind"] == "manga"
    assert result.files == [("00001.png", png("red")), ("00002.png", png("green")), ("00003.png", png("blue"))]
    assert [urlsplit(url).path for _, url, _ in transport.outgoing[2:]] == ["/p9.png", "/p0.png", "/p3.png"]
    assert_paced(transport)


@pytest.mark.parametrize("count,urls", [(None, ["https://i.pximg.net/p0.png"]),
    (2, ["https://i.pximg.net/p0.png"]), (2, ["https://i.pximg.net/p0.png", None]),
    (2, ["https://i.pximg.net/p0.png"] * 2), (1, []),
    (2, ["https://i.pximg.net/p0.png", "https://127.0.0.1/p1.png"])])
def test_multi_p_invalid_directory_fails_before_any_image_download(transport, count, urls):
    connector = pixiv_metadata(transport)
    details = transport.routes["/v1/illust/detail"]["illust"]
    details.update(page_count=count, meta_pages=[{"image_urls": {"original": url}} for url in urls], meta_single_page={})
    with pytest.raises(ConnectorError):
        connector.fetch("123")
    assert len(transport.outgoing) == 2


def test_multi_p_later_page_failure_never_returns_partial_work(transport):
    connector = pixiv_metadata(transport)
    transport.routes["/v1/illust/detail"]["illust"].update(page_count=2,
        meta_pages=[{"image_urls": {"original": f"https://i.pximg.net/p{n}.png"}} for n in range(2)])
    transport.routes["/p0.png"] = png("white")
    transport.routes["/p1.png"] = httpx.Response(503, text="private body must not appear")
    with pytest.raises(ConnectorError) as error:
        connector.fetch("123")
    assert "private" not in str(error.value) and len(transport.outgoing) == 4


def series_routes(transport, kind, pages):
    connector = pixiv_metadata(transport)
    path = "/v2/novel/series" if kind == "novel" else "/v1/illust/series"
    def respond(method, url, headers):
        query = parse_qs(urlsplit(url).query)
        assert headers["Authorization"] == "Bearer synthetic-access"
        return pages[query.get("last_order", query.get("offset", ["0"]))[0]]
    transport.routes[path] = respond
    return connector, path


@pytest.mark.parametrize("kind", ["novel", "manga"])
def test_real_sdk_series_pagination_only_catalogues_in_remote_order(transport, kind):
    novel = kind == "novel"
    path = "/v2/novel/series" if novel else "/v1/illust/series"
    identity = "series_id" if novel else "illust_series_id"
    key = "novels" if novel else "illusts"
    detail = {"id": 42, "title": "Synthetic series", "user": {"id": 77}}
    pages = {"0": {kind.replace("manga", "illust") + "_series_detail": detail, key: [work(999, novel=novel), work(12, novel=novel)],
        "next_url": f"https://app-api.pixiv.net{path}?{identity}=42&last_order=2"},
        "2": {kind.replace("manga", "illust") + "_series_detail": detail, key: [work(12, novel=novel), work(400, novel=novel)], "next_url": None}}
    connector, _ = series_routes(transport, kind, pages)
    result = connector.fetch_series("42", kind)
    prefix = "novel:" if novel else ""
    assert result["complete"] is True and result["remote_id"] == "42"
    assert [entry.remote_id for entry in result["works"]] == [prefix + str(n) for n in (999, 12, 400)]
    assert all(entry.metadata_only and not entry.files and entry.preview_cover is None and entry.remote_page_count == 2 for entry in result["works"])
    assert all(entry.source_series == {"remote_id": "42", "kind": kind, "title": "Synthetic series"} for entry in result["works"])
    assert len(transport.outgoing) == 3
    assert_paced(transport)


@pytest.mark.parametrize("next_url", [
    "https://evil.invalid/v2/novel/series?series_id=42&last_order=1",
    "https://app-api.pixiv.net/v1/user/novels?series_id=42&last_order=1",
    "https://app-api.pixiv.net/v2/novel/series?series_id=43&last_order=1",
    "https://app-api.pixiv.net/v2/novel/series?series_id=42&last_order=1&private_token=secret",
    "https://app-api.pixiv.net/v2/novel/series?series_id=42&last_order=1&last_order=2",
    "https://app-api.pixiv.net/v2/novel/series?series_id=42&offset=1",
    "https://app-api.pixiv.net/v2/novel/series?last_order=1",
    "https://app-api.pixiv.net/v2/novel/series?series_id=42&last_order=-1",
    "https://sub.app-api.pixiv.net/v2/novel/series?series_id=42&last_order=1",
])
def test_series_pagination_rejects_untrusted_or_wrong_identity_before_following(transport, next_url):
    connector, _ = series_routes(transport, "novel", {"0": {"novel_series_detail": {"id": 42, "title": "Synthetic"},
        "novels": [work(1, novel=True)], "next_url": next_url}})
    with pytest.raises(ConnectorError) as error:
        connector.fetch_series("42", "novel")
    assert len(transport.outgoing) == 2 and "secret" not in str(error.value)


def test_series_page_limit_and_offset_return_honest_incomplete_result(transport, monkeypatch):
    pages = {str(n): {"illust_series_detail": {"id": 42, "title": "Synthetic"}, "illusts": [work(n + 1)],
        "next_url": f"https://app-api.pixiv.net/v1/illust/series?illust_series_id=42&offset={n + 1}"} for n in range(2)}
    connector, _ = series_routes(transport, "manga", pages)
    monkeypatch.setattr(connector, "MAX_SERIES_PAGES", 2)
    result = connector.fetch_series("42", "manga")
    assert not result["complete"] and len(result["works"]) == 2 and len(transport.outgoing) == 3


def test_series_work_limit_is_bounded_without_silent_completion(transport, monkeypatch):
    connector, _ = series_routes(transport, "novel", {"0": {"novel_series_detail": {"id": 42, "title": "Synthetic"},
        "novels": [work(n, novel=True) for n in range(1, 5)], "next_url": None}})
    monkeypatch.setattr(connector, "MAX_SERIES_WORKS", 3)
    result = connector.fetch_series("42", "novel")
    assert not result["complete"] and len(result["works"]) == 3


def test_series_repeated_cursor_fails_instead_of_infinite_pagination(transport):
    page = {"novel_series_detail": {"id": 42, "title": "Synthetic"}, "novels": [work(1, novel=True)],
        "next_url": "https://app-api.pixiv.net/v2/novel/series?series_id=42&last_order=1"}
    connector, _ = series_routes(transport, "novel", {"0": page, "1": page})
    with pytest.raises(ConnectorError, match="循环"):
        connector.fetch_series("42", "novel")
    assert len(transport.outgoing) == 3


@pytest.mark.parametrize("scope,expected", [("images", ["11", "22"]), ("novels", ["novel:11"]), ("all", ["11", "22", "novel:11"])])
def test_author_discovery_includes_manga_and_keeps_novel_identity_namespace(transport, scope, expected):
    connector = pixiv_metadata(transport)
    transport.routes["/v1/user/illusts"] = lambda method, url, headers: {"illusts": [{"id": 11 if parse_qs(urlsplit(url).query)["type"] == ["illust"] else 22}], "next_url": None}
    transport.routes["/v1/user/novels"] = {"novels": [{"id": 11}], "next_url": None}
    assert connector.discover("author", "77", scope) == (expected, False)
    assert_paced(transport)


@pytest.mark.parametrize("target_type,search_target", [("tag", "exact_match_for_tags"), ("search", "keyword")])
def test_novel_search_uses_real_sdk_supported_target(transport, target_type, search_target):
    connector = pixiv_metadata(transport)
    transport.routes["/v1/search/novel"] = {"novels": [{"id": 123}], "next_url": "more"}
    assert connector.discover(target_type, "Synthetic", "novels") == (["novel:123"], True)
    assert parse_qs(urlsplit(transport.outgoing[-1][1]).query)["search_target"] == [search_target]


def test_series_cancellation_preserves_queue_pause_and_does_not_return_partial(transport, monkeypatch):
    page = {"novel_series_detail": {"id": 42, "title": "Synthetic"}, "novels": [work(1, novel=True)],
        "next_url": "https://app-api.pixiv.net/v2/novel/series?series_id=42&last_order=1"}
    connector, _ = series_routes(transport, "novel", {"0": page})
    cancellation = queue_control.QueuePaused()
    def gate():
        if len(transport.outgoing) >= 2:
            raise cancellation
    monkeypatch.setattr(queue_control, "wait_for_request", gate)
    with pytest.raises(queue_control.QueuePaused) as error:
        connector.fetch_series("42", "novel")
    assert error.value is cancellation and len(transport.outgoing) == 2


def hidden_series_member(ident=123):
    # Shape observed through the real App API: positive ID/page_count, blank
    # title, visible=false, restrict!=0, x_restrict=0 and series=null. All values
    # here are synthetic; no account or remote work data is retained.
    return {"id": ident, "title": "", "type": "illust", "image_urls": {
        "square_medium": "https://i.pximg.net/placeholder.png", "medium": "https://i.pximg.net/placeholder.png",
        "large": "https://i.pximg.net/placeholder.png"}, "caption": "", "restrict": 1,
        "user": {"id": 0, "name": "", "account": "", "profile_image_urls": {}, "is_accept_request": False},
        "tags": [], "tools": [], "page_count": 1, "width": 0, "height": 0, "sanity_level": 2,
        "x_restrict": 0, "series": None, "meta_single_page": {"original_image_url": ""}, "meta_pages": [],
        "total_view": 0, "total_bookmarks": 0, "is_bookmarked": False, "visible": False, "is_muted": False}


@pytest.mark.parametrize("kind", ["manga", "novel"])
def test_series_hidden_placeholder_does_not_abort_other_members_or_claim_complete(transport, kind):
    novel = kind == "novel"
    detail_key, key = ("novel_series_detail", "novels") if novel else ("illust_series_detail", "illusts")
    path = "/v2/novel/series" if novel else "/v1/illust/series"
    identity = "series_id" if novel else "illust_series_id"
    detail = {"id": 42, "title": "Synthetic"}
    pages = {"0": {detail_key: detail, key: [work(30, novel=novel), hidden_series_member(), work(10, novel=novel)],
        "next_url": f"https://app-api.pixiv.net{path}?{identity}=42&last_order=3"},
        "3": {detail_key: detail, key: [work(90, novel=novel)], "next_url": None}}
    connector, _ = series_routes(transport, kind, pages)
    result = connector.fetch_series("42", kind)
    assert result["complete"] is False
    assert [entry.remote_id for entry in result["works"]] == [("novel:" if novel else "") + str(ident) for ident in (30, 10, 90)]
    assert all(entry.metadata_only and not entry.files and entry.preview_cover is None for entry in result["works"])
    assert len(transport.outgoing) == 3  # auth + two catalog requests, no placeholder/cover fetch.
    assert_paced(transport)


def test_series_only_unavailable_members_returns_empty_incomplete_directory(transport):
    placeholder = hidden_series_member(0)
    placeholder["page_count"] = 0
    connector, _ = series_routes(transport, "manga", {"0": {"illust_series_detail": {"id": 42, "title": "Synthetic"},
        "illusts": [placeholder], "next_url": None}})
    result = connector.fetch_series("42", "manga")
    assert result["works"] == [] and result["complete"] is False


@pytest.mark.parametrize("changes", [
    {"visible": True}, {"visible": None}, {"visible": 0}, {"visible": "false"},
    {"id": None}, {"id": -1}, {"id": False}, {"title": None}, {"title": 12},
    {"page_count": -1}, {"page_count": False}, {"page_count": "1"},
])
def test_series_malformed_entry_is_not_mistaken_for_hidden_placeholder(transport, changes):
    placeholder = {**hidden_series_member(), **changes}
    connector, _ = series_routes(transport, "manga", {"0": {"illust_series_detail": {"id": 42, "title": "Synthetic"},
        "illusts": [work(10), placeholder], "next_url": None}})
    with pytest.raises(ConnectorError):
        connector.fetch_series("42", "manga")
    assert len(transport.outgoing) == 2


def test_single_hidden_work_remains_unavailable_instead_of_saving_blank_metadata(transport):
    connector = pixiv_metadata(transport)
    transport.routes["/v1/illust/detail"]["illust"] = hidden_series_member()
    with pytest.raises(ConnectorError, match="当前账号不可见"):
        connector.fetch("123", download=False)
    assert len(transport.outgoing) == 2
