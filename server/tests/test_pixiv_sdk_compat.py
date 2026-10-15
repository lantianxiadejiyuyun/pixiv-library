"""Use the installed Pixiv SDK JSON parser and HTTP boundary, never live accounts."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from pixivpy3 import AppPixivAPI
from pixivpy3.utils import JsonDict
from sqlalchemy import select

from test_library import client
from app import jobs, network, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError
from app.integrations.pixiv import PixivConnector
from app.models import Job, Subscription


@pytest.fixture
def pixiv_transport(monkeypatch):
    monkeypatch.setattr(network, "requests_proxies", lambda: {"http": "", "https": "", "all": ""})
    # Timing/cancellation are independently exercised by connector_stages and
    # queue tests; this fixture isolates SDK parsing and persisted job results.
    monkeypatch.setattr(queue_control, "wait_for_request", lambda: None)
    calls = []
    def send(adapter, request, **kwargs):
        parsed = urlsplit(request.url)
        query = parse_qs(parsed.query)
        calls.append((parsed.path, query))
        if parsed.hostname == "oauth.secure.pixiv.net" and parsed.path == "/auth/token":
            body = {"response": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "user": {"id": 101}}}
        elif parsed.hostname == "app-api.pixiv.net" and parsed.path == "/v1/user/following":
            assert request.headers["Authorization"] == "Bearer synthetic-access"
            assert query["user_id"] == ["101"]
            if query["restrict"] == ["public"] and not query.get("offset"):
                body = {"user_previews": [{"user": {"id": 201, "name": "公开作者一"}}],
                    "next_url": "https://app-api.pixiv.net/v1/user/following?user_id=101&restrict=public&offset=30"}
            elif query["restrict"] == ["public"] and query.get("offset") == ["30"]:
                body = {"user_previews": [{"user": {"id": 202, "name": "公开作者二"}}], "next_url": None}
            elif query["restrict"] == ["private"]:
                body = {"user_previews": [{"user": {"id": 203, "name": "私密作者"}}], "next_url": None}
            else:
                raise AssertionError("Unexpected following request in isolated fixture")
        elif parsed.hostname == "app-api.pixiv.net" and parsed.path == "/v1/illust/detail":
            body = {"illust": {"id": 123, "title": "模拟画作", "type": "illust", "user": {"name": "示例作者"},
                "caption": "<p>模拟简介</p>", "tags": [{"name": "风景"}], "x_restrict": 0}}
        elif parsed.hostname == "app-api.pixiv.net" and parsed.path in {"/v1/user/illusts", "/v1/search/illust"}:
            body = {"illusts": [{"id": 123}, {"id": 456}], "next_url": "https://app-api.pixiv.net/v1/user/illusts?offset=30"}
        else:
            raise AssertionError("Unexpected request: fixture prohibits live Pixiv traffic")
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(body, ensure_ascii=False).encode()
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls


def test_real_jsondict_missing_method_does_not_trigger_model_conversion():
    response = AppPixivAPI.parse_json('{"user_previews":[],"next_url":null}')
    assert isinstance(response, JsonDict)
    assert hasattr(response, "model_dump") and response.model_dump is None
    assert PixivConnector._check(response) is response
    response["model_dump"] = "untrusted JSON key, not a callable"
    assert PixivConnector._check(response) is response


def test_typed_response_is_converted_only_when_callable():
    response = SimpleNamespace(model_dump=lambda: {"illusts": []})
    assert PixivConnector._check(response) == {"illusts": []}


@pytest.mark.parametrize("response", [None, [], [1], "unexpected-response", {},
    SimpleNamespace(model_dump=None), SimpleNamespace(model_dump=lambda: []),
    JsonDict(error={"message": "private-token-do-not-echo"})])
def test_invalid_sdk_responses_are_actionable_and_redacted(response):
    with pytest.raises(ConnectorError, match="Pixiv 请求失败") as error:
        PixivConnector._check(response)
    assert "private-token" not in str(error.value)


def test_real_sdk_following_public_private_and_next_page(pixiv_transport):
    follows = PixivConnector({"refresh_token": "synthetic-refresh"}).follows()
    assert [(entry["target"], entry["label"]) for entry in follows] == [
        ("201", "公开作者一"), ("202", "公开作者二"), ("203", "私密作者")]
    following_queries = [query for path, query in pixiv_transport if path == "/v1/user/following"]
    assert len(following_queries) == 3
    assert following_queries[1]["offset"] == ["30"]


def test_real_sdk_metadata_and_discovery_share_response_normalization(pixiv_transport):
    connector = PixivConnector({"refresh_token": "synthetic-refresh"})
    work = connector.fetch("123", download=False)
    assert work.title == "模拟画作" and work.author == "示例作者"
    assert work.tags == ["风景"] and work.description == "模拟简介"
    assert work.metadata_only and work.files == []
    assert connector.discover("author", "201") == (["123", "456"], True)
    assert connector.discover("tag", "风景") == (["123", "456"], True)


def test_follow_sync_job_persists_real_sdk_results_and_retry_is_idempotent(client, pixiv_transport):
    jobs._stop.clear()
    account = client.post("/api/sources/pixiv/accounts", json={"label": "模拟 SDK 账号", "credentials": {
        "refresh_token": "synthetic-refresh", "user_id": "101"}})
    assert account.status_code == 200
    payload = {"account_id": account.json()["id"]}
    first = client.post("/api/sources/pixiv/sync", json=payload)
    assert first.status_code == 200
    assert jobs.run_once()
    with SessionLocal() as db:
        job = db.get(Job, first.json()["id"])
        assert job.status == "completed", job.error
        assert job.result["added_subscriptions"] == 3
        assert job.result["remote_follows"] == 3
        assert {sub.target for sub in db.scalars(select(Subscription))} == {"201", "202", "203"}
    second = client.post("/api/sources/pixiv/sync", json=payload)
    assert jobs.run_once()
    with SessionLocal() as db:
        job = db.get(Job, second.json()["id"])
        assert job.status == "completed", job.error
        assert job.result["added_subscriptions"] == 0
        assert len(list(db.scalars(select(Subscription)))) == 3
