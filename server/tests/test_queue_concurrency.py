"""Exercise actual parallel claims, live limits and cooperative interruption."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock
import time

import pytest
from sqlalchemy import select

from test_library import client, png
from app import jobs, queue_control
from app.database import SessionLocal
from app.integrations.base import JobCancelled, RemoteWork
from app.models import Item, Job, utcnow


@pytest.fixture
def parallel(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({"concurrency": 3, "rate_limit_enabled": False})
    release, entered, mutex = Event(), Event(), Lock()
    calls = []

    class Connector:
        def normalize_id(self, value):
            return value

        def fetch(self, ident, *, download=True, checkpoint=lambda _: None):
            queue_control.wait_for_request()
            with mutex:
                calls.append(ident)
                if len(calls) == 3:
                    entered.set()
            assert release.wait(10), "Test did not release the synthetic request"
            checkpoint(50)
            return RemoteWork(remote_id=ident, title="并行样本", source_url="https://example.invalid/work",
                              metadata_only=True, preview_cover=("cover.png", png()))

    monkeypatch.setattr(jobs, "get_connector", lambda *args: Connector())
    try:
        yield release, entered, calls
    finally:
        release.set()
        jobs._stop.clear()


def seed(count, kind="import"):
    with SessionLocal() as db:
        return [jobs.enqueue(db, kind, {"platform": "jm", "remote_id": str(index), "download": False}).id
                for index in range(count)]


def test_parallel_claims_obey_live_limit_without_duplicate_downloads(client, parallel):
    release, entered, calls = parallel
    identifiers = seed(7)
    idle, mutex = Event(), Lock()
    idle_count = 0
    def run():
        nonlocal idle_count
        result = jobs.run_once()
        if not result:
            with mutex:
                idle_count += 1
                if idle_count == 5:
                    idle.set()
        return result
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(run) for _ in range(8)]
        try:
            assert entered.wait(8)
            assert idle.wait(8)
            status = client.get("/api/queue/status").json()
            assert len(status["active_jobs"]) == status["counts"]["running"] == 3
            assert status["active"] == status["active_jobs"][0]
            assert all("payload" not in job for job in status["active_jobs"])
            with SessionLocal() as db:
                owners = list(db.scalars(select(Job.worker_id).where(Job.status == "running")))
                assert len(set(owners)) == 3
            # All other claimers must see the same global capacity. Reducing it
            # cannot kill those three requests or admit a fourth.
            assert client.put("/api/queue/settings", json={"concurrency": 1}).status_code == 200
            assert not jobs.run_once()
            assert client.get("/api/queue/status").json()["counts"]["running"] == 3
        finally:
            release.set()
        assert sum(future.result(timeout=8) for future in futures) == 3
    assert len(calls) == len(set(calls)) == 3
    with SessionLocal() as db:
        rows = [db.get(Job, identifier) for identifier in identifiers]
        assert sum(row.status == "completed" for row in rows) == 3
        assert sum(row.status == "queued" for row in rows) == 4
        assert all(row.attempts <= 1 for row in rows)
    assert client.get("/api/items").json()["total"] == 3
    # Raising the setting takes effect for subsequent claims without restart.
    queue_control.update_settings({"concurrency": 2})
    with SessionLocal() as db:
        for row in db.scalars(select(Job).where(Job.status == "queued").limit(2)):
            row.status, row.worker_id, row.lease_until = "running", "other-process", utcnow() + timedelta(minutes=3)
        db.commit()
    assert not jobs.run_once()
    queue_control.update_settings({"concurrency": 3})
    assert jobs.run_once()


@pytest.mark.parametrize("interrupt", ["pause", "cancel", "shutdown"])
def test_parallel_pause_cancel_and_shutdown_preserve_queue_state(client, parallel, interrupt):
    release, entered, calls = parallel
    identifiers = seed(3)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(jobs.run_once) for _ in range(3)]
        try:
            assert entered.wait(8)
            if interrupt == "pause":
                queue_control.update_settings({"paused": True})
            elif interrupt == "shutdown":
                jobs._stop.set()
            else:
                for identifier in identifiers:
                    assert client.post(f"/api/jobs/{identifier}/cancel").status_code == 200
        finally:
            release.set()
        assert all(future.result(timeout=8) for future in futures)
    with SessionLocal() as db:
        rows = [db.get(Job, identifier) for identifier in identifiers]
        assert all(row.status == ("cancelled" if interrupt == "cancel" else "queued") for row in rows)
        assert all(row.worker_id is None and row.lease_until is None and row.error is None for row in rows)
    assert client.get("/api/items").json()["total"] == 0


def test_ai_and_translation_have_independent_slots_at_acquisition_capacity(client, monkeypatch):
    queue_control.update_settings({"concurrency": 2, "paused": True})
    with SessionLocal() as db:
        for _ in range(2):
            db.add(Job(kind="import", status="running", payload={}, worker_id="other-process", lease_until=utcnow()+timedelta(minutes=3)))
        local = Job(kind="ai", status="queued", payload={})
        second = Job(kind="translate", status="queued", payload={})
        db.add_all([local, second]); db.commit()
    claimed = []
    monkeypatch.setattr(jobs, "process_job", lambda identifier: claimed.append(identifier))
    assert jobs.run_once()
    assert jobs.run_once()
    assert not jobs.run_once()
    assert len(claimed) == 2


def test_parallel_bodies_preserve_page_order_and_shared_blobs_and_tags(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({"concurrency": 3, "rate_limit_enabled": False, "paused": False})
    from threading import Barrier
    from app.storage import resolve_asset_path
    barrier = Barrier(3)
    files = [(f"page-{index}.png", png(color)) for index, color in enumerate(("red", "green", "blue"))]
    class Connector:
        def normalize_id(self, ident):
            return ident
        def fetch(self, ident, *, download=True, checkpoint=lambda _: None):
            assert download
            queue_control.wait_for_request()
            barrier.wait(timeout=8)
            return RemoteWork(remote_id=ident, title="远端标题", source_url="https://example.invalid/work",
                              files=files, tags=["正文共享新标签"], remote_page_count=3)
    monkeypatch.setattr(jobs, "get_connector", lambda *args: Connector())
    identifiers = []
    with SessionLocal() as db:
        for index in range(3):
            item = Item(title=f"人工标题 {index}", source="jm", source_id=str(index))
            db.add(item); db.commit()
            identifiers.append(item.id)
            jobs.enqueue(db, "import_content", {"platform": "jm", "remote_id": str(index), "item_id": item.id})
    with ThreadPoolExecutor(max_workers=3) as executor:
        assert all(executor.map(lambda _: jobs.run_once(), range(3)))
    with SessionLocal() as db:
        rows = list(db.scalars(select(Job).where(Job.kind == "import_content", Job.payload["item_id"].as_string().in_(identifiers))))
        assert len(rows) == 3 and all(row.status == "completed" for row in rows), [(row.status, row.error) for row in rows]
    paths = []
    for index, identifier in enumerate(identifiers):
        detail = client.get(f"/api/items/{identifier}").json()
        assert detail["title"] == f"人工标题 {index}"
        assert detail["content_state"] == "ready" and detail["page_count"] == 3
        assert any(tag["name"] == "正文共享新标签" for tag in detail["tags"])
        assert [client.get(asset["url"]).content for asset in detail["assets"]] == [content for _, content in files]
        with SessionLocal() as db:
            paths.append([asset.relative_path for asset in db.get(Item, identifier).assets])
    assert paths[0] == paths[1] == paths[2]
    assert all(resolve_asset_path(path).is_file() for path in paths[0])


def test_expired_attempt_cannot_checkpoint_or_commit_after_new_claim(client, monkeypatch):
    queue_control.update_settings({"concurrency": 2})
    identifier = seed(1)[0]
    owners = []
    def process(job_id):
        owners.append(jobs.current_worker_id())
        with SessionLocal() as db:
            row = db.get(Job, job_id)
            if len(owners) == 1:
                row.lease_until = utcnow() - timedelta(seconds=1)
            else:
                row.status = "completed"
            db.commit()
        if len(owners) == 1:
            assert jobs.run_once()
            with pytest.raises(JobCancelled):
                jobs.checkpoint(job_id, 90)
            with SessionLocal() as db, pytest.raises(JobCancelled):
                jobs.commit_guard(db, job_id)
    monkeypatch.setattr(jobs, "process_job", process)
    assert jobs.run_once()
    assert len(set(owners)) == 2
    with SessionLocal() as db:
        row = db.get(Job, identifier)
        assert row.status == "completed" and row.attempts == 2


def test_parallel_requests_share_pacing_and_upstream_cooldown(client):
    queue_control.update_settings({"concurrency": 3, "min_interval_seconds": .1, "max_interval_seconds": .1})
    with queue_control.request_context(lambda: None):
        queue_control.defer_requests(.2)
    def request():
        with queue_control.request_context(lambda: None):
            queue_control.wait_for_request()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: request(), range(3)))
    assert time.monotonic() - started >= .35


def test_worker_pool_starts_once_and_stops_all_lanes(client, monkeypatch):
    monkeypatch.setenv("LIBRARY_WORKER_ENABLED", "true")
    monkeypatch.setenv("LIBRARY_WORKER_MODE", "thread")
    mutex = Lock()
    entered = Event()
    count = 0
    def idle():
        nonlocal count
        with mutex:
            count += 1
            if count == 48:
                entered.set()
        jobs._stop.wait(5)
    monkeypatch.setattr(jobs, "_loop", idle)
    try:
        jobs.start_worker()
        assert entered.wait(3)
        jobs.start_worker()
        assert count == 48 and len(jobs._threads) == 48
    finally:
        jobs.stop_worker()
    assert all(not thread.is_alive() for thread in jobs._threads)
    jobs._stop.clear()
