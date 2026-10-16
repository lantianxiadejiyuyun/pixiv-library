"""Synthetic Kemono traffic through the real API, queues, and storage pipeline."""
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

from app import endpoint_config, jobs, queue_control
from app.database import SessionLocal
from app.integrations import PLATFORMS, get_connector
from app.integrations import base, kemono
from app.models import Asset, Item, Job, SourceAccount, Subscription
from app.storage import COLLECTIONS, resolve_asset_path
from test_library import client, png
from test_video_assets import mp4


def post(ident="mixed"):
    value = {"id": ident, "user": "creator-1", "service": "fanbox", "title": "合成资料",
             "content": "<p>第一段</p><p>第二段</p>", "file": {"name": "cover.png", "path": "/ab/cd/cover.png"},
             "attachments": [{"name": "clip.mp4", "path": "/ab/cd/clip.mp4"}], "tags": ["合成标签"], "embed": {}}
    if ident == "image":
        value.update(content="", attachments=[])
    elif ident == "article":
        value.update(file={}, attachments=[])
    elif ident == "unsupported":
        value["attachments"] = [{"name": "archive.zip", "path": "/ab/cd/archive.zip"}]
    return value


@pytest.fixture
def platform_http(client, monkeypatch):
    requests = []
    jobs._stop.clear()
    monkeypatch.setattr(queue_control, "wait_for_request", lambda: None)
    monkeypatch.setattr(endpoint_config, "public_dns", lambda host: None)
    monkeypatch.setattr(base.socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 443))])

    def send(request):
        assert "authorization" not in request.headers and "cookie" not in request.headers
        requests.append(str(request.url))
        path = request.url.path
        if path.startswith("/api/v1/") or path.startswith("/mirror/v1/"):
            assert request.headers["accept"] == "text/css"
            if path.endswith("/profile"):
                return httpx.Response(200, json={"id": "creator-1", "service": "fanbox", "name": "合成作者"})
            if "/post/" in path:
                return httpx.Response(200, json={"post": post(path.rsplit("/", 1)[1])})
            if path.endswith("/posts"):
                values = [post("mixed"), post("image")]
                if "/user/" in path:
                    return httpx.Response(200, json={"props": {"count": 75, "limit": 50}, "results": values})
                return httpx.Response(200, json={"count": 75, "posts": values})
        if path == "/data/ab/cd/cover.png" or path == "/thumbnail/data/ab/cd/cover.png":
            return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})
        if path == "/data/ab/cd/clip.mp4":
            return httpx.Response(200, content=mp4(), headers={"Content-Type": "video/mp4"})
        raise AssertionError(f"Unexpected synthetic request: {request.method} {path}")

    class SyntheticHTTP(base.SafeHTTP):
        def __init__(self, hosts, headers=None, cookies=None):
            assert cookies is None
            self.hosts = tuple(hosts)
            self.proxy = None
            self.client = httpx.Client(transport=httpx.MockTransport(send), headers=headers,
                                       follow_redirects=False, trust_env=False)

    monkeypatch.setattr(kemono, "SafeHTTP", SyntheticHTTP)
    monkeypatch.setattr(base, "SafeHTTP", SyntheticHTTP)
    return requests


def enqueue(client, ident, *, queue="details"):
    response = client.post("/api/sources/kemono/import", json={"url": f"https://kemono.cr/fanbox/user/creator-1/post/{ident}"})
    assert response.status_code == 200, response.text
    assert response.json()["queue"] == queue
    return response.json()["id"]


def detail_and_body(client, job_id):
    assert jobs.run_once({"details"})
    stage = client.get(f"/api/jobs/{job_id}").json()
    assert stage["status"] == "completed", stage
    assert stage["result"]["metadata_only"] is True
    item_id = stage["result"]["item_id"]
    body_id = stage["result"]["content_job_id"]
    assert body_id != job_id
    body = client.get(f"/api/jobs/{body_id}").json()
    assert body["kind"] == "import_content" and body["queue"] == "download" and body["status"] == "queued"
    return item_id, body_id


def test_registered_public_platform_and_separate_storage_collection(client):
    assert "kemono" in PLATFORMS and "kemono" in COLLECTIONS
    assert isinstance(get_connector("kemono"), kemono.KemonoConnector)
    sources = client.get("/api/sources").json()
    source = next(source for source in sources if source["platform"] == "kemono")
    assert source["status"] == "ready" and source["accounts"] == []
    assert {entry["key"]: entry["status"] for entry in source["capabilities"]}["import"] == "available"
    assert client.post("/api/sources/kemono/sync", json={}).status_code == 501


@pytest.mark.parametrize("ident,mimes", [("image", ["image/png"]), ("article", ["text/plain"]),
    ("mixed", ["image/png", "video/mp4", "text/plain"])])
def test_real_two_stage_pipeline_saves_complete_readable_assets(client, platform_http, ident, mimes):
    job_id = enqueue(client, ident)
    item_id, body_id = detail_and_body(client, job_id)
    metadata = client.get(f"/api/items/{item_id}").json()
    assert metadata["source"] == "kemono" and metadata["source_url"].endswith(f"/post/{ident}")
    assert metadata["assets"] == [] and metadata["remote_page_count"] == len(mimes)
    assert metadata["content_state"] == "queued"
    assert not any("/data/" in url and "/thumbnail/" not in url for url in platform_http)
    assert client.get("/api/items?source=kemono&downloaded=true").json()["total"] == 0
    if ident != "article":
        assert metadata["cover_url"] and client.get(metadata["cover_url"]).status_code == 200
    assert jobs.run_once({"download"})
    status = client.get(f"/api/jobs/{body_id}").json()
    assert status["status"] == "completed", status
    saved = client.get(f"/api/items/{item_id}").json()
    assert saved["content_state"] == "ready" and saved["page_count"] == len(mimes)
    assert [asset["mime_type"] for asset in saved["assets"]] == mimes
    assert client.get("/api/items?source=kemono&downloaded=true").json()["total"] == 1
    assert client.get("/api/items?source=pixiv").json()["total"] == 0
    with SessionLocal() as db:
        assert db.query(SourceAccount).count() == 0
        assert db.get(Item, item_id).source_id == f"fanbox:creator-1:{ident}"
        for asset in db.scalars(select(Asset).where(Asset.item_id == item_id)):
            assert asset.relative_path.startswith("collections/kemono/")
            assert resolve_asset_path(asset.relative_path).read_bytes()
    for asset in saved["assets"]:
        result = client.get(asset["url"])
        assert result.status_code == 200 and result.content
        assert result.headers["content-type"].startswith(asset["mime_type"])
        if asset["mime_type"] == "text/plain":
            assert result.content.decode() == "第一段\n\n第二段"
        if asset["mime_type"] == "video/mp4":
            partial = client.get(asset["url"], headers={"Range": "bytes=8-15"})
            assert partial.status_code == 206 and partial.content == mp4()[8:16]


def test_download_pause_does_not_prevent_metadata_and_preserves_preview(client, platform_http):
    client.put("/api/queue/lanes/download", json={"paused": True}).raise_for_status()
    item_id, body_id = detail_and_body(client, enqueue(client, "mixed"))
    assert jobs.run_once({"download"}) is False
    waiting = client.get(f"/api/items/{item_id}").json()
    assert waiting["content_state"] == "queued" and waiting["cover_url"]
    assert client.get(f"/api/jobs/{body_id}").json()["status"] == "queued"
    client.put("/api/queue/lanes/download", json={"paused": False}).raise_for_status()
    assert jobs.run_once({"download"})
    assert client.get(f"/api/jobs/{body_id}").json()["status"] == "completed"


def test_unsupported_attachment_keeps_metadata_but_never_counts_as_downloaded(client, platform_http):
    item_id, body_id = detail_and_body(client, enqueue(client, "unsupported"))
    assert jobs.run_once({"download"})
    status = client.get(f"/api/jobs/{body_id}").json()
    assert status["status"] == "failed" and "未标记为已下载" in status["error"]
    item = client.get(f"/api/items/{item_id}").json()
    assert item["content_state"] == "failed" and item["page_count"] == 0 and item["cover_url"]
    assert client.get("/api/items?source=kemono&downloaded=true").json()["total"] == 0
    assert not any("/data/" in url and "/thumbnail/" not in url for url in platform_http)


@pytest.mark.parametrize("target_type,target,part", [("author", "fanbox:creator-1", "/fanbox/user/creator-1/posts"),
    ("tag", "合成标签", "tag="), ("search", "synthetic", "q=synthetic")])
def test_author_tag_search_subscription_checks_enqueue_details_then_body(client, platform_http, target_type, target, part):
    response = client.post("/api/subscriptions", json={"platform": "kemono", "target_type": target_type,
        "target": target, "auto_download": True})
    assert response.status_code == 200, response.text
    sub = response.json()
    assert sub["runtime_status"] == "active"
    assert jobs.run_once({"checks"})
    assert any(part in request for request in platform_http)
    with SessionLocal() as db:
        check = db.scalar(select(Job).where(Job.kind == "source_sync"))
        assert check.status == "completed", check.error
        assert check.result["has_more"] is True
        imports = db.scalars(select(Job).where(Job.kind == "import")).all()
        assert len(imports) == 2 and all(job.status == "queued" for job in imports)
        assert all(job.payload["platform"] == "kemono" and job.payload["download"] is True for job in imports)
        assert {job.payload["remote_id"] for job in imports} == {"fanbox:creator-1:mixed", "fanbox:creator-1:image"}
        assert db.get(Subscription, sub["id"]).last_checked_at is not None
    assert jobs.run_once({"details"}) and jobs.run_once({"details"})
    assert jobs.run_once({"download"}) and jobs.run_once({"download"})
    assert client.get("/api/items?source=kemono&downloaded=true").json()["total"] == 2


def test_active_duplicate_import_reuses_jobs_and_saved_item(client, platform_http):
    first = enqueue(client, "mixed")
    assert enqueue(client, "mixed") == first
    item_id, body_id = detail_and_body(client, first)
    assert enqueue(client, "mixed", queue="download") == body_id
    assert jobs.run_once({"download"})
    repeat = enqueue(client, "mixed")
    assert jobs.run_once({"details"})
    status = client.get(f"/api/jobs/{repeat}").json()
    assert status["status"] == "completed" and status["result"]["item_id"] == item_id
    assert client.get("/api/items?source=kemono").json()["total"] == 1


def test_custom_api_changes_metadata_origin_but_keeps_official_media_hosts(client, platform_http):
    response = client.put("/api/platform-endpoints/kemono", json={"custom_url": "https://mirror.example.com/mirror/v1", "enabled": True})
    assert response.status_code == 200, response.text
    _item_id, body_id = detail_and_body(client, enqueue(client, "mixed"))
    assert jobs.run_once({"download"})
    assert client.get(f"/api/jobs/{body_id}").json()["status"] == "completed"
    api_calls = [url for url in platform_http if "/v1/" in url]
    assert api_calls and all(url.startswith("https://mirror.example.com/mirror/v1/") for url in api_calls)
    media_calls = [url for url in platform_http if "/data/" in url]
    assert media_calls and {urlparse(url).hostname for url in media_calls} <= {"kemono.cr", "img.kemono.cr"}
