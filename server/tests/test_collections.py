import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from test_library import client, png
from app import collection_migration
from app.collection_migration import migrate_item_collection
from app.database import SessionLocal
from app.library import import_files, set_tags
from app.models import Asset, Item, ReadingProgress
from app.storage import COLLECTIONS, collection_blob_path, resolve_asset_path, save_blob


def legacy_item(db, source="pixiv", colors=("white",)):
    """Create historical records only inside the conftest-guarded temporary DB."""
    item = Item(title="保留标题", source=source, source_id="legacy-" + source,
                type="manga", rating="adult", rating_origin="manual")
    for index, color in enumerate(colors):
        content = png(color)
        digest = hashlib.sha256(content).hexdigest()
        relative = f"blobs/{digest[:2]}/{digest[2:4]}/{digest}"
        path = resolve_asset_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        item.assets.append(Asset(position=index, relative_path=relative,
            original_name=f"{index + 1}.png", mime_type="image/png", sha256=digest,
            size_bytes=len(content), status="available"))
    item.progress = ReadingProgress(page=0)
    db.add(item)
    db.flush()
    set_tags(db, item, ["手动保留"], "manual")
    db.commit()
    return item


def test_same_source_deduplicates_but_each_collection_has_separate_files(client):
    paths = []
    with SessionLocal() as db:
        for source in COLLECTIONS:
            one = import_files(db, [("same.png", png())], source=source)
            two = import_files(db, [("same.png", png())], source=source)
            first, second = one.assets[0], two.assets[0]
            assert first.relative_path == second.relative_path
            assert first.relative_path.startswith(f"collections/{source}/blobs/")
            paths.append(resolve_asset_path(first.relative_path))
    assert len(set(paths)) == len(COLLECTIONS)
    assert all(path.read_bytes() == png() for path in paths)
    assert not any(paths[0].samefile(path) for path in paths[1:])
    with pytest.raises(ValueError, match="来源"):
        save_blob(png(), source="../pixiv")


def test_migration_checks_first_preserves_metadata_and_originals_and_is_repeatable(client):
    with SessionLocal() as db:
        item = legacy_item(db, colors=("white", "white"))
        item_id = item.id
        asset_ids = [asset.id for asset in item.assets]
        original = item.assets[0].relative_path
        target = collection_blob_path("pixiv", item.assets[0].sha256)
    checked = migrate_item_collection(SessionLocal, item_id)
    assert checked["changed_asset_count"] == 2
    assert checked["copy_bytes"] == len(png())  # Duplicate pages copy once.
    assert not resolve_asset_path(target).exists()
    assert client.get(f"/api/assets/{asset_ids[0]}/content").content == png()
    result = migrate_item_collection(SessionLocal, item_id, apply=True)
    assert result["changed_asset_count"] == 2
    assert resolve_asset_path(original).read_bytes() == png()
    assert resolve_asset_path(target).read_bytes() == png()
    assert not resolve_asset_path(original).samefile(resolve_asset_path(target))
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        assert item.title == "保留标题"
        assert item.rating == "adult" and item.rating_origin == "manual"
        assert [(link.tag.name, link.origin) for link in item.item_tags] == [("手动保留", "manual")]
        assert item.progress.page == 0
        assert all(asset.relative_path == target for asset in item.assets)
    assert client.get(f"/api/assets/{asset_ids[0]}/content").content == png()
    assert client.get(f"/api/items/{item_id}/export").status_code == 200
    assert migrate_item_collection(SessionLocal, item_id, apply=True)["changed_asset_count"] == 0


def test_interrupted_copy_leaves_all_references_and_originals_then_resumes(client, monkeypatch):
    with SessionLocal() as db:
        item = legacy_item(db, "ehentai", ("white", "red"))
        item_id = item.id
        originals = [asset.relative_path for asset in item.assets]
    actual_copy = collection_migration.copy_blob_to_collection
    calls = []

    def interrupted_copy(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 2:
            raise OSError("simulated NAS disconnect")
        return actual_copy(*args, **kwargs)

    monkeypatch.setattr(collection_migration, "copy_blob_to_collection", interrupted_copy)
    with pytest.raises(OSError, match="disconnect"):
        migrate_item_collection(SessionLocal, item_id, apply=True)
    with SessionLocal() as db:
        assert [a.relative_path for a in db.get(Item, item_id).assets] == originals
    assert all(resolve_asset_path(path).is_file() for path in originals)
    monkeypatch.setattr(collection_migration, "copy_blob_to_collection", actual_copy)
    assert migrate_item_collection(SessionLocal, item_id, apply=True)["changed_asset_count"] == 2


@pytest.mark.parametrize("corrupt_target", [False, True])
def test_hash_mismatch_rejects_migration_without_changing_references(client, corrupt_target):
    with SessionLocal() as db:
        item = legacy_item(db, "jm")
        item_id = item.id
        asset = item.assets[0]
        original = asset.relative_path
        relative = collection_blob_path("jm", asset.sha256) if corrupt_target else original
        path = resolve_asset_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * asset.size_bytes)
    with pytest.raises(OSError, match="完整性"):
        migrate_item_collection(SessionLocal, item_id, apply=True)
    with SessionLocal() as db:
        assert db.get(Item, item_id).assets[0].relative_path == original
    assert path.read_bytes() == b"x" * len(png())  # No silent overwrite.


def test_collection_overview_counts_legacy_and_metadata_and_requires_auth(client):
    with SessionLocal() as db:
        item = legacy_item(db)
        item_id = item.id
        db.add(Item(title="仅元数据", source="pixiv", source_id="meta", type="manga"))
        import_files(db, [("local.png", png())])
    rows = {row["source"]: row for row in client.get("/api/library/collections").json()}
    assert set(rows) == set(COLLECTIONS)
    assert rows["pixiv"] == {"source": "pixiv", "name": "Pixiv", "storage_directory": "collections/pixiv",
        "item_count": 2, "page_count": 1, "size_bytes": len(png()), "saved_item_count": 1,
        "metadata_item_count": 1, "legacy_asset_count": 1}
    assert rows["local"]["item_count"] == 1
    assert rows["jm"]["item_count"] == 0
    migrate_item_collection(SessionLocal, item_id, apply=True)
    rows = {row["source"]: row for row in client.get("/api/library/collections").json()}
    assert rows["pixiv"]["legacy_asset_count"] == 0
    client.post("/api/auth/logout")
    assert client.get("/api/library/collections").status_code == 401


def test_migration_cli_defaults_to_read_only_check(client, capsys):
    with SessionLocal() as db:
        item_id = legacy_item(db, "picacg").id
    assert collection_migration.main(["--source", "picacg"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["mode"] == "check"
    assert lines[-1]["changed_asset_count"] == 1
    with SessionLocal() as db:
        assert db.get(Item, item_id).assets[0].relative_path.startswith("blobs/")


def test_backup_includes_both_retained_legacy_and_new_collection_files(client, monkeypatch, tmp_path):
    from app.config import settings
    import sys
    import zipfile

    with SessionLocal() as db:
        old = legacy_item(db).assets[0].relative_path
        new = import_files(db, [("offline.png", png())], source="jm").assets[0].relative_path
    filename = Path(__file__).resolve().parents[2] / "scripts" / "backup.py"
    spec = importlib.util.spec_from_file_location("collection_backup_test", filename)
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)

    def fake_dump(_command, *, cwd, stdout, check):
        stdout.write(b"-- isolated test backup; no real database process invoked\n")

    monkeypatch.setattr(backup.subprocess, "run", fake_dump)
    archive = tmp_path / "saved-backup.zip"
    monkeypatch.setattr(sys, "argv", ["backup.py", "--storage", str(settings.storage_root), "--output", str(archive)])
    backup.main()
    with zipfile.ZipFile(archive) as stored:
        assert stored.read("storage/" + old) == png()
        assert stored.read("storage/" + new) == png()
        manifest = json.loads(stored.read("manifest.json"))
        assert {"storage/" + old, "storage/" + new} <= {entry["name"] for entry in manifest["files"]}
