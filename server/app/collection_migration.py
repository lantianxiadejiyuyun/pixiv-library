"""Verified, non-destructive migration from shared blobs to source collections.

Run with ``python -m app.collection_migration`` to check; add ``--apply`` to
copy and update references. Each item commits separately. Original files remain.
"""
from __future__ import annotations

import argparse
import json
import os

from sqlalchemy import select

from .models import Asset, Item
from .storage import (COLLECTIONS, assert_storage_available, collection_blob_path,
                      copy_blob_to_collection, resolve_asset_path, validate_collection, verify_blob)


def migrate_item_collection(session_factory, item_id: str, *, apply: bool = False) -> dict:
    """Use a new transaction per item; never commit a partial set of references."""
    with session_factory() as db, db.begin():
        query = select(Item.id, Item.source).where(Item.id == item_id)
        if apply:
            query = query.with_for_update()
        item = db.execute(query).one_or_none()
        if item is None:
            raise ValueError("作品不存在，可能已移除")
        validate_collection(item.source)
        query = select(Asset).where(Asset.item_id == item_id).order_by(Asset.position)
        if apply:
            query = query.with_for_update()
        assets = db.scalars(query).all()
        plan, copied_targets = [], set()
        copy_bytes = 0
        for asset in assets:
            relative = collection_blob_path(item.source, asset.sha256)
            original = resolve_asset_path(asset.relative_path)
            verify_blob(original, asset.sha256, asset.size_bytes)
            target = resolve_asset_path(relative)
            if target.exists():
                verify_blob(target, asset.sha256, asset.size_bytes)
            elif relative not in copied_targets:
                copy_bytes += asset.size_bytes
                copied_targets.add(relative)
            if asset.relative_path != relative:
                plan.append((asset, relative))
        if apply:
            # First finish all filesystem work, then update every reference in one
            # transaction. An interruption may leave unused copies, never lost data.
            for asset, _relative in plan:
                copy_blob_to_collection(asset.relative_path, source=item.source,
                    digest=asset.sha256, size=asset.size_bytes)
            assert_storage_available()
            for asset, relative in plan:
                asset.relative_path = relative
                asset.status = "available"
            db.flush()
        return {"item_id": item_id, "source": item.source, "asset_count": len(assets),
                "changed_asset_count": len(plan), "copy_bytes": copy_bytes,
                "mode": "applied" if apply else "check"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Copy verified blobs and commit each item's paths; retain old files")
    parser.add_argument("--source", choices=list(COLLECTIONS))
    parser.add_argument("--item-id")
    args = parser.parse_args(argv)
    # Unlike the server's convenient development defaults, a migration must name
    # its database explicitly. Never guess which existing database to modify.
    if not os.environ.get("LIBRARY_DATABASE_URL"):
        parser.error("Set LIBRARY_DATABASE_URL explicitly before checking or applying a migration")
    from .database import SessionLocal

    assert_storage_available()
    failures = changed = checked = 0
    cursor = ""
    while True:
        with SessionLocal() as db:
            query = select(Item.id).where(Item.id > cursor).order_by(Item.id).limit(200)
            if args.source:
                query = query.where(Item.source == args.source)
            if args.item_id:
                query = query.where(Item.id == args.item_id)
            ids = db.scalars(query).all()
        if not ids:
            break
        for item_id in ids:
            try:
                result = migrate_item_collection(SessionLocal, item_id, apply=args.apply)
                checked += 1
                changed += result["changed_asset_count"]
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except (OSError, ValueError) as exc:
                failures += 1
                print(json.dumps({"item_id": item_id, "error": str(exc), "references_unchanged": True}, ensure_ascii=False), flush=True)
        cursor = ids[-1]
    if args.item_id and not checked and not failures:
        failures += 1
        print(json.dumps({"item_id": args.item_id, "error": "没有找到匹配的作品"}, ensure_ascii=False))
    print(json.dumps({"mode": "applied" if args.apply else "check", "checked_items": checked,
        "changed_asset_count": changed, "failed_items": failures, "original_files_deleted": 0}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
