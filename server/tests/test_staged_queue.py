from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_library import client, png
from app import jobs, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError, JobCancelled, RemoteWork
from app.models import Asset, Item, Job, QueueControl, Subscription, utcnow
from app.storage import resolve_asset_path


class StagedConnector:
    supports_subscriptions = True
    preview = True
    calls = None
    fail_body = False

    def normalize_id(self, value):
        return value

    def fetch(self, ident, *, download=True, checkpoint=lambda value: None):
        queue_control.wait_for_request()
        if self.calls is not None:
            self.calls.append(download)
        checkpoint(50)
        if download and self.fail_body:
            raise ConnectorError("正文暂时不可用")
        return RemoteWork(remote_id=ident, title="分阶段作品", source_url="https://www.pixiv.net/artworks/123",
            files=[("正文.png", png("blue"))] if download else [], metadata_only=not download,
            preview_cover=("cover.png", png()) if self.preview and not download else None)


@pytest.fixture
def staged(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({"request_interval_seconds": 0})
    connector = StagedConnector()
    connector.calls = []
    monkeypatch.setattr(jobs, "get_connector", lambda *args: connector)
    return connector


def enqueue_cover(download=True, ident="123", item_id=None):
    with SessionLocal() as db:
        payload = {"platform": "jm", "remote_id": ident, "download": download}
        if item_id:
            payload["item_id"] = item_id
        return jobs.enqueue_import(db, payload).id


def cover_stage(client):
    ident = enqueue_cover()
    assert jobs.run_once()
    phase = client.get(f"/api/jobs/{ident}").json()
    assert phase["status"] == "completed", phase
    return client.get(f"/api/items/{phase['item_id']}").json()


def test_cover_commits_before_body_without_polluting_assets_and_is_authenticated(client, staged):
    item = cover_stage(client)
    assert staged.calls == [False]
    assert item["content_state"] == "queued"
    assert item["assets"] == [] and item["page_count"] == 0
    assert client.get(f"/api/items/{item['id']}/export").status_code == 409
    assert client.get(item["cover_url"]).headers["content-type"] == "image/webp"
    with SessionLocal() as db:
        record = db.get(Item, item["id"])
        assert record.preview_cover["relative_path"].startswith("collections/jm/blobs/")
        assert resolve_asset_path(record.preview_cover["relative_path"]).is_file()
    assert jobs.run_once()
    ready = client.get(f"/api/items/{item['id']}").json()
    assert staged.calls == [False, True]
    assert ready["content_state"] == "ready" and ready["page_count"] == 1
    assert len(ready["assets"]) == 1
    assert client.get(ready["assets"][0]["url"]).content == png("blue")
    client.post("/api/auth/logout")
    assert client.get(item["cover_url"]).status_code == 401


def test_absent_or_invalid_cover_never_blocks_body_and_message_is_truthful(client, staged, monkeypatch):
    monkeypatch.setattr(staged, "preview", False)
    item = cover_stage(client)
    assert item["cover_url"] is None
    phase = next(j for j in client.get("/api/jobs").json()["items"] if j["kind"] == "import")
    assert phase["result"]["cover_saved"] is False
    assert "封面暂不可用" in phase["result"]["message"]
    assert jobs.run_once()
    assert client.get(f"/api/items/{item['id']}").json()["content_state"] == "ready"

    import app.library as library
    monkeypatch.setattr(staged, "preview", True)
    monkeypatch.setattr(library, "save_preview_cover", lambda *args: (_ for _ in ()).throw(ValueError("broken")))
    ident = enqueue_cover(ident="456")
    assert jobs.run_once()
    phase = client.get(f"/api/jobs/{ident}").json()
    assert phase["status"] == "completed" and phase["result"]["cover_saved"] is False
    assert jobs.run_once()
    assert client.get(f"/api/items/{phase['item_id']}").json()["content_state"] == "ready"


def test_failure_cancel_and_retry_keep_preview_and_manual_metadata(client, staged):
    item = cover_stage(client)
    body_id = item["content_job_id"]
    client.patch(f"/api/items/{item['id']}", json={"title": "人工标题", "rating": "general", "tags": ["角色:人工"]})
    staged.fail_body = True
    assert jobs.run_once()
    failed = client.get(f"/api/items/{item['id']}").json()
    assert failed["content_state"] == "failed" and failed["content_error"] == "正文暂时不可用"
    assert client.get(failed["cover_url"]).status_code == 200
    assert client.post(f"/api/jobs/{body_id}/retry").json()["id"] == body_id
    assert client.post(f"/api/jobs/{body_id}/cancel").status_code == 200
    cancelled = client.get(f"/api/items/{item['id']}").json()
    assert cancelled["content_state"] == "metadata" and cancelled["cover_url"]
    assert client.post(f"/api/jobs/{body_id}/retry").status_code == 200
    staged.fail_body = False
    assert jobs.run_once()
    ready = client.get(f"/api/items/{item['id']}").json()
    assert ready["content_state"] == "ready" and ready["title"] == "人工标题"
    assert ready["rating"] == "general"
    assert any(t["name"] == "角色:人工" and t["origin"] == "manual" for t in ready["tags"])


def test_repeated_download_and_retry_reuse_active_body(client, staged):
    item = cover_stage(client)
    body_id = item["content_job_id"]
    assert client.post(f"/api/items/{item['id']}/download").json()["id"] == body_id
    with SessionLocal() as db:
        body = db.get(Job, body_id)
        body.status, body.worker_id, body.lease_until = "running", "other-worker", utcnow() + timedelta(minutes=3)
        db.commit()
    assert client.post(f"/api/items/{item['id']}/download").json()["id"] == body_id
    assert client.get(f"/api/items/{item['id']}").json()["content_state"] == "downloading"
    with SessionLocal() as db:
        old = Job(kind="import_content", status="failed", payload={"platform": "jm", "remote_id": "123", "item_id": item["id"]})
        db.add(old); db.commit(); old_id = old.id
    assert client.post(f"/api/jobs/{old_id}/retry").json()["id"] == body_id
    with SessionLocal() as db:
        assert len(db.scalars(select(Job).where(Job.kind == "import_content", Job.status.in_(("queued", "running")))).all()) == 1


def test_backfill_only_missing_covers_is_idempotent_and_preserves_subscriptions(client, staged):
    with SessionLocal() as db:
        missing = Item(title="旧元数据", source="jm", source_id="123")
        local = Item(title="本地", source="local")
        subscription = Subscription(platform="pixiv", target_type="tag", target="test", label="test", auto_download=False,
                                    next_check_at=utcnow() + timedelta(days=1))
        db.add_all([missing, local, subscription]); db.commit(); item_id = missing.id; sub_id = subscription.id
    assert client.post("/api/queue/covers", json={"source": "jm"}).json()["queued"] == 1
    assert client.post("/api/queue/covers", json={"source": "jm"}).json()["queued"] == 0
    assert jobs.run_once()
    saved = client.get(f"/api/items/{item_id}").json()
    assert saved["cover_url"] and saved["content_state"] == "metadata" and saved["page_count"] == 0
    assert client.post("/api/queue/covers", json={"source": "jm"}).json()["queued"] == 0
    with SessionLocal() as db:
        assert not db.get(Subscription, sub_id).auto_download
        assert db.scalar(select(Job).where(Job.kind == "import_content")) is None


def test_pause_during_body_requeues_without_failure_and_ai_remains_eligible(client, staged, monkeypatch):
    item = cover_stage(client)
    original = staged.fetch
    def pause_fetch(*args, **kwargs):
        queue_control.update_settings({"paused": True})
        return original(*args, **kwargs)
    monkeypatch.setattr(staged, "fetch", pause_fetch)
    assert jobs.run_once()
    phase = client.get(f"/api/jobs/{item['content_job_id']}").json()
    assert phase["status"] == "queued" and phase["error"] is None
    assert client.get(f"/api/items/{item['id']}").json()["cover_url"]
    assert not jobs.run_once()
    import app.ai.service as ai
    monkeypatch.setattr(ai, "analyze_item", lambda *args: {"local": True})
    with SessionLocal() as db:
        ai_id = jobs.enqueue(db, "ai", {"item_id": item["id"]}).id
    assert jobs.run_once()
    assert client.get(f"/api/jobs/{ai_id}").json()["status"] == "completed"
    monkeypatch.setattr(staged, "fetch", original)
    queue_control.update_settings({"paused": False})
    assert jobs.run_once()
    assert client.get(f"/api/items/{item['id']}").json()["content_state"] == "ready"


def test_deleted_item_is_not_resurrected_by_inflight_cover(client, staged, monkeypatch):
    with SessionLocal() as db:
        item = Item(title="待删", source="jm", source_id="123")
        db.add(item); db.commit(); item_id = item.id
    ident = enqueue_cover(item_id=item_id)
    original = staged.fetch
    def delete_fetch(*args, **kwargs):
        assert client.delete(f"/api/items/{item_id}").status_code == 200
        return original(*args, **kwargs)
    monkeypatch.setattr(staged, "fetch", delete_fetch)
    assert jobs.run_once()
    phase = client.get(f"/api/jobs/{ident}").json()
    assert phase["status"] == "completed" and phase["result"]["skipped"]
    assert client.get("/api/items").json()["total"] == 0


def test_cover_priority_has_bounded_body_starvation(client, staged):
    first = cover_stage(client)
    for ident in range(10):
        enqueue_cover(download=False, ident=str(1000 + ident))
    for _ in range(3):
        assert jobs.run_once()
    assert staged.calls == [False] * 4
    assert jobs.run_once()
    assert staged.calls[-1] is True
    assert client.get(f"/api/items/{first['id']}").json()["content_state"] == "ready"


def test_expired_body_lease_recovers_retaining_cover(client, staged):
    item = cover_stage(client)
    with SessionLocal() as db:
        body = db.get(Job, item["content_job_id"])
        body.status, body.worker_id, body.lease_until = "running", "crashed-worker", utcnow() - timedelta(seconds=1)
        db.get(Item, item["id"]).content_state = "downloading"
        db.commit()
    assert jobs.run_once()
    ready = client.get(f"/api/items/{item['id']}").json()
    assert ready["content_state"] == "ready" and ready["cover_url"]
    with SessionLocal() as db:
        db.get(Asset, ready["assets"][0]["id"]).status = "missing"
        db.commit()
    assert client.get(f"/api/items/{item['id']}").json()["content_state"] == "failed"


def test_random_pacing_waits_outside_transactions_and_respects_backoff(client, monkeypatch):
    now = [datetime(2026, 1, 1)]
    sleeps, bounds = [], []
    monkeypatch.setattr(queue_control, "utcnow", lambda: now[0])
    monkeypatch.setattr(queue_control.random, "uniform", lambda lo, hi: bounds.append((lo, hi)) or hi)
    def sleep(seconds):
        assert 0 < seconds <= .25
        # An independent write during each wait proves the gate released locks.
        with SessionLocal() as db:
            control = queue_control.lock_control(db)
            control.cover_streak = 0
            db.commit()
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)
    monkeypatch.setattr(queue_control, "time", SimpleNamespace(sleep=sleep))
    with queue_control.request_context(lambda: None):
        queue_control.wait_for_request()
        queue_control.wait_for_request()
        assert sum(sleeps) == 5 and bounds == [(2, 5), (2, 5)]
        queue_control.defer_requests(20)
        queue_control.update_settings({"request_interval_seconds": 0})
        queue_control.wait_for_request()
        assert sum(sleeps) == 25 and bounds[-1] == (0, 0)


def test_wait_is_cancellable_paused_and_noop_outside_acquisition(client, monkeypatch):
    queue_control.update_settings({"paused": True})
    with queue_control.request_context(lambda: None), pytest.raises(queue_control.QueuePaused):
        queue_control.wait_for_request()
    queue_control.update_settings({"paused": False})
    with queue_control.request_context(lambda: None):
        queue_control.defer_requests(999999)
    with SessionLocal() as db:
        assert 3598 < (db.get(QueueControl, 1).next_request_at - utcnow()).total_seconds() <= 3600
    with queue_control.request_context(lambda: (_ for _ in ()).throw(JobCancelled())), pytest.raises(JobCancelled):
        queue_control.wait_for_request()
    monkeypatch.setattr(queue_control, "SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("outside acquisition must not access DB")))
    queue_control.wait_for_request()
    queue_control.defer_requests(50)


@pytest.mark.parametrize("cancelled", [False, True])
def test_shutdown_transport_error_requeues_owned_job_without_overwriting_cancel(client, staged, monkeypatch, cancelled):
    item = cover_stage(client)
    job_id = item["content_job_id"]
    def interrupted_fetch(*args, **kwargs):
        if cancelled:
            assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        jobs._stop.set()
        raise ConnectorError("代理连接已关闭")
    monkeypatch.setattr(staged, "fetch", interrupted_fetch)
    assert jobs.run_once()
    phase = client.get(f"/api/jobs/{job_id}").json()
    assert phase["status"] == ("cancelled" if cancelled else "queued")
    assert phase["error"] is None
    saved = client.get(f"/api/items/{item['id']}").json()
    assert saved["content_state"] == ("metadata" if cancelled else "queued")
    assert saved["content_error"] is None and client.get(saved["cover_url"]).status_code == 200
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.worker_id is None and job.lease_until is None
