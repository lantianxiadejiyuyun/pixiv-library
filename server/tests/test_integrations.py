from datetime import timedelta
import pytest
from sqlalchemy import select

# Share the backend's isolated SQLite/filesystem fixture, never use the user's library.
from test_library import client, png
from app.config import settings
from app.database import SessionLocal
from app.models import Item, ItemTag, Job, SourceAccount, Subscription, utcnow
from app import jobs
from app.ai import service
from app.integrations.base import ConnectorError, RemoteWork, SafeHTTP, validate_url
from app.integrations.credentials import decrypt_credentials, encrypt_credentials
from app.integrations.pixiv import PixivConnector
from app.library import import_files


@pytest.fixture(autouse=True)
def worker_ready():
    jobs._stop.clear()


def add_account(client):
    response = client.post("/api/sources/pixiv/accounts", json={"label": "私人账号", "credentials": {"refresh_token": "sensitive-test-token"}})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_encrypted_accounts_never_return_credentials(client, monkeypatch):
    ident = add_account(client)
    with SessionLocal() as db:
        account = db.get(SourceAccount, ident)
        assert "sensitive-test-token" not in account.credentials_encrypted
        assert decrypt_credentials(account.credentials_encrypted)["refresh_token"] == "sensitive-test-token"
    assert "sensitive-test-token" not in client.get("/api/sources").text
    encrypted = encrypt_credentials({"refresh_token": "secret"})
    monkeypatch.setattr(settings, "secret_key", "a-completely-different-secret-key")
    with pytest.raises(ConnectorError, match="无法解密"):
        decrypt_credentials(encrypted)


@pytest.mark.parametrize("url", ["http://www.pixiv.net/artworks/1", "https://pixiv.net.evil.test/1",
    "https://localhost/1", "https://www.pixiv.net:8443/1", "https://evil@www.pixiv.net/1", "file:///etc/passwd"])
def test_platform_url_validation_rejects_arbitrary_fetch(url):
    with pytest.raises(ConnectorError):
        validate_url(url, ("pixiv.net",))


def test_pixiv_identity_normalization():
    connector = PixivConnector({})
    assert connector.normalize_id("https://www.pixiv.net/en/artworks/123") == "123"
    assert connector.normalize_id("https://www.pixiv.net/novel/show.php?id=456") == "novel:456"
    assert connector.normalize_id("novel:456") == "novel:456"


def test_redirect_to_local_network_is_blocked(client, monkeypatch):
    import httpx
    import app.integrations.base as base
    original_validate = base.validate_url
    # Keep URL checks but avoid real DNS in this transport-only regression test.
    monkeypatch.setattr(base, "validate_url", lambda url, hosts, resolve=False: original_validate(url, hosts))
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})
    client = SafeHTTP(("pximg.net",))
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ConnectorError):
            client.fetch("https://i.pximg.net/test.png")
        assert len(requested) == 1
    finally:
        client.close()


class FakeConnector:
    supports_subscriptions = True

    def normalize_id(self, value):
        return value

    def fetch(self, value, *, download=True, checkpoint=lambda value: None):
        checkpoint(50)
        return RemoteWork(remote_id=value, title="实际持久任务", source_url="https://www.pixiv.net/artworks/123",
                          item_type="article", tags=["来源标签"], files=[("正文.txt", "编程算法".encode())] if download else [], metadata_only=not download)

    def discover(self, target_type, target, content_scope="images"):
        return ["123", "123", "456"], True

    def follows(self, checkpoint=lambda value: None):
        checkpoint(50)
        return [{"target_type": "author", "target": "101", "label": "作者一"}]


def test_queue_claim_import_idempotence_and_failure_redaction(client, monkeypatch):
    add_account(client)
    monkeypatch.setattr(jobs, "get_connector", lambda *args: FakeConnector())
    queued = client.post("/api/sources/pixiv/import", json={"remote_id": "123"}).json()
    assert queued["status"] == "queued"
    assert jobs.run_once()
    cover = client.get(f"/api/jobs/{queued['id']}").json()
    assert cover["status"] == "completed", cover
    assert cover["result"]["metadata_only"] is False
    assert cover['label'] == 'Pixiv 详情与正文下载'
    assert cover['result']['content_job_id'] == queued['id']
    with SessionLocal() as db:
        assert db.scalar(select(Job).where(Job.kind == 'import_content')) is None
    client.post("/api/sources/pixiv/import", json={"remote_id": "123"})
    jobs.run_once()
    assert client.get("/api/items").json()["total"] == 1
    with SessionLocal() as db:
        failed = jobs.enqueue(db, "invalid", {})
        ident = failed.id
    jobs.run_once()
    with SessionLocal() as db:
        assert db.get(Job, ident).status == "failed"
    assert client.post(f"/api/jobs/{ident}/retry").json()["status"] == "queued"
    assert client.post(f"/api/jobs/{ident}/cancel").json()["status"] == "cancelled"


def test_expired_lease_recovers_but_active_lease_is_not_stolen(client, monkeypatch):
    monkeypatch.setattr(jobs, "get_connector", lambda *args: FakeConnector())
    with SessionLocal() as db:
        stale = Job(kind="import", status="running", payload={"platform": "jm", "remote_id": "123"},
                    worker_id="old-process", lease_until=utcnow() - timedelta(minutes=1))
        active = Job(kind="import", status="running", payload={"platform": "jm", "remote_id": "456"},
                     worker_id="other-process", lease_until=utcnow() + timedelta(minutes=10))
        db.add_all([stale, active]); db.commit()
        stale_id, active_id = stale.id, active.id
    jobs.run_once()
    with SessionLocal() as db:
        assert db.get(Job, stale_id).status == "queued"
        assert db.get(Job, active_id).worker_id == "other-process"
        db.get(Job, active_id).status = "completed"
        db.commit()
    assert jobs.run_once()
    with SessionLocal() as db:
        assert db.get(Job, stale_id).status == "completed"


def test_subscription_scheduler_creates_real_import_jobs(client, monkeypatch):
    monkeypatch.setattr(jobs, "get_connector", lambda *args: FakeConnector())
    response = client.post("/api/subscriptions", json={"platform": "pixiv", "target_type": "tag", "target": "风景", "auto_download": False})
    assert response.status_code == 200
    jobs.run_once()
    with SessionLocal() as db:
        queued = db.scalars(select(Job).where(Job.kind == "import")).all()
        assert len(queued) == 2
        assert all(j.payload["download"] is False for j in queued)
    jobs.run_once()
    assert client.get("/api/items").json()["total"] == 1
    assert client.get("/api/items").json()["items"][0]["page_count"] == 0


def test_metadata_only_record_upgrades_to_offline_files(client, monkeypatch):
    monkeypatch.setattr(jobs, "get_connector", lambda *args: FakeConnector())
    with SessionLocal() as db:
        jobs.enqueue(db, "import", {"platform": "jm", "remote_id": "123", "download": False})
    jobs.run_once()
    before = client.get("/api/items").json()["items"][0]
    assert before["page_count"] == 0
    client.patch(f"/api/items/{before['id']}", json={"title": "用户自定义标题", "tags": ["人工分类"]})
    with SessionLocal() as db:
        jobs.enqueue(db, "import", {"platform": "jm", "remote_id": "123", "download": True})
    jobs.run_once()
    waiting = client.get(f"/api/items/{before['id']}").json()
    assert waiting["page_count"] == 0
    assert waiting["content_state"] == "queued"
    assert jobs.run_once()
    after = client.get(f"/api/items/{before['id']}").json()
    assert after["page_count"] == 1
    assert after["title"] == "用户自定义标题"
    assert any(t["name"] == "人工分类" and t["origin"] == "manual" for t in after["tags"])
    assert client.get(after["assets"][0]["url"]).content == "编程算法".encode()
    assert client.get("/api/items").json()["total"] == 1


def test_cancel_during_fetch_does_not_persist_and_remote_errors_are_redacted(client, monkeypatch):
    class CancellingConnector(FakeConnector):
        def fetch(self, value, *, download=True, checkpoint=lambda value: None):
            with SessionLocal() as db:
                job = db.scalar(select(Job).where(Job.status == "running"))
                job.status = "cancelled"
                db.commit()
            return super().fetch(value, download=download, checkpoint=checkpoint)
    monkeypatch.setattr(jobs, "get_connector", lambda *args: CancellingConnector())
    with SessionLocal() as db:
        jobs.enqueue(db, "import", {"platform": "jm", "remote_id": "123"})
    jobs.run_once()
    assert client.get("/api/items").json()["total"] == 0
    assert client.get("/api/jobs").json()["items"][0]["status"] == "cancelled"

    class FailingConnector(FakeConnector):
        def fetch(self, *args, **kwargs):
            raise RuntimeError("remote echoed secret refresh_token=do-not-show")
    monkeypatch.setattr(jobs, "get_connector", lambda *args: FailingConnector())
    with SessionLocal() as db:
        jobs.enqueue(db, "import", {"platform": "jm", "remote_id": "123"})
    jobs.run_once()
    response = client.get("/api/jobs")
    assert "do-not-show" not in response.text
    assert response.json()["items"][0]["status"] == "failed"


def test_pending_connector_is_not_reported_as_success(client):
    assert client.post("/api/sources/jm/sync", json={}).status_code == 501
    sub = client.post("/api/subscriptions", json={"platform": "jm", "target_type": "tag", "target": "测试"}).json()
    assert sub["runtime_status"] == "pending"
    assert sub["last_checked_at"] is None


def test_ai_preserves_manual_rating_and_tags_and_records_per_page_evidence(client, monkeypatch):
    with SessionLocal() as db:
        item = import_files(db, [("1.png", png()), ("2.png", png("blue"))], tags=["人工保留"])
        item.rating, item.rating_origin = "general", "manual"
        db.commit(); item_id = item.id
    monkeypatch.setattr(service, "model_status", lambda: {"available": True})
    monkeypatch.setattr(service, "infer_image", lambda path: {"model_version": "test-model-v1", "rating": "adult",
        "rating_scores": {"explicit": .9, "general": .1}, "tags": [{"name": "角色:测试角色", "score": .97}]})
    client.post("/api/ai/analyze", json={"item_ids": [item_id]})
    jobs.run_once()
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        assert item.rating == "general"
        assert any(tag.origin == "manual" for tag in item.item_tags)
        predictions = [tag for tag in item.item_tags if tag.origin == "ai"]
        assert predictions[0].score == .97
        assert predictions[0].model_version == "test-model-v1"
        job = db.scalar(select(Job).where(Job.kind == "ai"))
        assert job.status == "completed", job.error
        assert len(job.result["page_evidence"]) == 2
        assert job.result["rating_applied"] is False


def test_ai_missing_model_fails_actionably_and_text_rules_work(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "model_dir", tmp_path / "no-model")
    with SessionLocal() as db:
        image = import_files(db, [("1.png", png())])
        text = import_files(db, [("文章.txt", "Python 数据库与算法学习".encode())], item_type="article")
        image_id, text_id = image.id, text.id
    client.post("/api/ai/analyze", json={"item_ids": [image_id, text_id]})
    jobs.run_once(); jobs.run_once()
    with SessionLocal() as db:
        entries = db.scalars(select(Job).order_by(Job.created_at)).all()
        assert entries[0].status == "failed"
        assert "model" in entries[0].error or "依赖" in entries[0].error
        assert entries[1].status == "completed", entries[1].error
        assert entries[1].result["text_evidence"][0]["tag"] == "分类:科技"


def test_ehentai_fetches_all_pages_and_keeps_credentials_off_media(monkeypatch):
    import json
    from app.integrations import ehentai
    clients = []
    class Transport:
        def __init__(self, hosts, headers=None, cookies=None):
            self.hosts, self.cookies = hosts, cookies
            self.requests = []
            clients.append(self)
        def close(self):
            pass
        def fetch(self, url, **kwargs):
            self.requests.append(url)
            if url.endswith("api.php"):
                return json.dumps({"gmetadata": [{"title": "测试画廊", "filecount": "2", "tags": ["artist:示例"]}]}).encode()
            if url.endswith("?p=0"):
                return b'<a href="/s/abc/123-1">one</a><a href="?p=1">next</a>'
            if url.endswith("?p=1"):
                return b'<a href="/s/def/123-2">two</a><a href="?p=0">previous</a>'
            if "/s/" in url:
                return b'<img id="img" src="https://node.hath.network/image.png">'
            if "hath.network" in url:
                return png()
            raise AssertionError(url)
    monkeypatch.setattr(ehentai, "SafeHTTP", Transport)
    connector = ehentai.EHentaiConnector({"ipb_member_id": "123", "ipb_pass_hash": "secret"})
    result = connector.fetch("123:abcdef0123")
    assert len(result.files) == 2
    assert result.files[0][0] == "00001.png"
    assert result.author == "示例"
    assert result.metadata_only is False
    media = next(c for c in clients if "hath.network" in c.hosts)
    assert media.cookies is None
    assert all("fullimg" not in url and "archiver" not in url for c in clients for url in c.requests)


def test_ehentai_incomplete_gallery_fails_without_partial_success(monkeypatch):
    import json
    from app.integrations import ehentai
    class Transport:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def fetch(self, url, **kwargs):
            if url.endswith("api.php"):
                return json.dumps({"gmetadata": [{"title": "缺页", "filecount": "2"}]}).encode()
            return b'<a href="/s/abc/123-1">only one page</a>'
    monkeypatch.setattr(ehentai, "SafeHTTP", Transport)
    with pytest.raises(ConnectorError, match="缺页"):
        ehentai.EHentaiConnector({}).fetch("123:abcdef0123")


def test_ai_uncertain_page_prevents_general_book_rating(client, monkeypatch):
    with SessionLocal() as db:
        item = import_files(db, [("1.png", png()), ("2.png", png("blue"))])
        item_id = item.id
    monkeypatch.setattr(service, "model_status", lambda: {"available": True})
    predictions = iter(["general", "unknown"])
    monkeypatch.setattr(service, "infer_image", lambda path: {"model_version": "test-model-v1", "rating": next(predictions),
        "rating_scores": {"general": .4, "sensitive": .3, "questionable": .2, "explicit": .1}, "tags": []})
    client.post("/api/ai/analyze", json={"item_ids": [item_id]})
    jobs.run_once()
    with SessionLocal() as db:
        assert db.get(Item, item_id).rating == "unknown"
        result = db.scalar(select(Job).where(Job.kind == "ai")).result
        assert result["suggested_rating"] == "unknown"


def test_commit_guard_rejects_lost_lease_after_analysis_before_db_commit(client, monkeypatch):
    with SessionLocal() as db:
        item = import_files(db, [("文章.txt", "Python 数据库".encode())], item_type="article")
        item_id = item.id
    def reject(db, job_id):
        raise jobs.JobCancelled()
    monkeypatch.setattr(jobs, "commit_guard", reject)
    client.post("/api/ai/analyze", json={"item_ids": [item_id]})
    jobs.run_once()
    with SessionLocal() as db:
        assert not any(t.origin == "ai" for t in db.get(Item, item_id).item_tags)
        assert db.scalar(select(Job).where(Job.kind == "ai")).status == "queued"


def test_picacg_favorites_sync_queues_metadata_without_inventing_author_follows(client, monkeypatch):
    account = client.post("/api/sources/picacg/accounts", json={"label": "哔咔", "credentials": {"token": "private-token"}})
    assert account.status_code == 200
    class FavoritesConnector(FakeConnector):
        def favorites(self, checkpoint=lambda value: None):
            checkpoint(50)
            return ["1234567890abcdef12345678", "abcdef1234567890abcdef12"]
    monkeypatch.setattr(jobs, "get_connector", lambda *args: FavoritesConnector())
    response = client.post("/api/sources/picacg/sync", json={"account_id": account.json()["id"]})
    assert response.status_code == 200
    jobs.run_once()
    with SessionLocal() as db:
        completed = db.scalar(select(Job).where(Job.kind == "source_sync"))
        assert completed.status == "completed", completed.error
        assert completed.result["remote_favorites"] == 2
        imports = db.scalars(select(Job).where(Job.kind == "import")).all()
        assert len(imports) == 2
        assert all(job.payload["download"] is False for job in imports)
        assert list(db.scalars(select(Subscription))) == []
    assert client.post("/api/subscriptions", json={"platform": "picacg", "target_type": "author", "target": "无接口"}).status_code == 400


def test_jm_download_uses_real_local_decoder_and_keeps_page_order(monkeypatch):
    from types import SimpleNamespace
    import jmcomic
    from app.integrations import jm
    class Album:
        name, authors, tags, description, page_count = "测试漫画", ["作者"], ["测试"], "说明", 2
        def __iter__(self):
            yield SimpleNamespace(photo_id="1")
    class API:
        postman = SimpleNamespace(get=lambda *args, **kwargs: None)
        def get_album_detail(self, ident): return Album()
        def get_photo_detail(self, ident, fetch_album):
            return [SimpleNamespace(img_url="https://cdn-msp.jmapiproxy2.cc/1.png", scramble_id=2, aid=1, img_file_name="1"),
                    SimpleNamespace(img_url="https://cdn-msp.jmapiproxy2.cc/2.png", scramble_id=2, aid=1, img_file_name="2")]
    class Transport:
        def __init__(self, *args, **kwargs): pass
        def close(self): pass
        def fetch(self, url): return png("blue" if url.endswith("2.png") else "white")
    monkeypatch.setattr(jmcomic.JmOption, "construct", lambda value: SimpleNamespace(new_jm_client=lambda: API()))
    # This test isolates the image decoder; transport pacing has SDK-boundary tests.
    monkeypatch.setattr(jm, "pace_sdk_requests", lambda *args, **kwargs: None)
    monkeypatch.setattr(jm, "SafeHTTP", Transport)
    monkeypatch.setattr(jm, "validate_url", lambda *args, **kwargs: None)
    result = jm.JMConnector({}).fetch("JM123")
    assert result.remote_id == "123"
    assert result.author == "作者"
    assert [name for name, data in result.files] == ["00001.png", "00002.png"]
    assert result.files[0][1] == png("white")
    assert result.files[1][1] == png("blue")
