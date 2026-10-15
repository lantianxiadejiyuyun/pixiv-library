import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.library import import_files, reconcile_assets, set_tags
from app.main import app
from app.models import Asset, Item, ItemTag, Tag
from app.storage import MARKER, resolve_asset_path
from conftest import assert_test_database


def png(color="white"):
    stream = io.BytesIO()
    Image.new("RGB", (12, 16), color).save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    assert settings.environment == "test", "Refusing to reset a non-test application database"
    assert_test_database(engine)
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "import_root", tmp_path / "import")
    monkeypatch.setattr(settings, "admin_password", "local-library")
    monkeypatch.setattr(settings, "storage_require_marker", False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as test_client:
        response = test_client.post("/api/auth/login", json={"password": "local-library"})
        assert response.status_code == 200
        yield test_client


def upload(client, files=None, **fields):
    return client.post("/api/imports/upload", files=files or [("files", ("图书.png", png(), "image/png"))], data=fields)


def test_authentication_and_origin(client):
    assert client.get("/api/auth/status").json() == {"authenticated": True}
    assert client.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/items").status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    response = client.post("/api/auth/login", json={"password": "local-library"}, headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    client.cookies.set("qiye_session", "forged.signature")
    assert client.get("/api/items").status_code == 401


def test_image_import_persists_and_deduplicates_blobs(client):
    first = upload(client, title="中文图册").json()
    second = upload(client, title="另一份").json()
    assert first["page_count"] == 1
    assert first["title"] == "中文图册"
    with SessionLocal() as db:
        one = db.get(Asset, first["assets"][0]["id"])
        two = db.get(Asset, second["assets"][0]["id"])
        assert one.relative_path == two.relative_path
        stored = resolve_asset_path(one.relative_path)
        assert stored.read_bytes() == png()
    assert client.get(first["assets"][0]["url"]).content == png()
    assert client.get(first["cover_url"]).headers["content-type"] == "image/webp"
    assert client.delete(f"/api/items/{first['id']}").status_code == 200
    assert stored.is_file()  # Item removal is deliberately non-destructive.
    assert client.get(f"/api/items/{second['id']}").status_code == 200
    client.post("/api/auth/logout")
    assert client.get(second["assets"][0]["url"]).status_code == 401


def test_zip_natural_order_and_traversal_rejection(client):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("漫画/10.png", png("red"))
        stream.writestr("漫画/2.png", png("blue"))
    response = upload(client, [("files", ("book.cbz", archive.getvalue(), "application/zip"))])
    assert response.status_code == 200, response.text
    item = response.json()
    assert [a["original_name"] for a in item["assets"]] == ["2.png", "10.png"]
    exported = client.get(f"/api/items/{item['id']}/export")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as result:
        assert result.namelist() == ["0001_2.png", "0002_10.png"]
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as stream:
        stream.writestr("../escape.png", png())
    response = upload(client, [("files", ("bad.zip", malicious.getvalue(), "application/zip"))])
    assert response.status_code == 400
    assert client.get("/api/items").json()["total"] == 1


def test_text_is_plain_and_folder_cannot_escape(client, tmp_path):
    text = "<script>alert('x')</script>\n正文".encode()
    item = upload(client, [("files", ("文章.md", text, "text/markdown"))], type="article").json()
    response = client.get(item["assets"][0]["url"])
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content == text
    assert "sandbox" in response.headers["content-security-policy"]
    assert client.post("/api/imports/folder", json={"path": "../"}).status_code == 400
    folder = settings.import_root / "本地文章"
    folder.mkdir(parents=True)
    (folder / "文章.txt").write_text("本地正文", encoding="utf-8")
    response = client.post("/api/imports/folder", json={"path": "本地文章", "type": "article"})
    assert response.status_code == 200, response.text


def test_metadata_manual_tags_progress_and_filters(client):
    item = upload(client).json()
    id_ = item["id"]
    response = client.patch(f"/api/items/{id_}", json={"title": "星际旅行", "author": "匿名作者", "rating": "general", "tags": ["旅行", "已整理"]})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        stored = db.get(Item, id_)
        set_tags(db, stored, ["character:example"], "ai")
        db.commit()
    updated = client.get(f"/api/items/{id_}").json()
    assert {(t["name"], t["origin"]) for t in updated["tags"]} == {("旅行", "manual"), ("已整理", "manual"), ("character:example", "ai")}
    assert updated["rating_origin"] == "manual"
    assert client.get("/api/items", params={"q": "星际", "tag": "旅行", "rating": "general"}).json()["total"] == 1
    assert client.get("/api/items", params={"q": "character:exam"}).json()["items"][0]["id"] == id_
    assert client.get("/api/items", params={"tag": "不存在"}).json()["total"] == 0
    assert client.put(f"/api/items/{id_}/progress", json={"page": 0}).status_code == 200
    assert client.put(f"/api/items/{id_}/progress", json={"page": 10}).status_code == 400
    assert client.get(f"/api/items/{id_}").json()["progress"] == {"page": 0, "section": 0, "offset": 0.0}
    assert client.get("/api/dashboard").json()["total_items"] == 1


def test_storage_marker_and_missing_file_recovery(client):
    item = upload(client).json()
    with SessionLocal() as db:
        asset = db.get(Asset, item["assets"][0]["id"])
        path = resolve_asset_path(asset.relative_path)
        original = path.read_bytes()
        path.unlink()
        reconcile_assets(db)
        assert asset.status == "missing"
        path.write_bytes(original)
        reconcile_assets(db)
        assert asset.status == "available"
    (settings.storage_root / MARKER).unlink()
    assert upload(client).status_code == 503
    assert client.get("/api/health").status_code == 503
    assert client.get("/api/items").json()["total"] == 1


def test_source_idempotency_and_metadata_only_upgrade(client):
    with SessionLocal() as db:
        metadata = Item(title="手动标题", source="pixiv", source_id="123", type="illustration", rating="general", rating_origin="manual")
        db.add(metadata)
        db.commit()
        assert client.get(f"/api/items/{metadata.id}/export").status_code == 409
        set_tags(db, metadata, ["人工标签"], "manual")
        db.commit()
        first = import_files(db, [("page.png", png())], source="pixiv", source_id="123", title="来源标题", tags=["来源标签"])
        assert first.title == "手动标题"
        assert first.rating_origin == "manual"
        assert len(first.assets) == 1
        again = import_files(db, [("page2.png", png("red"))], source="pixiv", source_id="123")
        assert again.id == first.id
        assert len(again.assets) == 1
        assert {(t.tag.name, t.origin) for t in first.item_tags} == {("人工标签", "manual"), ("来源标签", "source")}


def test_invalid_image_and_oversized_archive_are_atomic(client, monkeypatch):
    assert upload(client, [("files", ("broken.png", b"not-an-image", "image/png"))]).status_code == 400
    monkeypatch.setattr(settings, "max_file_bytes", 100)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        stream.writestr("large.txt", "x" * 10000)
    response = upload(client, [("files", ("large.cbz", archive.getvalue(), "application/zip"))])
    assert response.status_code in {400, 413}
    assert client.get("/api/items").json()["total"] == 0


def test_unknown_api_never_returns_spa_html(client):
    response = client.get("/api/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"


def test_cancelled_import_guard_does_not_commit_item(client):
    from app.integrations.base import JobCancelled

    def cancelled(_db):
        raise JobCancelled()

    with SessionLocal() as db:
        with pytest.raises(JobCancelled):
            import_files(db, [("cancelled.png", png())], before_commit=cancelled)
        assert db.scalar(select(Item)) is None
