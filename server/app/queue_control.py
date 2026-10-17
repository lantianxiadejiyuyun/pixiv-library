"""Persistent acquisition settings and request pacing scoped to worker jobs.

No settings lookup or waiting occurs for login, UI search, local AI, or proxy
checks. Adapters call wait_for_request before each actual HTTP attempt.
"""
from contextlib import contextmanager
from datetime import timedelta
from threading import local
import math
import random
import time

from sqlalchemy import select, text, case, and_, or_, not_, func

from .config import settings
from .database import SessionLocal
from .integrations.base import JobCancelled
from .models import Job, QueueControl, utcnow

ACQUISITION_KINDS = ("import", "import_content", "source_sync", "series_sync", "author_profile", "author_feed", "pixiv_bookmark")
MAX_CONCURRENCY = 8
LANES = {"manual_download": "手动下载", "retry": "失败重试", "download": "自动下载", "details": "详情解析", "checks": "作者与订阅检查", "ai": "AI 识别", "translation": "AI 汉化", "other": "其他任务"}
NETWORK_LANES = ("manual_download", "retry", "download", "details", "checks")
COLLECTION_LANES = ("download", "details", "checks")
_context = local()


def job_lane(job):
    if job.payload.get("retry_queue") is True:
        return "retry"
    return original_lane(job)


def original_lane(job):
    if job.kind in ("import", "import_content") and job.payload.get("download_origin") == "manual":
        return "manual_download"
    if job.kind == "import_content" or (job.kind == "import" and job.payload.get("platform") == "pixiv" and job.payload.get("download", True)):
        return "manual_download" if job.payload.get("download_origin") == "manual" else "download"
    return {"import": "details", "source_sync": "checks", "series_sync": "checks", "author_profile": "checks", "author_feed": "checks", "pixiv_bookmark": "checks", "ai": "ai", "translate": "translation"}.get(job.kind, "other")


def download_requested():
    # MySQL's JSON boolean cast maps a missing property to false before COALESCE
    # can apply its default. Check absence with the nullable string cast first.
    return or_(Job.payload["download"].as_string().is_(None), Job.payload["download"].as_boolean())


def lane_expression():
    return Job.queue_lane


def retry_requested():
    # A missing flag is ordinary work on both MySQL and SQLite. Keep the JSON
    # boolean comparison explicit rather than depending on dialect truthiness.
    return func.coalesce(Job.payload["retry_queue"].as_boolean(), False).is_(True)


def lane_clause(key):
    if key not in LANES:
        raise KeyError(key)
    return Job.queue_lane == key


def lane_settings(control):
    stored = control.lanes or {}
    return {key: {"paused": stored.get(key, {}).get("paused", False),
                  "concurrency": stored.get(key, {}).get("concurrency", control.concurrency if key in COLLECTION_LANES else 1)} for key in LANES}


def update_lane(key, changes):
    if key not in LANES:
        raise ValueError("队列不存在")
    with SessionLocal() as db:
        control = lock_control(db)
        lanes = lane_settings(control)
        lanes[key] = {**lanes[key], **changes}
        control.lanes = lanes
        db.commit()
        return lanes[key]


class QueuePaused(JobCancelled):
    pass


def lock_control(db):
    """Serialize queue claims/settings/request reservations; caller commits."""
    values = {"id": 1, "paused": False, "rate_limit_enabled": True, "concurrency": 1, "download_account_cursor": 0, "min_interval_seconds": settings.request_interval_min_seconds,
              "max_interval_seconds": settings.request_interval_max_seconds,
              "cover_streak": 0, "created_at": utcnow(), "updated_at": utcnow()}
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        if not db.in_transaction():
            db.execute(text("BEGIN IMMEDIATE"))
        db.execute(insert(QueueControl).values(**values).on_conflict_do_nothing(index_elements=["id"]))
    else:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(QueueControl).values(**values)
        db.execute(statement.on_duplicate_key_update(id=QueueControl.id))
    return db.scalar(select(QueueControl).where(QueueControl.id == 1).with_for_update().execution_options(populate_existing=True))


def view(control):
    return {"paused": control.paused, "rate_limit_enabled": control.rate_limit_enabled, "min_interval_seconds": control.min_interval_seconds,
            "max_interval_seconds": control.max_interval_seconds, "concurrency": control.concurrency}


def read_settings():
    with SessionLocal() as db:
        control = db.get(QueueControl, 1)
        if control is None:
            control = lock_control(db)
            db.commit()
        return view(control)


def update_settings(changes):
    with SessionLocal() as db:
        control = lock_control(db)
        if "request_interval_seconds" in changes:
            if "min_interval_seconds" in changes or "max_interval_seconds" in changes:
                raise ValueError("固定间隔与随机间隔不能同时指定")
            changes = {**changes, "min_interval_seconds": changes["request_interval_seconds"], "max_interval_seconds": changes["request_interval_seconds"]}
        for key in ("paused", "rate_limit_enabled", "concurrency", "min_interval_seconds", "max_interval_seconds"):
            if key in changes:
                setattr(control, key, changes[key])
        if "concurrency" in changes:
            # Collection-wide settings must not overwrite independent lanes.
            lanes = lane_settings(control)
            for key in COLLECTION_LANES:
                lanes[key]["concurrency"] = changes["concurrency"]
            control.lanes = lanes
        if type(control.concurrency) is not int or not 1 <= control.concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"并发任务数必须是 1–{MAX_CONCURRENCY} 的整数")
        if not all(math.isfinite(v) for v in (control.min_interval_seconds, control.max_interval_seconds)) or not 0 <= control.min_interval_seconds <= control.max_interval_seconds <= 60:
            raise ValueError("请求间隔必须满足 0 ≤ 最短 ≤ 最长 ≤ 60 秒")
        if changes.get("rate_limit_enabled") is False:
            control.next_request_at = None
        # Keep an outstanding reservation/backoff: changing the slider must not
        # bypass an upstream rate limit or send another request immediately.
        db.commit()
        return view(control)


def check_paused(lane=None):
    with SessionLocal() as db:
        control = db.get(QueueControl, 1)
        lane = lane or getattr(_context, "lane", None)
        if control is not None and ((control.paused and (lane is None or lane in COLLECTION_LANES)) or (lane and lane_settings(control)[lane]["paused"])):
            raise QueuePaused()


def current_lane():
    return getattr(_context, "lane", None)


@contextmanager
def request_context(heartbeat, lane=None):
    previous = getattr(_context, "heartbeat", None)
    previous_lane = getattr(_context, "lane", None)
    _context.heartbeat = heartbeat
    _context.lane = lane
    try:
        yield
    finally:
        _context.heartbeat = previous
        _context.lane = previous_lane


def wait_for_request():
    heartbeat = getattr(_context, "heartbeat", None)
    if heartbeat is None:
        return
    while True:
        heartbeat()
        with SessionLocal() as db:
            control = lock_control(db)
            lane = getattr(_context, "lane", None)
            if control.paused and (lane is None or lane in COLLECTION_LANES):
                raise QueuePaused()
            if lane and lane_settings(control)[lane]["paused"]:
                raise QueuePaused()
            moment = utcnow()
            pacing = (control.next_request_at - moment).total_seconds() if control.rate_limit_enabled and control.next_request_at else 0
            cooldown = (control.backoff_until - moment).total_seconds() if control.backoff_until else 0
            remaining = max(0, pacing, cooldown)
            if remaining <= 0:
                control.next_request_at = moment + timedelta(seconds=random.uniform(control.min_interval_seconds, control.max_interval_seconds)) if control.rate_limit_enabled else None
                db.commit()
                return
            db.rollback()
        # Never keep a DB transaction/row lock open while waiting. Checkpoints
        # detect pause/cancellation and renew the job lease during long waits.
        time.sleep(min(remaining, 0.25))


def defer_requests(seconds=60):
    """Honor rate limiting without changing a user's configured interval."""
    if getattr(_context, "heartbeat", None) is None:
        return
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 60
    if not math.isfinite(seconds):
        seconds = 60
    until = utcnow() + timedelta(seconds=min(3600, max(0, seconds)))
    with SessionLocal() as db:
        control = lock_control(db)
        if control.backoff_until is None or until > control.backoff_until:
            control.backoff_until = until
        if control.next_request_at is None or until > control.next_request_at:
            control.next_request_at = until
        db.commit()
