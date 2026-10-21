"""Persistent queue claims and task execution shared by isolated workers. Lease-based claiming permits safe restart recovery.

Lifecycle functions start_worker / stop_worker are synchronous and non-blocking
except for the bounded (2 second) shutdown join. Cancellation is cooperative.
"""
from __future__ import annotations
from collections import deque
from datetime import timedelta
import json
import logging
import os
import re
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from threading import Event, Lock, Thread
import uuid

from sqlalchemy import func, or_, select, update
from app.database import SessionLocal
from app.models import Item, ItemTag, Job, SourceAccount, Subscription, Tag, utcnow
from app.integrations import get_connector
from app.integrations.base import ConnectorError, JobCancelled
from app.integrations.credentials import decrypt_credentials
from app import queue_control


log = logging.getLogger(__name__)
_stop = Event()
_threads = []
_lifecycle_lock = Lock()
_worker_id = str(uuid.uuid4())
_lease_owner = ContextVar("job_lease_owner", default=None)
LEASE_SECONDS = 300
DIAGNOSTIC_FRAME_LIMIT = 8


def current_worker_id():
    # A new owner for every claim fences an expired attempt even in this same
    # process. The fallback keeps explicitly claimed jobs usable by tools/tests.
    return _lease_owner.get() or _worker_id


def _exception_type(exc):
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"


def _record_internal_failure(job_id, kind, exc):
    """Log code locations only, never format an exception or capture its locals.

    Standard exception/traceback formatting can include HTTP URLs, SQL, bound
    parameters and source lines. Walk the traceback's code metadata directly.
    Keep the innermost frames so deep SDK stacks still identify the failure.
    """
    try:
        opaque_id = str(uuid.UUID(job_id))
    except (ValueError, TypeError, AttributeError):
        opaque_id = "unknown"
    safe_kind = kind if kind in {"import", "import_content", "source_sync", "series_sync", "ai", "translate", "author_profile", "author_feed", "pixiv_bookmark"} else "unknown"
    frames = deque(maxlen=DIAGNOSTIC_FRAME_LIMIT)
    trace = exc.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        # Generated code may supply arbitrary filenames. Accept source basenames
        # only; never echo a URL, query string or full local path as a filename.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}\.py", filename):
            filename = "<generated>"
        function = code.co_name
        if function not in {"<module>", "<lambda>"} and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,95}", function):
            function = "<function>"
        frames.append({"file": filename, "function": function, "line": trace.tb_lineno})
        trace = trace.tb_next
    log.error("Background job internal failure: job_id=%s kind=%s exception_type=%s frames=%s",
              opaque_id, safe_kind, _exception_type(exc), json.dumps(list(frames), ensure_ascii=True))


def job_view(job):
    return {"id": job.id, "kind": job.kind, "queue": queue_control.job_lane(job), "original_queue": queue_control.original_lane(job), "status": job.status, "progress": job.progress,
            "error": job.error, "retry_last_error": job.payload.get("retry_last_error"), "created_at": job.created_at.isoformat() + "Z",
            "updated_at": job.updated_at.isoformat() + "Z", "result": job.result,
            "item_id": job.payload.get("item_id") or (job.result or {}).get("item_id"),
            "label": "Pixiv 追更列表同步" if job.payload.get("watchlist") else "Pixiv 主账号收藏检查" if job.kind == "pixiv_bookmark" else "Pixiv 详情与正文下载" if bundled_pixiv(job) and not (job.status == "completed" and (job.result or {}).get("metadata_only") is True) else None}


def bundled_pixiv(job):
    return job.kind == "import" and job.payload.get("platform") == "pixiv" and job.payload.get("download", True)


def enqueue(db, kind, payload):
    job = Job(kind=kind, status="queued", payload=payload)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue_import(db, payload, *, commit=True, locked=False):
    """Idempotently queue a cover phase; callers may explicitly request body."""
    if not locked:
        queue_control.lock_control(db)
    platform, remote_id = payload["platform"], payload["remote_id"]
    active = db.scalars(select(Job).where(Job.kind.in_(("import", "import_content")),
        Job.status.in_(("queued", "running")), Job.payload["platform"].as_string() == platform,
        Job.payload["remote_id"].as_string() == remote_id).order_by(Job.created_at).with_for_update().execution_options(populate_existing=True)).all()
    requested_body = payload.get("download", True)
    chosen = None
    for candidate in active:
        if candidate.kind == "import_content" or candidate.payload.get("download", True) or not requested_body:
            chosen = candidate
            break
        if candidate.status == "queued":
            candidate.payload = {**candidate.payload, "download": True,
                                 "account_id": payload.get("account_id") or candidate.payload.get("account_id")}
            chosen = candidate
            break
    if chosen is None:
        chosen = Job(kind="import", status="queued", payload=payload)
        db.add(chosen)
        db.flush()
    if isinstance(payload.get("pixiv_bookmark_state"), dict):
        chosen.payload = {**chosen.payload, "pixiv_bookmark_state": payload["pixiv_bookmark_state"]}
    if payload.get("download_origin") == "manual" and requested_body and chosen.status == "queued":
        # Explicit manual requests promote waiting automatic work, while an
        # already running body retains its identity and is not downloaded twice.
        chosen.payload = {**{key: value for key, value in chosen.payload.items()
                            if key not in ("download_account_id", "details_account_id", "execution_account_id")},
                          "download_origin": "manual"}
    elif payload.get("download_origin") == "manual" and requested_body and chosen.kind == "import" and platform != "pixiv":
        # Other platforms' running first stage has not started the body yet.
        # Let the follow-up body inherit the explicit manual request.
        chosen.payload = {**chosen.payload, "download_origin": "manual"}
    item = db.scalar(select(Item).where(Item.source == platform, Item.source_id == remote_id).with_for_update().execution_options(populate_existing=True))
    if item is not None:
        if not chosen.payload.get("item_id"):
            chosen.payload = {**chosen.payload, "item_id": item.id}
        if requested_body and not item.assets:
            item.content_state = "downloading" if chosen.status == "running" and (chosen.kind == "import_content" or bundled_pixiv(chosen)) else "queued"
            item.content_error, item.content_job_id = None, chosen.id
    if commit:
        db.commit()
        db.refresh(chosen)
    return chosen


def account_credentials(db, platform, account_id=None, *, purpose="metadata", pinned=False, require_retry=True):
    if platform == "pixiv":
        from .pixiv_accounts import resolve_account_id
        account_id = resolve_account_id(db, account_id, purpose=purpose, pinned=pinned, require_retry=require_retry)
    if account_id:
        account = db.get(SourceAccount, account_id)
        if not account or account.platform != platform:
            raise ConnectorError("账号不存在或与内容平台不匹配")
    else:
        account = db.scalar(select(SourceAccount).where(SourceAccount.platform == platform).order_by(SourceAccount.created_at))
    return decrypt_credentials(account.credentials_encrypted) if account else {}


def pixiv_account_label(db, payload, *, content=False):
    """Resolve the actual identity while the initial short read txn is open."""
    from .pixiv_accounts import resolve_account_id
    field, purpose = pixiv_body_role(payload) if content else ("details_account_id", "details")
    requested = payload.get(field) or payload.get("execution_account_id") or payload.get("account_id")
    ident = resolve_account_id(db, requested, purpose=purpose, pinned=bool(payload.get(field)))
    account = db.get(SourceAccount, ident) if ident else db.scalar(select(SourceAccount).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at))
    label = " ".join(account.label.split())[:80] if account else ""
    stage = "手动下载" if purpose == "manual" else "图片下载" if purpose == "content" else "作品详情与正文"
    return f"Pixiv {stage}账号" + (f"「{label}」" if label else "")


def pixiv_body_role(payload):
    from .pixiv_accounts import is_novel_payload
    if payload.get("download_origin") == "manual":
        return "download_account_id", "manual"
    return ("details_account_id", "details") if is_novel_payload(payload) else ("download_account_id", "content")


@contextmanager
def pixiv_stage(label):
    try:
        yield
    except ConnectorError as exc:
        raise ConnectorError(f"{label}：{exc}") from None


def checkpoint(job_id, progress):
    with SessionLocal() as db:
        if queue_control.current_lane() == "retry":
            from .pixiv_accounts import validate_retry_assignment
            job = db.get(Job, job_id)
            if job is not None:
                validate_retry_assignment(db, job)
        changed = db.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.worker_id == current_worker_id())
            .values(progress=progress, updated_at=utcnow(), lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)))
        db.commit()
        if not changed.rowcount:
            raise JobCancelled()
    if _stop.is_set():
        raise JobCancelled()


def commit_guard(db, job_id):
    """Lock and re-read ownership inside the transaction being committed."""
    if queue_control.current_lane() == "retry":
        # Same lock order as role updates. The following permission read must
        # see the current configuration, not a MySQL repeatable-read snapshot.
        queue_control.lock_control(db)
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True))
    if (not job or job.status != "running" or job.worker_id != current_worker_id()
            or job.lease_until is None or job.lease_until < utcnow() or _stop.is_set()):
        raise JobCancelled()
    queue_control.check_paused(queue_control.job_lane(job))
    if job.payload.get("retry_queue") is True:
        from .pixiv_accounts import validate_retry_assignment
        validate_retry_assignment(db, job, for_update=True)


def merge_remote_item(db, platform, work, *, bookmark_account_id=None, bookmark_hint=None):
    """Merge only source navigation fields into existing, possibly edited items."""
    item = db.scalar(select(Item).where(Item.source == platform, Item.source_id == work.remote_id)
                     .with_for_update().execution_options(populate_existing=True))
    if item is None:
        item = Item(title=work.title[:512], type=work.item_type, source=platform, source_id=work.remote_id,
                    source_url=work.source_url, author=work.author[:512], description=work.description,
                    language=work.language, rating=work.rating, rating_origin="source", content_state="metadata")
        db.add(item)
        db.flush()
        from .library import ensure_tag
        seen = set()
        for name in sorted(set(t[:191] for t in work.tags if t)):
            tag = ensure_tag(db, name)
            if tag.id in seen:
                continue
            seen.add(tag.id)
            db.add(ItemTag(item_id=item.id, tag_id=tag.id, origin="source"))
    remote_count = getattr(work, "remote_page_count", None)
    from .source_dates import source_timestamp
    published = source_timestamp(getattr(work, "source_published_at", None))
    if published is not None:
        item.source_published_at = published
    if getattr(work, "author_id", None):
        item.author_source_id = work.author_id
    if type(remote_count) is int and 0 <= remote_count <= 1000000:
        item.remote_page_count = remote_count
    bookmark_count = getattr(work, "bookmark_count", None)
    if type(bookmark_count) is int and 0 <= bookmark_count <= 9223372036854775807:
        item.source_bookmark_count = bookmark_count
    source_series = getattr(work, "source_series", None)
    if isinstance(source_series, dict) and source_series.get("kind") in ("novel", "manga"):
        ident, title = str(source_series.get("remote_id", "")), str(source_series.get("title", ""))
        if ident and len(ident) <= 255 and title and len(title) <= 512:
            item.source_series = {"remote_id": ident, "title": title, "kind": source_series["kind"]}
    if platform == "pixiv" and bookmark_account_id is not None:
        from .pixiv_favorites import observe
        observe(db, item, work, bookmark_account_id, bookmark_hint)
    return item


def _persist_metadata(db, platform, work, guard, *, preview=None, queue_payload=None, job_id=None, expected_item_id=None, cover_only_if_missing=False, bookmark_account_id=None, bookmark_hint=None):
    queue_control.lock_control(db)
    if expected_item_id and db.scalar(select(Item.id).where(Item.id == expected_item_id).with_for_update()) is None:
        db.rollback()
        return None
    item = merge_remote_item(db, platform, work, bookmark_account_id=bookmark_account_id, bookmark_hint=bookmark_hint)
    if preview is not None and not (cover_only_if_missing and item.preview_cover):
        item.preview_cover = preview
    if job_id:
        current = db.get(Job, job_id)
        if current:
            if queue_payload is not None and current.payload.get("download_origin") == "manual":
                queue_payload = {**queue_payload, "download_origin": "manual"}
            current.payload = {**current.payload, "item_id": item.id}
    if queue_payload is not None and not item.assets:
        body = db.scalar(select(Job).where(Job.kind == "import_content", Job.status.in_(("queued", "running")),
            Job.payload["item_id"].as_string() == item.id).order_by(Job.created_at).with_for_update())
        if body is None:
            body = Job(kind="import_content", status="queued", payload={**queue_payload, "item_id": item.id, "download": True})
            db.add(body)
            db.flush()
        elif body.status == "queued" and queue_payload.get("download_origin") == "manual":
            body.payload = {**body.payload, "download_origin": "manual"}
        if body.status == "queued" and queue_payload.get("retry_queue") is True:
            body.payload = {**body.payload, "retry_queue": True,
                            "retry_last_error": queue_payload.get("retry_last_error")}
        item.content_job_id, item.content_error = body.id, None
        item.content_state = "downloading" if body.status == "running" else "queued"
    guard(db)
    db.commit()
    return item


def _import(db, payload, report, guard, job_id=None):
    """Stage 1: commit metadata/preview before the independent body job exists."""
    from app.library import save_preview_cover
    platform = payload["platform"]
    if platform == "pixiv" and payload.get("download", True):
        return _import_pixiv(db, payload, report, guard, job_id)
    automatic_cover = payload.get("auto_novel_cover") is True and payload.get("download") is False
    if automatic_cover:
        current = db.execute(select(Item.id, Item.preview_cover).where(Item.id == payload.get("item_id"),
            Item.source == platform, Item.source_id == payload["remote_id"])).first()
        if current is None or current.preview_cover:
            return {"item_id": payload.get("item_id"), "skipped": True,
                    "cover_saved": bool(current and current.preview_cover), "message": "封面已存在或作品已删除，自动补全已跳过"}
    connector = get_connector(platform, account_credentials(db, platform, payload.get("details_account_id") or payload.get("execution_account_id") or payload.get("account_id"),
                              purpose="details" if platform == "pixiv" else "metadata", pinned=bool(payload.get("details_account_id"))))
    bookmark_owner = None
    if platform == "pixiv":
        from .pixiv_favorites import request_account_id
        bookmark_owner = request_account_id(db, payload.get("details_account_id") or payload.get("execution_account_id") or payload.get("account_id"),
            purpose="details", pinned=bool(payload.get("details_account_id")))
    stage_label = pixiv_account_label(db, payload) if platform == "pixiv" else None
    ident = connector.normalize_id(payload["remote_id"])
    existing = db.scalar(select(Item).where(Item.source == platform, Item.source_id == ident))
    if existing and existing.assets and not payload.get("refresh_metadata") and not (existing.type in ("novel", "article") and not existing.preview_cover):
        return {"item_id": existing.id, "existing": True, "metadata_only": False, "content_state": "ready"}
    skip_preview = platform == "pixiv" and bool(payload.get("refresh_metadata") and existing and existing.preview_cover)
    # Finish the read transaction before gate waits and platform requests.
    db.rollback()
    with pixiv_stage(stage_label) if stage_label else nullcontext():
        work = connector.fetch(ident, download=False, checkpoint=report, **({"include_preview": False} if skip_preview else {}))
    report(90)
    preview = None
    if getattr(work, "preview_cover", None):
        try:
            preview = save_preview_cover(work.preview_cover, platform)
        except (ValueError, OSError):
            # Broken/unavailable preview must not prevent body queueing. Any
            # existing valid cover is retained by the metadata merge.
            pass
    report(95)
    db.rollback()
    item = _persist_metadata(db, platform, work, guard, preview=preview,
        queue_payload={**payload, "remote_id": ident} if payload.get("download", True) else None,
        job_id=job_id, expected_item_id=payload.get("item_id"), cover_only_if_missing=automatic_cover,
        bookmark_account_id=bookmark_owner, bookmark_hint=payload.get("pixiv_bookmark_state"))
    if item is None:
        return {"skipped": True, "message": "作品已删除，封面任务已跳过"}
    return {"item_id": item.id, "metadata_only": not bool(item.assets), "content_state": item.content_state,
            "content_job_id": item.content_job_id, "cover_saved": bool(item.preview_cover),
            "message": (("封面与资料已保存" if item.preview_cover else "资料已保存，封面暂不可用") +
                        ("，正文已加入下载队列" if payload.get("download", True) else "；未排队下载正文"))}


def _import_pixiv(db, payload, report, guard, job_id):
    """One job, one final commit: Pixiv details and complete body together.

    Novel metadata and text use the work-details identity in one fetch. Images
    use the download identity (including its permission/detail request), while
    the work-details identity supplies metadata. No follow-up job is created.
    """
    from .pixiv_accounts import read_config
    from .library import import_files
    split = read_config(db)["enabled"]
    field, purpose = pixiv_body_role(payload)
    content = get_connector("pixiv", account_credentials(db, "pixiv", payload.get(field) or payload.get("execution_account_id") or payload.get("account_id"),
                            purpose=purpose, pinned=bool(payload.get(field))))
    from .pixiv_accounts import is_novel_payload
    metadata = get_connector("pixiv", account_credentials(db, "pixiv", payload.get("details_account_id") or payload.get("account_id"),
                             purpose="details", pinned=bool(payload.get("details_account_id")))) if split and not is_novel_payload(payload) else None
    if payload.get("details_account_id") and payload.get("details_account_id") == payload.get("download_account_id"):
        metadata = None  # The same identity returns both in one fetch.
    split = metadata is not None
    from .pixiv_favorites import request_account_id
    bookmark_owner = request_account_id(db,
        (payload.get("details_account_id") or payload.get("account_id")) if split else
        (payload.get(field) or payload.get("execution_account_id") or payload.get("account_id")),
        purpose="details" if split else purpose, pinned=bool(payload.get("details_account_id") if split else payload.get(field)))
    content_label = pixiv_account_label(db, payload, content=True)
    metadata_label = pixiv_account_label(db, payload) if split else None
    ident = content.normalize_id(payload["remote_id"])
    existing = db.scalar(select(Item).where(Item.source == "pixiv", Item.source_id == ident))
    if payload.get("novel_images_only"):
        if existing is None or existing.id != payload.get("item_id") or existing.type != "novel":
            return {"skipped": True, "message": "小说已删除，插图补全已跳过"}
        from .storage import resolve_asset_path
        from .novel_text import MAX_TEXT_BYTES
        texts = [asset for asset in existing.assets if asset.mime_type.startswith("text/") and asset.size_bytes <= MAX_TEXT_BYTES]
        if len(texts) != 1:
            raise ConnectorError("插图补全需要一份有效的小说正文")
        with resolve_asset_path(texts[0].relative_path).open("rb") as stream:
            raw = stream.read(MAX_TEXT_BYTES + 1)
        if len(raw) > MAX_TEXT_BYTES:
            raise ConnectorError("小说正文超过解析限制")
        text = raw.decode("utf-8-sig")
        existing_keys = {asset.inline_key for asset in existing.assets if asset.inline_key and asset.status == "available"}
        item_id = existing.id
        db.rollback()
        with pixiv_stage(content_label):
            body = content.fetch_novel_images(ident, text, checkpoint=report, existing_keys=existing_keys)
        db.rollback()
        queue_control.lock_control(db)
        from .library import append_novel_images
        added = append_novel_images(db, item_id, body.files, body.inline_images, before_commit=guard)
        return {"item_id": item_id, "illustrations_added": added, "message": "正文插图已补全，原文与阅读进度已保留"}
    if existing and existing.assets:
        return {"item_id": existing.id, "existing": True, "metadata_only": False, "content_state": "ready"}
    if payload.get("item_id") and (existing is None or existing.id != payload["item_id"]):
        return {"skipped": True, "message": "作品已删除，下载任务已跳过"}
    db.rollback()
    work = None
    if split:
        with pixiv_stage(metadata_label):
            work = metadata.fetch(ident, download=False, include_preview=False,
                                  checkpoint=lambda value: report(min(15, value)))
    with pixiv_stage(content_label):
        body = content.fetch(ident, download=True, checkpoint=lambda value: report((20 if split else 0) + int(value * (0.7 if split else 0.9))))
    work = work or body
    if work.remote_id != ident or body.remote_id != ident or body.metadata_only or not body.files:
        raise ConnectorError("Pixiv 未返回匹配作品的完整正文，未保存空作品")
    if work.item_type != "novel" and work.remote_page_count and len(body.files) != work.remote_page_count:
        raise ConnectorError("Pixiv 正文与作品页数不一致，未保存不完整作品")
    preview = None
    if body.preview_cover:
        from .library import save_preview_cover
        try:
            preview = save_preview_cover(body.preview_cover, "pixiv")
        except (ValueError, OSError):
            pass
    report(92)
    db.rollback()
    queue_control.lock_control(db)
    if payload.get("item_id") and db.scalar(select(Item.id).where(Item.id == payload["item_id"]).with_for_update()) is None:
        db.rollback()
        return {"skipped": True, "message": "作品已删除，下载任务已跳过"}
    item = merge_remote_item(db, "pixiv", work, bookmark_account_id=bookmark_owner,
        bookmark_hint=payload.get("pixiv_bookmark_state"))
    if preview is not None:
        item.preview_cover = preview
    def before_commit(session):
        guard(session)
        if job_id:
            current = session.get(Job, job_id)
            if current:
                current.payload = {**current.payload, "item_id": item.id}
        if work.rating != "unknown":
            session.execute(update(Item).where(Item.id == item.id, Item.rating_origin != "manual").values(rating=work.rating, rating_origin="source"))
    item = import_files(db, body.files, title=work.title, item_type=work.item_type, source="pixiv",
        source_id=ident, source_url=work.source_url, author=work.author, description=work.description,
        language=work.language, tags=work.tags, before_commit=before_commit, inline_images=body.inline_images)
    return {"item_id": item.id, "metadata_only": False, "content_state": "ready",
            "content_job_id": job_id, "message": "Pixiv 详情与完整正文已在同一任务中保存，可阅读"}


def _import_content(db, payload, report, guard):
    platform = payload["platform"]
    item = db.get(Item, payload.get("item_id"))
    if item is None:
        return {"skipped": True, "message": "作品已删除，正文任务已跳过"}
    from .pixiv_accounts import read_config
    split_accounts = platform == "pixiv" and read_config(db)["enabled"]
    field, purpose = pixiv_body_role(payload) if platform == "pixiv" else ("download_account_id", "content")
    connector = get_connector(platform, account_credentials(db, platform, payload.get(field) or payload.get("execution_account_id") or payload.get("account_id"),
                              purpose=purpose, pinned=bool(payload.get(field))))
    bookmark_owner = None
    if platform == "pixiv":
        from .pixiv_favorites import request_account_id
        bookmark_owner = request_account_id(db, payload.get(field) or payload.get("execution_account_id") or payload.get("account_id"),
            purpose=purpose, pinned=bool(payload.get(field)))
    stage_label = pixiv_account_label(db, payload, content=True) if platform == "pixiv" else None
    ident = connector.normalize_id(payload["remote_id"])
    existing = db.scalar(select(Item).where(Item.source == platform, Item.source_id == ident))
    if existing and existing.assets:
        return {"item_id": existing.id, "existing": True, "metadata_only": not bool(existing.assets)}
    db.rollback()
    with pixiv_stage(stage_label) if stage_label else nullcontext():
        work = connector.fetch(ident, download=True, checkpoint=report)
    report(93)
    db.rollback()
    if work.metadata_only or not work.files:
        raise ConnectorError("平台未返回任何可保存文件，未创建空作品")
    else:
        from app.library import import_files
        def source_commit(db):
            guard(db)
            if db.get(Item, payload["item_id"]) is None:
                raise JobCancelled()
            from .source_dates import source_timestamp
            published = source_timestamp(getattr(work, "source_published_at", None))
            if published is not None:
                db.execute(update(Item).where(Item.id == payload["item_id"]).values(source_published_at=published))
            if work.rating != "unknown" and not split_accounts:
                db.execute(update(Item).where(Item.source == platform, Item.source_id == work.remote_id,
                    Item.rating_origin != "manual").values(rating=work.rating, rating_origin="source"))
            if platform == "pixiv" and bookmark_owner:
                from .pixiv_favorites import observe
                observed_item = db.scalar(select(Item).where(Item.id == payload["item_id"]).with_for_update())
                observe(db, observed_item, work, bookmark_owner, payload.get("pixiv_bookmark_state"))
        item = import_files(db, work.files, title=work.title, item_type=work.item_type, source=platform,
            source_id=work.remote_id, source_url=work.source_url, author=work.author,
            description=work.description, language=work.language, tags=None if split_accounts else work.tags,
            before_commit=source_commit, checkpoint=report, inline_images=work.inline_images)
    return {"item_id": item.id, "metadata_only": work.metadata_only,
            "message": "仅保存元数据，尚未下载离线文件" if work.metadata_only else
                       ("画廊全部页面图片已保存（站点展示清晰度）" if platform == "ehentai" else "原文件已保存，可离线阅读")}


def _sync(db, payload, report, guard):
    if payload.get('pixiv_bookmark_scan') is True:
        from .pixiv_collection import scan_page
        return scan_page(db, payload, report, guard)
    if payload.get("watchlist"):
        from .watchlist import process
        return process(db, payload, report, guard)
    platform = payload["platform"]
    if platform == "pixiv":
        from .pixiv_accounts import resolve_account_id
        payload = {**payload, "account_id": resolve_account_id(db, payload.get("account_id"))}
    connector = get_connector(platform, account_credentials(db, platform, payload.get("check_account_id") or payload.get("execution_account_id") or payload.get("account_id"),
                              purpose="metadata", pinned=bool(payload.get("check_account_id"))))
    bookmark_owner = None
    if platform == "pixiv":
        from .pixiv_favorites import request_account_id
        bookmark_owner = request_account_id(db, payload.get("check_account_id") or payload.get("execution_account_id") or payload.get("account_id"),
            pinned=bool(payload.get("check_account_id")))
    db.commit()
    if payload.get("subscription_id"):
        subscription = db.get(Subscription, payload["subscription_id"])
        if not subscription or not subscription.enabled:
            return {"skipped": True, "message": "订阅已暂停或删除"}
        db.commit()
        if platform == "pixiv":
            identities, has_more = connector.discover(subscription.target_type, subscription.target,
                                                       content_scope=subscription.content_scope,
                                                       include_author_bookmarks=subscription.target_type == "author")
        else:
            identities, has_more = connector.discover(subscription.target_type, subscription.target)
        report(85)
        db.rollback()
        queue_control.lock_control(db)
        subscription = db.get(Subscription, payload["subscription_id"])
        if not subscription or not subscription.enabled:
            return {"skipped": True, "message": "订阅已暂停或删除"}
        created = []
        from .pixiv_follow_download import default_works
        author_default = default_works(db) if platform == 'pixiv' else False
        for ident in dict.fromkeys(identities):
            from .subscription_policy import wants_body
            download = wants_body(subscription, ident, getattr(connector, "discovery_origins", None), default_works=author_default)
            existing = db.scalar(select(Item).where(Item.source == platform, Item.source_id == ident).with_for_update())
            hint = None
            if platform == "pixiv":
                state = getattr(connector, "bookmark_observations", {}).get(ident)
                if type(state) is bool:
                    hint = {"account_id": bookmark_owner, "bookmarked": state}
                    if existing:
                        from .pixiv_favorites import observe
                        observe(db, existing, None, bookmark_owner, hint)
            if existing and (existing.assets or (not download and existing.preview_cover)):
                continue
            new_job = enqueue_import(db, {"platform": platform, "remote_id": ident,
                "account_id": subscription.account_id, "download": download,
                **({"pixiv_bookmark_state": hint} if hint is not None else {})}, commit=False, locked=True)
            created.append(new_job.id)
        subscription.last_checked_at = utcnow()
        subscription.last_error = None
        guard(db)
        db.commit()
        return {"subscription_id": subscription.id, "discovered": len(identities), "queued_jobs": created,
                "latest_page_only": True, "has_more": has_more,
                "message": ("已检查作者作品及公开收藏的最新一页；详情与封面已排队，不扩展收藏作品的作者；历史补全尚未实现"
                            if platform == "pixiv" and subscription.target_type == "author" else "已检查最新一页；历史补全尚未实现")}
    if platform == "picacg":
        identities = connector.favorites(checkpoint=report)
        report(90)
        db.rollback()
        queue_control.lock_control(db)
        created = []
        for ident in dict.fromkeys(identities):
            existing = db.scalar(select(Item).where(Item.source == platform, Item.source_id == ident))
            if existing and (existing.assets or existing.preview_cover):
                continue
            new_job = enqueue_import(db, {"platform": platform, "remote_id": ident,
                "account_id": payload.get("account_id"), "download": False}, commit=False, locked=True)
            created.append(new_job.id)
        guard(db)
        db.commit()
        return {"remote_favorites": len(identities), "queued_jobs": created,
                "message": "收藏已排队导入元数据；未自动下载，不代表作者关注同步"}
    follows = connector.follows(checkpoint=report)
    report(90)
    db.rollback()
    added = 0
    for follow in follows:
        existing = db.scalar(select(Subscription).where(Subscription.platform == platform,
            Subscription.target_type == follow["target_type"], Subscription.target == follow["target"]))
        if existing:
            continue
        db.add(Subscription(platform=platform, target_type=follow["target_type"], target=follow["target"],
            label=follow["label"][:191], enabled=True, auto_download=False, account_id=payload.get("account_id"),
            content_scope="all" if platform == "pixiv" and follow["target_type"] == "author" else "images",
            next_check_at=utcnow() + timedelta(minutes=5 + added)))
        added += 1
    watchlist_job = None
    if platform == "pixiv" and getattr(connector, "supports_watchlist", False):
        from .watchlist import queue_sync
        watchlist_job = queue_sync(db, automatic=True)
    guard(db)
    db.commit()
    from .pixiv_follow_download import default_works
    download_authors = platform == 'pixiv' and default_works(db)
    return {"added_subscriptions": added, "remote_follows": len(follows), "watchlist_job_id": watchlist_job.id if watchlist_job else None,
            "message": "关注作者已导入为本地订阅；检查更新时默认下载作者作品，作者收藏单独控制" if download_authors else
                       "关注作者已导入为本地订阅；默认仅建档元数据，不自动下载"}


def process_job(job_id):
    kind = "unknown"
    try:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if not job or job.status != "running" or job.worker_id != current_worker_id():
                return
            payload, kind = job.payload, job.kind
            lane = queue_control.job_lane(job)
            if payload.get("retry_queue") is True:
                from .pixiv_accounts import validate_retry_assignment
                validate_retry_assignment(db, job)
            progress = 0
            last_heartbeat = 0.0
            def report(value):
                nonlocal progress, last_heartbeat
                checkpoint(job_id, value)
                progress, last_heartbeat = value, time.monotonic()
                queue_control.check_paused(lane)
            def heartbeat():
                if time.monotonic() - last_heartbeat >= 1:
                    report(progress)
            guard = lambda session: commit_guard(session, job_id)
            if kind == "import_content" or (kind == "import" and payload.get("platform") == "pixiv" and payload.get("download", True)):
                item = db.get(Item, payload["item_id"]) if payload.get("item_id") else None
                if item and not item.assets:
                    item.content_state, item.content_error, item.content_job_id = "downloading", None, job_id
            # End read transaction before long network/model work; no SQLite read lock held.
            db.commit()
            from .download_staging import staging_context
            with staging_context(), queue_control.request_context(heartbeat, lane) if kind in queue_control.ACQUISITION_KINDS else nullcontext():
                if kind == "import":
                    result = _import(db, payload, report, guard, job_id)
                elif kind == "import_content":
                    result = _import_content(db, payload, report, guard)
                elif kind == "source_sync":
                    result = _sync(db, payload, report, guard)
                elif kind == "pixiv_bookmark":
                    from .pixiv_favorites import process as process_bookmark
                    result = process_bookmark(db, payload, report, guard)
                elif kind == "series_sync":
                    from app.series_service import sync_series
                    result = sync_series(db, payload, report, guard)
                elif kind == "ai":
                    from app.ai.service import analyze_item
                    result = analyze_item(db, payload["item_id"], report, guard)
                elif kind == "translate":
                    from app.translation import process_translation
                    result = process_translation(db, payload, report, guard)
                elif kind in ("author_profile", "author_feed"):
                    from app.author_api import sync_profile, sync_feed
                    result = (sync_profile if kind == "author_profile" else sync_feed)(db, payload, report, guard)
                else:
                    raise ConnectorError("未知任务类型")
            db.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.worker_id == current_worker_id())
                       .values(status="completed", progress=100, result=result, error=None, lease_until=None, worker_id=None))
            db.commit()
    except JobCancelled:
        with SessionLocal() as db:
            # Shutdown returns in-flight work to queue; user cancellation remains cancelled.
            db.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.worker_id == current_worker_id())
                .values(status="queued", lease_until=None, worker_id=None))
            db.commit()
            sync_content_state(db, job_id)
    except Exception as exc:
        if _stop.is_set():
            # The proxy/client can close while a request is still unwinding on
            # shutdown. Treat that transport failure as interrupted work, not
            # invalid credentials or a failed download. A user cancellation or
            # lease transferred to another worker must never be overwritten.
            with SessionLocal() as db:
                db.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.worker_id == current_worker_id())
                    .values(status="queued", lease_until=None, worker_id=None))
                db.commit()
                sync_content_state(db, job_id)
            return
        if isinstance(exc, ConnectorError):
            message = str(exc)
        else:
            _record_internal_failure(job_id, kind, exc)
            message = f"任务内部处理错误（{_exception_type(exc)}）。请提供任务编号以便排查，修复后重试。"
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job and job.status == "running" and job.worker_id == current_worker_id():
                job.status, job.error, job.lease_until, job.worker_id = "failed", message[:1500], None, None
                subscription = db.get(Subscription, job.payload.get("subscription_id")) if job.payload.get("subscription_id") else None
                if subscription:
                    subscription.last_error = message[:1500]
                    subscription.last_checked_at = utcnow()
                if job.payload.get("watchlist") or job.payload.get("watchlist_entry_id"):
                    from .watchlist import failure
                    failure(db, job, message)
                db.commit()
                sync_content_state(db, job_id)


def sync_content_state(db, job_id):
    """Reflect queue controls on the body state without discarding a preview."""
    db.expire_all()
    job = db.get(Job, job_id)
    if job is None or job.kind not in ("import", "import_content"):
        return
    item_id = job.payload.get("item_id")
    item = db.get(Item, item_id) if item_id else None
    if item is None or item.assets or item.content_job_id != job_id:
        return
    if job.status == "failed":
        item.content_state, item.content_error = "failed", job.error
    elif job.status == "cancelled":
        item.content_state, item.content_error = "metadata", None
    elif job.status == "queued":
        item.content_state, item.content_error = "queued", None
    db.commit()


def schedule_subscriptions(db, *, commit=True):
    from .pixiv_collection import schedule as schedule_bookmarks
    schedule_bookmarks(db)
    from .watchlist import schedule
    schedule(db)
    now = utcnow()
    subscriptions = db.scalars(select(Subscription).where(Subscription.enabled.is_(True),
        or_(Subscription.next_check_at.is_(None), Subscription.next_check_at <= now)).limit(20).with_for_update(skip_locked=True)).all()
    identifiers = [subscription.id for subscription in subscriptions]
    existing = set(db.scalars(select(Job.payload["subscription_id"].as_string()).where(
        Job.kind == "source_sync", Job.status.in_(("queued", "running")),
        Job.payload["subscription_id"].as_string().in_(identifiers)))) if identifiers else set()
    connectors = {}
    for subscription in subscriptions:
        if subscription.platform not in connectors:
            connectors[subscription.platform] = get_connector(subscription.platform)
        connector = connectors[subscription.platform]
        if not connector.supports_subscriptions:
            subscription.last_error = "该平台定时订阅待实现；已保存规则，尚未执行远端检查"
            subscription.next_check_at = now + timedelta(minutes=subscription.interval_minutes)
            continue
        if subscription.id not in existing:
            db.add(Job(kind="source_sync", status="queued", payload={"platform": subscription.platform,
                       "account_id": subscription.account_id, "subscription_id": subscription.id}))
        subscription.next_check_at = now + timedelta(minutes=subscription.interval_minutes)
    if commit:
        db.commit()


def claim_once(allowed_lanes=None, *, preflight=False):
    allowed = set(queue_control.LANES if allowed_lanes is None else allowed_lanes)
    if not allowed <= queue_control.LANES.keys():
        raise ValueError("Unknown worker queue")
    if not allowed:
        return None
    with SessionLocal() as db:
        if preflight:
            control = db.get(queue_control.QueueControl, 1)
            if control:
                limits = queue_control.lane_settings(control)
                allowed = {key for key in allowed if not limits[key]["paused"] and not (control.paused and key in queue_control.COLLECTION_LANES)}
            if not allowed:
                return None
            expression = queue_control.lane_expression()
            scope = or_(*(queue_control.lane_clause(key) for key in allowed))
            queued = db.scalar(select(Job.id).where(Job.status == "queued", scope).limit(1))
            stale = db.scalar(select(Job.id).where(Job.status == "running", or_(Job.lease_until.is_(None), Job.lease_until < utcnow())).limit(1))
            from .watchlist import due as watchlist_due
            from .pixiv_collection import due as bookmark_scan_due
            due = "checks" in allowed and (db.scalar(select(Subscription.id).where(Subscription.enabled.is_(True),
                    or_(Subscription.next_check_at.is_(None), Subscription.next_check_at <= utcnow())).limit(1)) or watchlist_due(db) or bookmark_scan_due(db))
            full = False
            if control and len(allowed) == 1 and not stale and not due:
                key = next(iter(allowed))
                full = db.scalar(select(func.count(Job.id)).where(Job.status == "running", scope)) >= limits[key]["concurrency"]
            db.rollback()  # Fresh MySQL snapshot after obtaining the claim lock.
            if full or not queued and not stale and not due:
                return None
        control = queue_control.lock_control(db)
        now = utcnow()
        stale = db.scalars(select(Job).where(Job.status == "running", or_(Job.lease_until.is_(None), Job.lease_until < now))).all()
        for job in stale:
            job.status, job.worker_id, job.lease_until = "queued", None, None
            if (job.kind == "import_content" or bundled_pixiv(job)) and job.payload.get("item_id"):
                item = db.get(Item, job.payload["item_id"])
                if item and not item.assets:
                    item.content_state, item.content_error = "queued", None
        db.flush()
        # The persisted row lock serializes claims across threads/processes.
        # Reducing the limit drains existing work; it never cancels a download.
        expression = queue_control.lane_expression()
        limits = queue_control.lane_settings(control)
        counts = dict(db.execute(select(expression, func.count(Job.id)).where(Job.status == "running").group_by(expression)).all())
        available = [key for key, value in limits.items() if key in allowed and not value["paused"]
                     and not (control.paused and key in queue_control.COLLECTION_LANES)
                     and counts.get(key, 0) < value["concurrency"]]
        if "checks" in available:
            schedule_subscriptions(db, commit=False)
        if not available:
            db.commit()
            return None
        # Query each available lane separately so a busy account in a large
        # download backlog cannot hide a runnable AI or author-check task.
        candidates = []
        from .pixiv_accounts import assign_download_account, assignment_state, assignment_filter
        resources = assignment_state(db)
        account_filter = assignment_filter(resources)
        for key in available:
            candidates.extend(db.scalars(select(Job).where(Job.status == "queued", queue_control.lane_clause(key), account_filter)
                              .order_by(Job.created_at, Job.id).limit(64)).all())
        candidates.sort(key=lambda job: (job.kind == "import" if control.cover_streak >= 4 else job.kind != "import", job.created_at, job.id))
        chosen = next((job for job in candidates if assign_download_account(db, job, control, state=resources)), None)
        if chosen is None:
            db.commit()
            return None
        candidate = chosen.id
        owner = str(uuid.uuid4())
        claimed = db.execute(update(Job).where(Job.id == candidate, Job.status == "queued").values(
            status="running", worker_id=owner, lease_until=now + timedelta(seconds=LEASE_SECONDS),
            attempts=Job.attempts + 1, error=None, progress=0))
        if claimed.rowcount:
            if chosen.kind == "import":
                control.cover_streak = min(4, control.cover_streak + 1)
            else:
                control.cover_streak = 0
        db.commit()
        if not claimed.rowcount:
            return None
    return candidate, owner


def execute_claim(candidate, owner):
    token = _lease_owner.set(owner)
    try:
        process_job(candidate)
    finally:
        _lease_owner.reset(token)
    return True


def run_once(allowed_lanes=None):
    claim = claim_once(allowed_lanes)
    if not claim:
        return False
    execute_claim(*claim)
    return True


def _loop():
    while not _stop.is_set():
        try:
            from .novel_covers import scheduler
            scheduler.tick()
            worked = run_once()
        except Exception:
            # Deliberately exclude exception text: SQL/HTTP errors can contain secrets.
            log.warning("Background worker temporarily unavailable; will retry")
            worked = False
        if not worked:
            _stop.wait(2)


def start_worker():
    global _threads
    if os.getenv("LIBRARY_WORKER_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    if os.getenv("LIBRARY_WORKER_MODE", "process") == "process":
        from .worker_runtime import supervisor
        supervisor.start()
        return
    if os.getenv("LIBRARY_WORKER_MODE") != "thread":
        raise ValueError("LIBRARY_WORKER_MODE must be process or thread")
    with _lifecycle_lock:
        if any(thread.is_alive() for thread in _threads):
            return
        _stop.clear()
        # Enough executors for every independently bounded lane. Database claims
        # enforce lane and account limits across all processes.
        _threads = [Thread(target=_loop, name=f"library-worker-{index + 1}", daemon=True)
                    for index in range(queue_control.MAX_CONCURRENCY * len(queue_control.LANES))]
        for thread in _threads:
            thread.start()


def stop_worker():
    from .worker_runtime import supervisor
    if supervisor.workers or supervisor.placement is not None:
        supervisor.stop()
    with _lifecycle_lock:
        _stop.set()
        deadline = time.monotonic() + 2
        for thread in _threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
