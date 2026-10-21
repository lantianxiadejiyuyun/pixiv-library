"""Refresh synthetic Rust fixtures from the Python implementation; never open live data.

Run with the existing server development environment. All LIBRARY_* values are
discarded before application imports. Only this process's TemporaryDirectory is
permitted by the SQLAlchemy connection guard.
"""
from pathlib import Path
import base64
import json
import os
import re
import sys
import tempfile
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1] / "contracts"


def main():
    with ExitStack() as stack:
        temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="qiye-rust-contract-"))
        temp = Path(temporary).resolve()
        for name in list(os.environ):
            if name.startswith("LIBRARY_"):
                del os.environ[name]
        os.environ.update({
            "LIBRARY_DATABASE_URL": "sqlite:///" + (temp / "contract.db").as_posix(),
            "LIBRARY_STORAGE_ROOT": str(temp / "storage"),
            "LIBRARY_IMPORT_ROOT": str(temp / "import"),
            "LIBRARY_ENV": "test", "LIBRARY_ADMIN_PASSWORD": "local-library",
            "LIBRARY_SECRET_KEY": "isolated-rust-contract-fixture-not-production",
            "LIBRARY_WORKER_ENABLED": "false", "LIBRARY_MANAGED_PROXY_RUNTIME": "false",
            "LIBRARY_STORAGE_REQUIRE_MARKER": "false",
        })
        sys.path.insert(0, str(ROOT))
        from sqlalchemy import event
        from sqlalchemy.engine import Dialect

        @event.listens_for(Dialect, "do_connect")
        def guard(dialect, _record, args, _kwargs):
            if dialect.name != "sqlite" or not args or not Path(args[0]).resolve().is_relative_to(temp):
                raise RuntimeError("Rust contract export forbids external database connections")

        from datetime import datetime, timedelta
        from app.main import app
        from app.database import Base, engine, SessionLocal
        stack.callback(engine.dispose)
        from app.models import Item, Asset, Tag, ItemTag, ReadingProgress, FavoriteGroup, ItemFavorite, FavoriteGroupItem, ItemVisit, Series, SeriesEntry, Job, QueueControl
        from app.storage import initialize_storage
        from fastapi.testclient import TestClient

        initialize_storage()
        Base.metadata.create_all(engine)
        moment = datetime(2025, 1, 2, 3, 4, 5)
        text = "[chapter:第一章]甲\n\n乙[uploadedimage:1][newpage][chapter:第二章]丙[pixivimage:123-2]"
        import io
        from PIL import Image
        image_stream = io.BytesIO()
        Image.new("RGB", (8, 12), (70, 100, 140)).save(image_stream, "PNG")
        png = image_stream.getvalue()
        blobs = {"fixture/page.png": png, "fixture/novel.txt": text.encode(), "fixture/video.mp4": b"synthetic-video"}
        for name, data in blobs.items():
            path = temp / "storage" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with SessionLocal() as db:
            entries = [
                Item(id="item-1", title="原题", title_zh="中文标题", title_zh_source="原题", type="manga", source="pixiv", source_id="123", author="作者甲", author_source_id="77", remote_page_count=2, source_bookmark_count=120, source_published_at=moment, content_state="ready", created_at=moment, updated_at=moment),
                Item(id="item-2", title="100%_测试", type="illustration", source="pixiv", source_id="124", author="作者乙", remote_page_count=8, description="摘要", content_state="metadata", created_at=moment + timedelta(seconds=1), updated_at=moment),
                Item(id="item-3", title="小说", type="novel", source="pixiv", source_id="novel:9", author="作者甲", author_source_id="77", created_at=moment + timedelta(seconds=2), updated_at=moment),
                Item(id="item-4", title="Missing", type="article", source="kemono", author="Creator", created_at=moment + timedelta(seconds=3), updated_at=moment),
            ]
            db.add_all(entries)
            db.flush()
            for ident, item, position, relative, mime, inline, status in [
                ("asset-1", "item-1", 0, "fixture/page.png", "image/png", None, "available"),
                ("asset-2", "item-1", 1, "fixture/page.png", "image/png", None, "available"),
                ("asset-3", "item-3", 0, "fixture/novel.txt", "text/plain", None, "available"),
                ("asset-4", "item-3", 1, "fixture/page.png", "image/png", "uploadedimage:1", "available"),
                ("asset-5", "item-4", 0, "fixture/video.mp4", "video/mp4", None, "missing"),
            ]:
                import hashlib
                db.add(Asset(id=ident, item_id=item, position=position, relative_path=relative, original_name=relative.split("/")[-1], mime_type=mime, inline_key=inline, status=status, size_bytes=len(blobs[relative]), sha256=hashlib.sha256(blobs[relative]).hexdigest(), width=1 if mime.startswith("image/") else None, height=1 if mime.startswith("image/") else None))
            db.add_all([Tag(id="tag-a", name="alpha", name_zh="标签甲"), Tag(id="tag-b", name="beta"), Tag(id="tag-c", name="100%_literal"), Tag(id="tag-unused", name="unused")])
            db.flush()
            db.add_all([ItemTag(item_id="item-1", tag_id="tag-a", origin="manual"), ItemTag(item_id="item-1", tag_id="tag-a", origin="source"), ItemTag(item_id="item-1", tag_id="tag-b", origin="source"), ItemTag(item_id="item-2", tag_id="tag-c", origin="source"), ItemTag(item_id="item-3", tag_id="tag-a", origin="source")])
            db.add_all([FavoriteGroup(id="group-1", name="想看", created_at=moment, updated_at=moment), FavoriteGroup(id="group-2", name="已看", created_at=moment, updated_at=moment)])
            db.add(ItemFavorite(item_id="item-1", created_at=moment))
            db.flush()
            db.add(FavoriteGroupItem(group_id="group-1", item_id="item-1"))
            db.add(ItemVisit(item_id="item-3", viewed_at=moment))
            db.add(ReadingProgress(item_id="item-3", page=0, section=1, offset=0.4, updated_at=moment))
            db.add_all([
                Series(id="series-1", title="上下集", origin="manual", source="local", kind="manga", complete=True, revision=1, created_at=moment, updated_at=moment),
                Series(id="series-2", title="官方小说", origin="source", source="pixiv", source_id="99", kind="novel", complete=False, revision=2, created_at=moment, updated_at=moment),
            ])
            db.flush()
            db.add_all([SeriesEntry(series_id="series-1", item_id="item-1", position=0, label="上"), SeriesEntry(series_id="series-1", item_id="item-3", position=1, label="下"), SeriesEntry(series_id="series-2", item_id="item-3", position=0, label="")])
            db.add(QueueControl(id=1, concurrency=2, created_at=moment, updated_at=moment))
            db.add_all([
                Job(id="job-1", kind="import", status="queued", payload={"platform": "pixiv", "remote_id": "123", "download": False, "item_id": "item-1"}, created_at=moment, updated_at=moment),
                Job(id="job-2", kind="import_content", status="running", payload={"platform": "pixiv", "remote_id": "124", "download_origin": "manual"}, created_at=moment, updated_at=moment),
                Job(id="job-3", kind="import", status="failed", payload={"platform": "pixiv", "remote_id": "125", "retry_queue": True, "retry_last_error": "synthetic failure"}, error="synthetic failure", created_at=moment, updated_at=moment),
            ])
            db.commit()
        routes = []
        for path, operations in app.openapi()["paths"].items():
            for method in operations:
                if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
                    routes.append({"method": method.upper(), "path": path, "example_path": re.sub(r"\{[^}]+\}", "fixture", path)})
        routes.append({"method": "HEAD", "path": "/api/assets/{asset_id}/content", "example_path": "/api/assets/fixture/content"})
        routes.sort(key=lambda r: (r["path"], r["method"]))
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "routes.json").write_text(json.dumps(routes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with engine.connect() as conn:
            dump = "\n".join("\n".join(line.rstrip() for line in sql.splitlines()) if sql.startswith("CREATE ") else sql for sql in conn.connection.driver_connection.iterdump())
            mysql_seed = []
            from sqlalchemy import select
            from sqlalchemy.schema import CreateTable, CreateIndex
            from sqlalchemy.dialects import mysql, sqlite
            mysql_schema = []
            sqlite_schema = []
            for table in Base.metadata.sorted_tables:
                mysql_schema.append(str(CreateTable(table).compile(dialect=mysql.dialect())))
                mysql_schema.extend(str(CreateIndex(index).compile(dialect=mysql.dialect())) for index in sorted(table.indexes, key=lambda index: index.name))
                sqlite_schema.append(str(CreateTable(table).compile(dialect=sqlite.dialect())))
                sqlite_schema.extend(str(CreateIndex(index).compile(dialect=sqlite.dialect())) for index in sorted(table.indexes, key=lambda index: index.name))
                columns = [column for column in table.columns if column.computed is None]
                for row in conn.execute(select(*columns)).all():
                    mysql_seed.append({"table": table.name, "columns": [c.name for c in columns], "values": [str(v) if isinstance(v, datetime) else v for v in row]})
        (OUT / "mysql-schema.json").write_text(json.dumps(mysql_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (OUT / "sqlite-schema.json").write_text(json.dumps(sqlite_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (OUT / "mysql-seed.json").write_text(json.dumps(mysql_seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        from app.integrations.credentials import _cipher
        plain = {"refresh_token": "synthetic-test-only-token", "user_id": "123", "label": "合成测试"}
        encrypted = _cipher()._encrypt_from_parts(json.dumps(plain, ensure_ascii=False).encode(), 1600000000, b"\x01" * 16).decode()
        (OUT / "legacy-fernet.json").write_text(json.dumps({"plain": plain, "encrypted": encrypted}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (OUT / "fixture.txt").write_text(dump + "\n", encoding="utf-8")
        (OUT / "blobs.json").write_text(json.dumps({k: base64.b64encode(v).decode() for k, v in blobs.items()}, indent=2) + "\n", encoding="utf-8")
        client = TestClient(app)
        assert client.post("/api/auth/login", json={"password": "local-library"}).status_code == 200
        catalog = client.get("/api/mobile-admin/catalog")
        assert catalog.status_code == 200
        (OUT / "mobile-catalog.json").write_text(json.dumps(catalog.json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths = [
            "/api/dashboard", "/api/items", "/api/items?page_size=2&page=2", "/api/items?sort=title", "/api/items?sort=oldest",
            "/api/items?q=中文", "/api/items?q=100%25_", "/api/items?author=作者甲", "/api/items?source=pixiv&type=novel",
            "/api/items?tag_ids=tag-a,tag-b", "/api/items?tag_ids=tag-a,tag-b&tag_mode=any", "/api/items?tag_ids=tag-a,tag-a",
            "/api/items?tag=标签甲", "/api/items?tag=alpha", "/api/items?tag_ids=not-found", "/api/items?downloaded=true",
            "/api/items?favorite=true", "/api/items?favorite_group=group-1", "/api/items?favorite_group=ungrouped", "/api/items?history=true",
            "/api/items/item-1", "/api/items/item-2", "/api/items/item-3", "/api/items/item-4",
            "/api/tags", "/api/tags?limit=2&offset=1", "/api/tags?suggest=true&q=标签", "/api/tags?q=%25_", "/api/tags?source=pixiv",
            "/api/favorite-groups", "/api/library/collections", "/api/assets/asset-3/text-sections",
            "/api/series/series-1", "/api/series/series-2", "/api/queue/settings", "/api/jobs", "/api/jobs?queue=retry", "/api/jobs/job-2",
        ]
        snapshots = []
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, (path, response.status_code)
            snapshots.append({"path": path, "body": response.json()})
        (OUT / "responses.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Exported {len(routes)} legacy routes, {len(snapshots)} synthetic responses; no live data used")
        engine.dispose()


if __name__ == "__main__":
    main()
