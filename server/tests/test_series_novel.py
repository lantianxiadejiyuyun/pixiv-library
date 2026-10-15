from datetime import timedelta

import pytest
from sqlalchemy import select

from test_library import client, png, upload
from app import jobs, queue_control, series_api
from app.database import SessionLocal
from app.integrations.base import RemoteWork, JobCancelled
from app.library import import_files
from app.models import Item, Job, Series, SeriesEntry, Subscription, utcnow
from app.novel_text import parse_sections
from app.series_service import enqueue_series


def new_item(title="作品", text=None):
    with SessionLocal() as db:
        return import_files(db, [("novel.txt", text.encode())] if text is not None else [("1.png", png())],
                            title=title, item_type="novel" if text is not None else "manga").id


def test_sections_are_explicit_plain_text_and_original_is_unchanged(client):
    original = "序言\n[newpage][chapter:上篇]\n<script>alert(1)</script>正文😀\n[chapter:下篇]结尾[newpage]尾声"
    item_id = new_item(text=original)
    item = client.get(f"/api/items/{item_id}").json()
    asset = item["assets"][0]
    response = client.get(f"/api/assets/{asset['id']}/text-sections")
    assert response.status_code == 200
    sections = response.json()["sections"]
    assert [entry["index"] for entry in sections] == [0, 1, 2, 3]
    assert [entry["title"] for entry in sections] == ["正文", "上篇", "下篇", "第 4 节"]
    assert sections[1]["text"] == "\n<script>alert(1)</script>正文😀\n"
    assert client.get(asset["url"]).content.decode() == original
    assert client.get(f"/api/items/{item_id}").json()["page_count"] == 1
    image = upload(client).json()
    assert client.get(f"/api/assets/{image['assets'][0]['id']}/text-sections").status_code == 400
    client.post("/api/auth/logout")
    assert client.get(f"/api/assets/{asset['id']}/text-sections").status_code == 401


def test_text_progress_validates_actual_sections_and_old_page_requests_remain_valid(client):
    item_id = new_item(text="[chapter:上篇]first[newpage][chapter:下篇]second")
    response = client.put(f"/api/items/{item_id}/progress", json={"page": 0, "section": 1, "offset": .625})
    assert response.status_code == 200
    assert response.json()["section"] == 1 and response.json()["offset"] == .625
    assert client.get(f"/api/items/{item_id}").json()["progress"] == {"page": 0, "section": 1, "offset": .625}
    assert client.put(f"/api/items/{item_id}/progress", json={"page": 0, "section": 2}).status_code == 400
    assert client.put(f"/api/items/{item_id}/progress", json={"page": 0, "offset": 1.01}).status_code == 422
    assert client.put(f"/api/items/{item_id}/progress", json={"page": 0}).json()["section"] == 0
    image_id = new_item()
    assert client.put(f"/api/items/{image_id}/progress", json={"page": 0, "section": 1}).status_code == 400


def test_unknown_markup_is_preserved_and_sections_are_bounded():
    text = "[chapter:] [chapter:incomplete [pixivimage:123] <iframe>标题相似的上集"
    assert parse_sections(text) == [{"index": 0, "title": "正文", "text": text}]
    assert len(parse_sections("[newpage][chapter:标题]正文")) == 1
    with pytest.raises(ValueError, match="5000"):
        parse_sections("body[newpage]" * 5001)


def test_manual_series_reorder_revision_and_removal_preserve_items(client):
    first, second = new_item("下集"), new_item("上集")
    created = client.post("/api/series", json={"title": "我的上下集", "kind": "manga",
        "entries": [{"item_id": first, "label": "上篇"}, {"item_id": second, "label": "下篇"}]}).json()
    assert created["origin"] == "manual" and created["source"] == "local" and created["source_id"] is None
    assert [entry["item_id"] for entry in created["entries"]] == [first, second]  # No title inference.
    assert client.get(f"/api/items/{first}").json()["series"][0]["id"] == created["id"]
    update = {"revision": created["revision"], "title": "已排序", "entries": [{"item_id": second}, {"item_id": first}]}
    updated = client.put(f"/api/series/{created['id']}", json=update).json()
    assert [entry["item_id"] for entry in updated["entries"]] == [second, first]
    assert [entry["position"] for entry in updated["entries"]] == [0, 1]
    assert updated["revision"] == created["revision"] + 1
    assert client.put(f"/api/series/{created['id']}", json=update).status_code == 409
    removed = client.put(f"/api/series/{created['id']}", json={"revision": updated["revision"], "title": "仅下集", "entries": [{"item_id": second}]}).json()
    assert len(removed["entries"]) == 1
    assert client.get(f"/api/items/{first}").json()["series"] == []
    assert client.get(f"/api/items/{first}/export").status_code == 200
    assert client.post("/api/series", json={"title": "重复", "kind": "manga", "entries": [{"item_id": first}, {"item_id": first}]}).status_code == 400
    assert client.post("/api/series", json={"title": "缺失", "kind": "novel", "entries": [{"item_id": "missing"}]}).status_code == 404


class CatalogConnector:
    supports_subscriptions = True
    complete = True
    def __init__(self):
        self.calls = []
        self.ids = ["novel:90", "novel:10"]

    def validate_credentials(self):
        pass

    def normalize_series(self, value, kind=None):
        assert kind in ("novel", "manga")
        return value, kind

    def normalize_id(self, value):
        return value

    def fetch_series(self, remote_id, kind, checkpoint=lambda value: None):
        queue_control.wait_for_request()
        self.calls.append(("series", remote_id))
        checkpoint(50)
        return {"remote_id": remote_id, "title": "官方目录", "kind": kind, "complete": self.complete,
                "works": [RemoteWork(remote_id=ident, title=ident, item_type="novel", source_url="https://www.pixiv.net/novel/show.php?id=" + ident.split(":")[-1],
                                     remote_page_count=3, tags=["source-tag"], metadata_only=True) for ident in self.ids]}

    def fetch(self, ident, *, download=True, checkpoint=lambda value: None):
        self.calls.append(("body" if download else "cover", ident))
        checkpoint(50)
        return RemoteWork(remote_id=ident, title=ident, item_type="novel", source_url="https://www.pixiv.net/novel/show.php?id=1",
                          preview_cover=("cover.png", png()) if not download else None,
                          files=[("novel.txt", b"body")] if download else [], metadata_only=not download,
                          remote_page_count=3, source_series={"remote_id": "777", "title": "官方目录", "kind": "novel"})

    def discover(self, target_type, target, content_scope="images"):
        self.calls.append(("discover", content_scope))
        return (["novel:90"] if content_scope == "novels" else ["90"]), False


@pytest.fixture
def catalog(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({"request_interval_seconds": 0})
    value = CatalogConnector()
    monkeypatch.setattr(jobs, "get_connector", lambda *args: value)
    monkeypatch.setattr(series_api, "get_connector", lambda *args: value)
    return value


def queue_catalog(client, ident="777"):
    response = client.post("/api/series/import", json={"platform": "pixiv", "url_or_id": ident, "kind": "novel"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_official_series_catalog_order_metadata_queue_and_manual_edits(client, catalog):
    ident = queue_catalog(client)
    assert queue_catalog(client) == ident
    assert jobs.run_once()
    phase = client.get(f"/api/jobs/{ident}").json()
    assert phase["status"] == "completed", phase
    series_id = phase["result"]["series_id"]
    series = client.get(f"/api/series/{series_id}").json()
    assert series["complete"] and series["origin"] == "source"
    assert [entry["title"] for entry in series["entries"]] == ["novel:90", "novel:10"]
    assert all(entry["page_count"] == 0 and entry["content_state"] == "metadata" for entry in series["entries"])
    with SessionLocal() as db:
        tasks = db.scalars(select(Job).where(Job.kind == "import")).all()
        assert len(tasks) == 2 and all(task.payload["download"] is False for task in tasks)
        assert not db.scalar(select(Job).where(Job.kind == "import_content"))
    first = series["entries"][0]["item_id"]
    client.patch(f"/api/items/{first}", json={"title": "人工标题", "tags": ["人工分类"]})
    item = client.get(f"/api/items/{first}").json()
    assert item["remote_page_count"] == 3 and item["source_series"]["remote_id"] == "777"
    assert item["series"][0]["id"] == series_id
    assert client.put(f"/api/series/{series_id}", json={"revision": series["revision"], "title": "改官方", "entries": []}).status_code == 409
    manual = client.post("/api/series", json={"title": "人工合集", "kind": "novel", "entries": [{"item_id": first}]}).json()
    assert len(client.get(f"/api/items/{first}").json()["series"]) == 2
    assert jobs.run_once() and jobs.run_once()
    assert all(call[0] != "body" for call in catalog.calls)
    assert client.get(f"/api/items/{first}").json()["title"] == "人工标题"
    catalog.ids.reverse()
    retry = queue_catalog(client)
    assert jobs.run_once()
    assert client.get(f"/api/jobs/{retry}").json()["status"] == "completed"
    refreshed = client.get(f"/api/series/{series_id}").json()
    assert refreshed["entries"][1]["item_id"] == first
    assert client.get(f"/api/series/{manual['id']}").json()["entries"][0]["item_id"] == first
    assert client.delete(f"/api/items/{first}").status_code == 200
    assert len(client.get(f"/api/series/{series_id}").json()["entries"]) == 1
    assert client.get(f"/api/series/{series_id}").json()["complete"] is False


def test_partial_refresh_does_not_erase_saved_directory(client, catalog):
    ident = queue_catalog(client)
    jobs.run_once()
    series_id = client.get(f"/api/jobs/{ident}").json()["result"]["series_id"]
    jobs.run_once(); jobs.run_once()
    catalog.complete, catalog.ids = False, ["novel:90"]
    refresh = queue_catalog(client)
    jobs.run_once()
    result = client.get(f"/api/jobs/{refresh}").json()["result"]
    assert result["directory_updated"] is False
    assert len(client.get(f"/api/series/{series_id}").json()["entries"]) == 2


def test_series_pause_fairness_cancel_and_single_acquisition(client, catalog, monkeypatch):
    with SessionLocal() as db:
        for number in range(12):
            jobs.enqueue_import(db, {"platform": "pixiv", "remote_id": str(number + 100), "download": False})
    series_job = queue_catalog(client)
    for _ in range(4):
        assert jobs.run_once()
    assert client.get(f"/api/jobs/{series_job}").json()["status"] == "queued"
    assert jobs.run_once()
    assert client.get(f"/api/jobs/{series_job}").json()["status"] == "completed"
    queue_control.update_settings({"paused": True})
    another = queue_catalog(client, "888")
    assert not jobs.run_once()
    queue_control.update_settings({"paused": False})
    with SessionLocal() as db:
        active = Job(kind="series_sync", status="running", payload={}, worker_id="other-worker", lease_until=utcnow() + timedelta(minutes=1))
        db.add(active)
        for pending in db.scalars(select(Job).where(Job.kind == "import", Job.status == "queued")):
            pending.status = "cancelled"
        db.commit(); active_id = active.id
    assert not jobs.run_once()
    assert client.get(f"/api/jobs/{another}").json()["status"] == "queued"
    with SessionLocal() as db:
        db.get(Job, active_id).status = "completed"
        for job in db.scalars(select(Job).where(Job.kind == "import", Job.status == "queued")):
            job.status = "cancelled"
        db.commit()
    original = catalog.fetch_series
    def cancelled(*args, **kwargs):
        result = original(*args, **kwargs)
        assert client.post(f"/api/jobs/{another}/cancel").status_code == 200
        return result
    monkeypatch.setattr(catalog, "fetch_series", cancelled)
    assert jobs.run_once()
    assert client.get(f"/api/jobs/{another}").json()["status"] == "cancelled"
    with SessionLocal() as db:
        assert db.scalar(select(Series).where(Series.source_id == "888")) is None


def test_subscription_scope_persists_and_only_pixiv_passes_scope(client, catalog, monkeypatch):
    response = client.post("/api/subscriptions", json={"platform": "pixiv", "target_type": "author", "target": "123", "content_scope": "novels"})
    assert response.status_code == 200 and response.json()["content_scope"] == "novels"
    assert jobs.run_once()
    assert ("discover", "novels") in catalog.calls
    with SessionLocal() as db:
        cover = db.scalar(select(Job).where(Job.kind == "import"))
        assert cover.payload["remote_id"] == "novel:90" and cover.payload["download"] is False
    assert client.post("/api/subscriptions", json={"platform": "jm", "target_type": "tag", "target": "x", "content_scope": "novels"}).status_code == 400
    legacy = client.post("/api/subscriptions", json={"platform": "pixiv", "target_type": "tag", "target": "x"}).json()
    assert legacy["content_scope"] == "images"
    other = client.post("/api/subscriptions", json={"platform": "picacg", "target_type": "tag", "target": "x"}).json()
    assert client.patch(f"/api/subscriptions/{other['id']}", json={"content_scope": "all"}).status_code == 400


def test_old_metadata_cover_refresh_adds_navigation_without_overwriting_manual_title(client, catalog):
    with SessionLocal() as db:
        item = Item(source="pixiv", source_id="novel:90", title="手工标题", type="novel")
        db.add(item); db.commit(); item_id = item.id
        jobs.enqueue_import(db, {"platform": "pixiv", "remote_id": "novel:90", "download": False})
    assert jobs.run_once()
    item = client.get(f"/api/items/{item_id}").json()
    assert item["title"] == "手工标题" and item["page_count"] == 0
    assert item["remote_page_count"] == 3 and item["source_series"]["kind"] == "novel"
    assert item["content_state"] == "metadata" and item["cover_url"]


def test_series_retry_reuses_active_task_and_api_requires_login(client, catalog):
    active_id = queue_catalog(client)
    with SessionLocal() as db:
        old = Job(kind="series_sync", status="failed", payload={"platform": "pixiv", "remote_id": "777", "series_kind": "novel"})
        db.add(old); db.commit(); old_id = old.id
    assert client.post(f"/api/jobs/{old_id}/retry").json()["id"] == active_id
    assert client.get("/api/series/missing").status_code == 404
    assert client.post("/api/series", json={"title": " ", "kind": "novel", "entries": [{"item_id": "missing"}]}).status_code == 422
    client.post("/api/auth/logout")
    assert client.get("/api/series/missing").status_code == 401
    assert client.post("/api/series/import", json={"url_or_id": "777", "kind": "novel"}).status_code == 401


@pytest.mark.parametrize("has_catalog", [False, True])
def test_delete_during_series_fetch_is_not_resurrected_but_later_explicit_refresh_can_restore(client, catalog, monkeypatch, has_catalog):
    if has_catalog:
        initial_job = queue_catalog(client)
        jobs.run_once()
        series_id = client.get(f"/api/jobs/{initial_job}").json()["result"]["series_id"]
        removed_id = client.get(f"/api/series/{series_id}").json()["entries"][0]["item_id"]
        jobs.run_once(); jobs.run_once()  # Finish the initial cover jobs.
        # Exercise the SeriesEntry branch independently of source_series JSON.
        with SessionLocal() as db:
            db.get(Item, removed_id).source_series = None
            db.commit()
    else:
        with SessionLocal() as db:
            item = Item(source="pixiv", source_id="novel:90", type="novel", title="旧独立作品",
                        source_series={"remote_id": "777", "kind": "novel", "title": "官方目录"})
            db.add(item); db.commit(); removed_id = item.id
    original = catalog.fetch_series
    def delete_during_fetch(*args, **kwargs):
        assert client.delete(f"/api/items/{removed_id}").status_code == 200
        return original(*args, **kwargs)
    monkeypatch.setattr(catalog, "fetch_series", delete_during_fetch)
    refresh_job = queue_catalog(client)
    assert jobs.run_once()
    result = client.get(f"/api/jobs/{refresh_job}").json()["result"]
    assert result["skipped_deleted"] == 1 and result["complete"] is False
    with SessionLocal() as db:
        assert db.scalar(select(Item).where(Item.source == "pixiv", Item.source_id == "novel:90")) is None
        assert not db.scalar(select(Job).where(Job.kind == "import", Job.status == "queued",
                                               Job.payload["remote_id"].as_string() == "novel:90"))
    saved = client.get(f"/api/series/{result['series_id']}").json()
    assert len(saved["entries"]) == 1 and saved["complete"] is False
    monkeypatch.setattr(catalog, "fetch_series", original)
    next_job = queue_catalog(client)
    for _ in range(5):
        jobs.run_once()
        if client.get(f"/api/jobs/{next_job}").json()["status"] == "completed":
            break
    restored = client.get(f"/api/series/{result['series_id']}").json()
    assert restored["complete"] and len(restored["entries"]) == 2
    assert restored["entries"][0]["item_id"] != removed_id
