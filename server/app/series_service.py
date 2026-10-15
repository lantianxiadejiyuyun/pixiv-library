"""Ordered cross-work series; body assets and per-work progress stay independent."""
from sqlalchemy import and_, or_, select

from .models import Item, Job, Series, SeriesEntry
from .integrations.base import ConnectorError
from .library import build_item_view
from . import queue_control

MAX_SERIES_ENTRIES = 2000


def summary(series):
    return {key: getattr(series, key) for key in
            ("id", "title", "origin", "source", "source_id", "kind", "complete", "revision")}


def item_series(db, item_id):
    records = db.scalars(select(Series).join(SeriesEntry).where(SeriesEntry.item_id == item_id)
                         .order_by(Series.origin, Series.title, Series.id)).all()
    return [summary(series) for series in records]


def detail(db, series):
    result = summary(series)
    result["entries"] = []
    for entry, item in db.execute(select(SeriesEntry, Item).join(Item, Item.id == SeriesEntry.item_id)
                                  .where(SeriesEntry.series_id == series.id).order_by(SeriesEntry.position)):
        view = build_item_view(item)
        result["entries"].append({"item_id": item.id, "title": item.title, "position": entry.position,
                                   "label": entry.label, "content_state": view["content_state"], "page_count": view["page_count"]})
    return result


def replace_entries(db, series, entries):
    # Delete first to avoid the (series_id,position) unique constraint during swaps.
    series.entries.clear()
    db.flush()
    series.entries.extend(SeriesEntry(item_id=item_id, position=position, label=label)
                          for position, (item_id, label) in enumerate(entries))
    db.flush()


def enqueue_series(db, payload):
    queue_control.lock_control(db)
    existing = db.scalar(select(Job).where(Job.kind == "series_sync", Job.status.in_(("queued", "running")),
        Job.payload["remote_id"].as_string() == payload["remote_id"],
        Job.payload["series_kind"].as_string() == payload["series_kind"]).with_for_update())
    if existing:
        db.commit()
        return existing
    job = Job(kind="series_sync", status="queued", payload=payload)
    db.add(job)
    db.commit()
    return job


def sync_series(db, payload, report, guard):
    from .jobs import get_connector, account_credentials, merge_remote_item, enqueue_import
    connector = get_connector("pixiv", account_credentials(db, "pixiv", payload.get("check_account_id") or payload.get("execution_account_id") or payload.get("account_id"),
                             purpose="checks", pinned=bool(payload.get("check_account_id"))))
    membership = select(SeriesEntry.item_id).join(Series, Series.id == SeriesEntry.series_id).where(
        Series.source == "pixiv", Series.source_id == payload["remote_id"], Series.kind == payload["series_kind"])
    known_members = dict(db.execute(select(Item.source_id, Item.id).where(Item.source == "pixiv", or_(
        Item.id.in_(membership), and_(Item.source_series["remote_id"].as_string() == payload["remote_id"],
                                     Item.source_series["kind"].as_string() == payload["series_kind"]))))
        .all())
    db.rollback()
    catalog = connector.fetch_series(payload["remote_id"], kind=payload["series_kind"], checkpoint=report)
    report(90)
    db.rollback()
    works = catalog.get("works")
    if (catalog.get("remote_id") != payload["remote_id"] or catalog.get("kind") != payload["series_kind"]
            or not isinstance(catalog.get("title"), str) or not catalog["title"].strip()
            or len(catalog["title"]) > 512 or not isinstance(works, list) or len(works) > MAX_SERIES_ENTRIES):
        raise ConnectorError("Pixiv 系列目录格式不完整或超过 2000 篇限制")
    if len({work.remote_id for work in works}) != len(works):
        raise ConnectorError("Pixiv 系列目录含重复作品，请稍后重新获取")
    queue_control.lock_control(db)
    series = db.scalar(select(Series).where(Series.source == "pixiv", Series.source_id == payload["remote_id"],
        Series.kind == payload["series_kind"]).with_for_update().execution_options(populate_existing=True))
    if series is None:
        series = Series(origin="source", source="pixiv", source_id=payload["remote_id"],
                        kind=payload["series_kind"], title=catalog["title"], complete=False, revision=0)
        db.add(series)
        db.flush()
    # No locks are kept across the remote request. A current locking read now
    # gives a concurrent user's deletion precedence over the older request.
    alive_ids = set(db.scalars(select(Item.id).where(Item.id.in_(list(known_members.values()))).with_for_update())) if known_members else set()
    deleted_members = {remote_id for remote_id, item_id in known_members.items() if item_id not in alive_ids}
    entries, queued = [], []
    for work in works:
        if work.remote_id in deleted_members:
            continue
        # Directory membership is authoritative, regardless of whether the
        # individual summary included its own series field.
        work.source_series = {"remote_id": payload["remote_id"], "title": catalog["title"], "kind": payload["series_kind"]}
        item = merge_remote_item(db, "pixiv", work)
        entries.append((item.id, ""))
        if not item.assets and not item.preview_cover:
            job = enqueue_import(db, {"platform": "pixiv", "remote_id": work.remote_id,
                "item_id": item.id, "account_id": payload.get("account_id"), "download": False}, commit=False, locked=True)
            queued.append(job.id)
    complete = catalog.get("complete") is True and not deleted_members
    # An interrupted/limited refresh must not erase an already saved directory.
    directory_updated = complete or not series.entries
    if directory_updated:
        replace_entries(db, series, entries)
        series.title, series.complete = catalog["title"], complete
        series.revision += 1
    if deleted_members:
        series.complete = False
        if not directory_updated:
            series.revision += 1
    guard(db)
    db.commit()
    return {"series_id": series.id, "entry_count": len(series.entries), "complete": series.complete,
            "directory_updated": directory_updated, "queued_jobs": queued, "skipped_deleted": len(deleted_members),
            "message": ("读取目录期间有作品被删除，已跳过；目录标记为不完整，未下载正文" if deleted_members else
                        "系列目录已保存，缺失封面已排队；未下载正文" if directory_updated else
                        "远端仅返回部分目录，已保留已有目录；缺失封面已排队，未下载正文")}
