from concurrent.futures import ThreadPoolExecutor
import gzip
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from test_library import client
from app import character_search as search
from app.auth import COOKIE_NAME, issue_session
from app.database import SessionLocal
from app.library import set_tags
from app.models import Item, ItemTag, Tag


@pytest.fixture(autouse=True)
def reset_search_limit():
    with search._request_lock:
        search._requests.clear()
    yield
    with search._request_lock:
        search._requests.clear()


@pytest.fixture
def api(client):
    # The production router is mounted by main; this small app also proves that
    # its authentication requirement is intrinsic to the router.
    app = FastAPI()
    app.include_router(search.router, prefix="/api")
    with TestClient(app) as instance:
        instance.cookies.set(COOKIE_NAME, issue_session())
        yield instance


def character(id_=17, name="Naruto Uzumaki"):
    return {"id": id_, "name": {"full": name, "native": "うずまきナルト", "alternative": ["Naruto", "鸣人", "Naruto"]},
        "description": "<p>A ninja.</p><script>do-not-render</script>![remote](https://image.invalid/pic.jpg) [Details](https://other.invalid)",
        "siteUrl": "javascript:untrusted-link",
        "media": {"nodes": [work()]}}


def work(id_=30011, title="NARUTO"):
    return {"id": id_, "type": "MANGA", "title": {"romaji": title, "native": title},
            "siteUrl": "https://evil.invalid/private"}


def provider_response(data, status=200, headers=None):
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(json.dumps(data).encode()))


def mock_provider(monkeypatch, handler):
    original = httpx.Client
    options = []

    def client_factory(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler), follow_redirects=False, headers=kwargs["headers"])

    monkeypatch.setattr(search.httpx, "Client", client_factory)
    monkeypatch.setattr(search, "httpx_proxy", lambda: "http://proxy-user:proxy-secret@127.0.0.1:7890")
    return options


def test_search_only_sends_confirmed_text_via_configured_proxy_and_returns_candidates(api, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == search.ENDPOINT
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["variables"] == {"query": "Naruto Uzumaki", "limit": 6}
        assert set(body) == {"query", "variables"}
        assert "image" not in body["query"].lower()
        assert "authorization" not in request.headers
        return provider_response({"data": {"Page": {"characters": [character()]}}})

    options = mock_provider(monkeypatch, handler)
    response = api.post("/api/ai/characters/search", json={"query": " Naruto Uzumaki "})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["provider"] == "AniList" and "不是" in result["notice"]
    candidate = result["candidates"][0]
    assert candidate["name"] == "Naruto Uzumaki" and candidate["aliases"] == ["Naruto", "鸣人"]
    assert candidate["source_url"] == "https://anilist.co/character/17"
    assert candidate["works"][0]["source_url"] == "https://anilist.co/manga/30011"
    assert all(value not in response.text for value in ("javascript:", "do-not-render", "https://image.invalid", "https://other.invalid", "proxy-secret"))
    assert options[0]["proxy"].startswith("http://proxy-user:")
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert len(calls) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Tag.id))) == 0


def test_work_search_deduplicates_candidates_and_keeps_matching_work_sources(api, monkeypatch):
    rows = [{**work(), "characters": {"nodes": [character(), character(2, "Second")]}},
            {**work(30012, "Another manga"), "characters": {"nodes": [character()]}}]

    def handler(request):
        body = json.loads(request.content)
        assert "type: MANGA" in body["query"]
        assert body["variables"] == {"query": "NARUTO", "limit": 2}
        return provider_response({"data": {"Page": {"media": rows}}})

    mock_provider(monkeypatch, handler)
    result = api.post("/api/ai/characters/search", json={"query": "NARUTO", "mode": "work", "limit": 2}).json()
    assert len(result["candidates"]) == 2
    assert [work["title"] for work in result["candidates"][0]["works"]] == ["NARUTO", "Another manga"]


def test_empty_results_are_real_empty_and_do_not_write_tags(api, monkeypatch):
    mock_provider(monkeypatch, lambda request: provider_response({"data": {"Page": {"characters": []}}}))
    response = api.post("/api/ai/characters/search", json={"query": "NothingMatch123"})
    assert response.status_code == 200 and response.json()["candidates"] == []


@pytest.mark.parametrize("body", [{"query": ""}, {"query": "  "}, {"query": "x" * 121}, {"query": "a\nb"},
    {"query": "role", "mode": "image"}, {"query": "role", "limit": 11}, {"query": "role", "limit": 0},
    {"query": "role", "image": "base64-data-must-never-go-upstream"}], ids=["empty", "spaces", "long", "control", "bad-mode", "too-many", "zero", "no-images"])
def test_search_input_limits_stop_before_network(api, monkeypatch, body):
    monkeypatch.setattr(search, "_request_provider", lambda value: pytest.fail("Invalid input reached provider"))
    assert api.post("/api/ai/characters/search", json=body).status_code == 422


@pytest.mark.parametrize("status", [301, 302, 401, 403, 500])
def test_upstream_rejection_is_safe_and_never_401_or_redirect(api, monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return provider_response({"message": "secret-upstream-body"}, status, {"location": "https://evil.invalid/collect"})

    mock_provider(monkeypatch, handler)
    response = api.post("/api/ai/characters/search", json={"query": "role"})
    assert response.status_code == 502 and "secret" not in response.text
    assert len(calls) == 1


def test_provider_rate_limit_returns_retry_after_without_body(api, monkeypatch):
    mock_provider(monkeypatch, lambda request: provider_response({"message": "secret"}, 429, {"retry-after": "25"}))
    response = api.post("/api/ai/characters/search", json={"query": "role"})
    assert response.status_code == 429 and response.headers["retry-after"] == "25"
    assert "secret" not in response.text


def test_local_rate_limit_is_bounded_and_stops_requests(api, monkeypatch):
    calls = []
    mock_provider(monkeypatch, lambda request: (calls.append(request) or provider_response({"data": {"Page": {"characters": []}}})))
    for _ in range(20):
        assert api.post("/api/ai/characters/search", json={"query": "role"}).status_code == 200
    response = api.post("/api/ai/characters/search", json={"query": "role"})
    assert response.status_code == 429 and len(calls) == 20


@pytest.mark.parametrize("timeout", [False, True])
def test_connection_and_timeout_errors_do_not_expose_proxy_secrets(api, monkeypatch, timeout):
    def handler(request):
        error = httpx.ReadTimeout if timeout else httpx.ConnectError
        raise error("proxy-secret token=secret-upstream")

    mock_provider(monkeypatch, handler)
    response = api.post("/api/ai/characters/search", json={"query": "role"})
    assert response.status_code == (504 if timeout else 502)
    assert "secret" not in response.text


@pytest.mark.parametrize("data", [{"errors": [{"message": "secret"}]}, {"data": None}, {"data": {"Page": {}}},
    {"data": {"Page": {"characters": "not-a-list"}}}, {"data": {"Page": {"characters": [{"id": "../../invalid", "name": {"full": "bad row"}}]}}}])
def test_malformed_or_graphql_failure_is_not_reported_as_empty(api, monkeypatch, data):
    mock_provider(monkeypatch, lambda request: provider_response(data))
    response = api.post("/api/ai/characters/search", json={"query": "role"})
    assert response.status_code == 502 and "secret" not in response.text


def test_response_size_limit_applies_after_compression(api, monkeypatch):
    compressed = gzip.compress(b"a" * (search.MAX_RESPONSE_BYTES + 1))
    mock_provider(monkeypatch, lambda request: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(compressed)))
    assert api.post("/api/ai/characters/search", json={"query": "role"}).status_code == 502


def make_item():
    with SessionLocal() as db:
        item = Item(title="本地作品", type="manga", source="local")
        db.add(item)
        db.flush()
        set_tags(db, item, ["人工标签"], "manual")
        set_tags(db, item, ["来源标签"], "source")
        set_tags(db, item, ["角色:旧AI角色"], "ai")
        db.commit()
        return item.id


def test_confirm_appends_manual_tag_idempotently_without_overwriting_any_origin(api):
    item_id = make_item()
    first = api.post("/api/ai/characters/confirm", json={"item_id": item_id, "name": " Naruto Uzumaki "})
    second = api.post("/api/ai/characters/confirm", json={"item_id": item_id, "name": "角色:Naruto Uzumaki"})
    assert first.status_code == 200 and second.json() == first.json()
    assert first.json()["tag"]["name"] == "角色:Naruto Uzumaki"
    assert first.json()["tag"]["origin"] == "manual" and first.json()["tag"]["score"] is None
    with SessionLocal() as db:
        tags = {(link.tag.name, link.origin) for link in db.get(Item, item_id).item_tags}
        assert tags == {("人工标签", "manual"), ("来源标签", "source"), ("角色:旧AI角色", "ai"), ("角色:Naruto Uzumaki", "manual")}


def test_concurrent_confirmations_preserve_additions_and_unique_tag_identity(api):
    item_id, second_item = make_item(), make_item()

    def confirm_entry(value):
        target, name = value
        with SessionLocal() as db:
            request = search.ConfirmInput(item_id=target, name=name)
            return search.confirm_character(db, request.item_id, request.name)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(confirm_entry, [(item_id, "Naruto"), (item_id, "Sasuke"), (item_id, "Naruto"), (second_item, "Naruto")]))
    assert results[0]["tag"]["id"] == results[2]["tag"]["id"] == results[3]["tag"]["id"]
    with SessionLocal() as db:
        manual = {link.tag.name for link in db.get(Item, item_id).item_tags if link.origin == "manual"}
        assert manual == {"人工标签", "角色:Naruto", "角色:Sasuke"}
        assert db.scalar(select(func.count(Tag.id)).where(Tag.name == "角色:Naruto")) == 1


@pytest.mark.parametrize("name", ["", "  ", "角色:", "x" * 189, "role\nname"], ids=["empty", "spaces", "prefix-only", "long-tag", "control"])
def test_confirm_rejects_invalid_or_overlong_names_without_any_tag_creation(api, name):
    response = api.post("/api/ai/characters/confirm", json={"item_id": "missing", "name": name})
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(func.count(Tag.id))) == 0


def test_confirm_missing_item_is_404_and_both_routes_require_login(api, monkeypatch):
    assert api.post("/api/ai/characters/confirm", json={"item_id": "missing", "name": "Naruto"}).status_code == 404
    api.cookies.clear()
    monkeypatch.setattr(search, "_request_provider", lambda value: pytest.fail("Unauthenticated request reached provider"))
    assert api.post("/api/ai/characters/search", json={"query": "Naruto"}).status_code == 401
    assert api.post("/api/ai/characters/confirm", json={"item_id": "missing", "name": "Naruto"}).status_code == 401
