from io import BytesIO
import json

import httpx
from PIL import Image, PngImagePlugin
import pytest

from test_library import client, png, upload
from app import character_image_search as service
from app.database import SessionLocal
from app.integrations.credentials import decrypt_credentials
from app.models import NetworkSetting


@pytest.fixture(autouse=True)
def reset_requests():
    service._attempts.clear()
    yield
    service._attempts.clear()


def mock_remote(monkeypatch, *, status=200, payload=None, content=None, headers=None):
    captured = []
    original_client = httpx.Client
    def handler(request):
        captured.append(request)
        body = content if content is not None else json.dumps(payload or {"header": {"status": 0}, "results": []}).encode()
        return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers)
    def factory(**kwargs):
        assert kwargs.pop("proxy") == "http://127.0.0.1:17890"
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(service.network, "httpx_proxy", lambda: "http://127.0.0.1:17890")
    monkeypatch.setattr(service.httpx, "Client", factory)
    return captured


def search(client, **data):
    return client.post("/api/ai/characters/image-search", data={"consent": "true", **data},
                       files={"file": ("private-book-page.png", png(), "image/png")})


def test_image_settings_encrypt_retain_clear_and_no_secret_echo(client):
    path = "/api/ai/characters/image/settings"
    assert client.get(path).json()["has_api_key"] is False
    response = client.put(path, json={"api_key": "private-api-key-123"})
    assert response.status_code == 200 and response.json()["has_api_key"]
    assert "private-api-key" not in response.text
    with SessionLocal() as db:
        row = db.get(NetworkSetting, service.KEY)
        assert "private-api-key" not in row.value_encrypted
        assert decrypt_credentials(row.value_encrypted)["api_key"] == "private-api-key-123"
    assert client.put(path, json={"api_key": None}).json()["has_api_key"]
    assert client.put(path, json={}).json()["has_api_key"]
    assert client.put(path, json={"api_key": ""}).json()["has_api_key"] is False
    response = client.put(path, json={"api_key": "secret\ninvalid"})
    assert response.status_code == 400 and "secret" not in response.text
    response = client.put(path, json={"api_key": "sensitive" * 50})
    assert response.status_code == 422 and "sensitive" not in response.text


def test_explicit_consent_and_exactly_one_image_required(client, monkeypatch):
    monkeypatch.setattr(service, "search_image", lambda *args: pytest.fail("must not upload"))
    assert search(client, consent="false").status_code == 400
    assert search(client, asset_id="both").status_code == 400
    assert client.post("/api/ai/characters/image-search", data={"consent": "true"}).status_code == 400
    assert client.post("/api/ai/characters/image-search", data={"consent": "true", "asset_id": "missing"}).status_code == 404


def test_image_routes_require_authentication_and_same_origin(client):
    assert client.put("/api/ai/characters/image/settings", json={"api_key": "abc"}, headers={"Origin": "https://evil.example"}).status_code == 403
    client.post("/api/auth/logout")
    assert client.get("/api/ai/characters/image/settings").status_code == 401
    assert search(client).status_code == 401


def test_upload_uses_fixed_provider_proxy_post_secret_and_generic_filename(client, monkeypatch):
    client.put("/api/ai/characters/image/settings", json={"api_key": "private-api-key-123"})
    captured = mock_remote(monkeypatch)
    result = search(client)
    assert result.status_code == 200 and result.json()["matches"] == []
    request = captured[0]
    assert request.method == "POST" and str(request.url) == service.ENDPOINT
    body = request.read()
    assert b"private-api-key-123" in body and b'filename="image.jpg"' in body
    assert b"private-book-page" not in body
    assert "private-api-key-123" not in result.text
    assert result.json()["uploaded_size"] > 0


def test_prepare_image_strips_metadata_and_reduces_size():
    buffer = BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("private-note", "secret filename and GPS location")
    Image.new("RGBA", (2400, 1600), (0, 0, 0, 0)).save(buffer, "PNG", pnginfo=info)
    result = service.prepare_image(buffer.getvalue())
    assert b"private-note" not in result and b"GPS" not in result
    with Image.open(BytesIO(result)) as image:
        assert image.format == "JPEG" and image.size == (1024, 683)
        assert image.getpixel((0, 0)) == (255, 255, 255)
        assert not image.getexif()


def test_library_page_upload_does_not_modify_original_or_item(client, monkeypatch):
    item = upload(client, title="原作品").json()
    asset = item["assets"][0]
    captured = mock_remote(monkeypatch)
    response = client.post("/api/ai/characters/image-search", data={"asset_id": asset["id"], "consent": "true"})
    assert response.status_code == 200 and len(captured) == 1
    assert client.get(asset["url"]).content == png()
    after = client.get(f"/api/items/{item['id']}").json()
    assert after["tags"] == item["tags"] and after["rating"] == item["rating"]


@pytest.mark.parametrize("content,status", [(b"not an image", 400), (b"x" * (service.MAX_FILE + 1), 413), (b"", 413)], ids=["invalid", "oversize", "empty"])
def test_invalid_image_never_reaches_network(client, monkeypatch, content, status):
    monkeypatch.setattr(service, "search_image", lambda *args: pytest.fail("must not upload"))
    response = client.post("/api/ai/characters/image-search", data={"consent": "true"}, files={"file": ("x.png", content, "image/png")})
    assert response.status_code == status


@pytest.mark.parametrize("upstream,expected", [(403, 502), (429, 429), (500, 502), (302, 502)])
def test_upstream_errors_and_redirects_do_not_escape_or_follow(client, monkeypatch, upstream, expected):
    calls = mock_remote(monkeypatch, status=upstream, content=b"private-key-from-error", headers={"Location": "https://unapproved.example/"})
    response = search(client)
    assert response.status_code == expected and "private-key" not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize("content,headers", [(b"x" * (service.MAX_RESPONSE + 1), {}), (b"not-json", {}), (b"compressed", {"Content-Encoding": "gzip"}), (b"[" * 1500 + b"0" + b"]" * 1500, {})], ids=["oversize", "not-json", "compression", "deep-json"])
def test_bounded_response_and_safe_parse_failure(client, monkeypatch, content, headers):
    mock_remote(monkeypatch, content=content, headers=headers)
    assert search(client).status_code == 502


def test_parse_matches_preserves_uncertainty_and_removes_unsafe_links():
    results = service.parse_matches({"header": {"status": 0}, "results": [{
        "header": {"similarity": "42.4", "index_name": "Manga index", "thumbnail": "https://remote/image.jpg"},
        "data": {"source": "Example manga", "creator": ["Author"], "characters": "Example, Example",
                 "ext_urls": ["javascript:alert(1)", "http://127.0.0.1/", "http://127.1/", "http://0x7f.1/", "http://localhost.localdomain/", "https://user:password@example.net/", "https://anilist.co/manga/30011/"]}}]})
    assert len(results) == 1
    result = results[0]
    assert result["similarity"] == 42.4 and result["characters"] == ["Example"]
    assert result["source_urls"] == ["https://anilist.co/manga/30011/"]
    assert "thumbnail" not in result
    assert "confidence" not in result


def test_vendor_error_is_not_reflected(client, monkeypatch):
    mock_remote(monkeypatch, payload={"header": {"status": -1, "message": "your private-api-key expired"}})
    response = search(client)
    assert response.status_code == 502 and "private-api-key" not in response.text


def test_missing_result_list_is_not_treated_as_no_match(client, monkeypatch):
    mock_remote(monkeypatch, payload={"header": {"status": 0}})
    assert search(client).status_code == 502


def test_invalid_result_rows_are_not_treated_as_no_match(client, monkeypatch):
    mock_remote(monkeypatch, payload={"header": {"status": 0}, "results": [{"header": {"similarity": "NaN"}, "data": {}}]})
    assert search(client).status_code == 502


def test_rate_limit_applies_before_remote_request(client, monkeypatch):
    captured = mock_remote(monkeypatch)
    for _ in range(6):
        assert search(client).status_code == 200
    assert search(client).status_code == 429
    assert len(captured) == 6
