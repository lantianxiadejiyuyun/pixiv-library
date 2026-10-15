import base64
import hashlib
import hmac

import pytest

from app.integrations.base import ConnectorError
from app.integrations.picacg import API_KEY, SIGNATURE_KEY, PicACGConnector, media_url, signed_headers

COMIC = "a" * 24
SECOND = "b" * 24


def test_signature_includes_endpoint_query_time_nonce_and_method():
    endpoint = f"comics/{COMIC}/eps?page=2"
    headers = signed_headers(endpoint, "GET", token="test-token", timestamp="1700000000", nonce="aabbcc")
    expected = hmac.new(SIGNATURE_KEY.encode(), (endpoint + "1700000000aabbccGET" + API_KEY).lower().encode(), hashlib.sha256).hexdigest()
    assert headers["signature"] == expected
    assert headers["authorization"] == "test-token"
    assert headers["signature"] != signed_headers(endpoint, "POST", timestamp="1700000000", nonce="aabbcc")["signature"]


def test_identity_and_credentials():
    connector = PicACGConnector({"token": "secret"})
    connector.validate_credentials()
    assert connector.normalize_id(COMIC.upper()) == COMIC
    assert connector.normalize_id(f"https://picaapi.picacomic.com/comics/{COMIC}") == COMIC
    with pytest.raises(ConnectorError):
        connector.normalize_id(f"https://picacomic.com.evil.example/comics/{COMIC}")
    with pytest.raises(ConnectorError):
        PicACGConnector({}).validate_credentials()


def test_media_paths_and_embedded_external_url_restriction():
    assert media_url({"path": "books/a.jpg"}) == "https://storage1.picacomic.com/static/books/a.jpg"
    assert media_url({"path": "tobs/books/a.jpg"}) == "https://storage-b.picacomic.com/static/books/a.jpg"
    assert media_url({"path": "tobeimg/books/a.jpg"}) == "https://img.picacomic.com/books/a.jpg"
    encoded = base64.b64encode(b"https://storage1.picacomic.com/static/a.jpg").decode().rstrip("=")
    assert media_url({"path": f"tobeimg/{encoded}.jpg", "fileServer": "https://storage1.picacomic.com"}).endswith("/static/a.jpg")
    bad = base64.b64encode(b"https://localhost/private").decode()
    with pytest.raises(ConnectorError):
        media_url({"path": f"tobeimg/{bad}.jpg", "fileServer": "https://storage1.picacomic.com"})


def test_login_uses_account_data_and_caches_token(monkeypatch):
    connector = PicACGConnector({"email": "test@example.invalid", "password": "private"})
    calls = []

    def request(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return {"token": "issued-token"}

    monkeypatch.setattr(connector, "_request", request)
    connector._ensure_token()
    connector._ensure_token()
    assert len(calls) == 1
    assert calls[0][0] == "auth/sign-in"
    assert calls[0][1]["auth"] is False
    assert connector._access_token == "issued-token"


def test_complete_episode_pagination_and_download_order(monkeypatch):
    from app.integrations import picacg
    connector = PicACGConnector({"token": "secret"})
    calls = []

    def request(endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint == f"comics/{COMIC}":
            return {"comic": {"title": "分页样本", "tags": ["测试"], "pagesCount": 3, "allowDownload": True}}
        if "/eps?" in endpoint:
            page = int(endpoint.split("page=")[1])
            return {"eps": {"docs": [{"order": 2 if page == 1 else 1}], "total": 2, "pages": 2}}
        if "/order/1/" in endpoint:
            page = int(endpoint.split("page=")[1])
            return {"pages": {"docs": [{"media": {"path": f"chapter1-{page}.png", "originalName": "page.png"}}], "total": 2, "pages": 2}}
        return {"pages": {"docs": [{"media": {"path": "chapter2.png", "originalName": "page.png"}}], "total": 1, "pages": 1}}

    class Images:
        def __init__(self, *args, **kwargs):
            pass
        def fetch(self, url):
            return url.encode()
        def close(self):
            pass

    monkeypatch.setattr(connector, "_request", request)
    monkeypatch.setattr(picacg, "SafeHTTP", Images)
    work = connector.fetch(COMIC)
    assert [name for name, _ in work.files] == ["e00001_p00001.png", "e00001_p00002.png", "e00002_p00001.png"]
    assert len(calls) == 6
    assert work.metadata_only is False
    assert work.rating == "unknown"


def test_incomplete_pagination_and_download_denial_fail(monkeypatch):
    connector = PicACGConnector({"token": "secret"})
    monkeypatch.setattr(connector, "_request", lambda *args, **kwargs: {"eps": {"docs": [], "pages": 1, "total": 2}})
    with pytest.raises(ConnectorError, match="数量不匹配"):
        connector._pages("comics/x/eps", "eps", lambda _: None)
    monkeypatch.setattr(connector, "_request", lambda *args, **kwargs: {"comic": {"title": "不允许下载", "allowDownload": False}})
    assert connector.fetch(COMIC, download=False).metadata_only
    with pytest.raises(ConnectorError, match="不允许下载"):
        connector.fetch(COMIC)


def test_favorites_are_comic_ids_and_tag_discovery_is_exact(monkeypatch):
    connector = PicACGConnector({"token": "secret"})
    monkeypatch.setattr(connector, "_request", lambda endpoint, **kwargs: {"comics": {"docs": [{"_id": COMIC, "tags": ["风景"]}, {"_id": SECOND, "tags": ["其他"]}], "total": 2, "pages": 1}})
    assert connector.favorites() == [COMIC, SECOND]
    assert connector.discover("tag", "风景") == ([COMIC], False)
    assert connector.discover("search", "风景") == ([COMIC, SECOND], False)
    with pytest.raises(ConnectorError, match="作者关注"):
        connector.discover("author", "某人")
