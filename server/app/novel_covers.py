"""Bounded, metadata-only cover repair in the details worker, never in API reads."""
from datetime import timedelta
import logging
from threading import Lock
import time

from sqlalchemy import String, and_, cast, func, or_, select

from .database import SessionLocal
from .models import Item, Job, SourceAccount, utcnow
from . import queue_control


BATCH_SIZE = 20
SCAN_SIZE = 200
SCAN_INTERVAL = 60
RETRY_AFTER = timedelta(hours=24)
log = logging.getLogger(__name__)


def automatic_cover_job():
    # A user may upgrade a pending metadata import to a body download. That
    # explicit request must no longer be treated as automatic cover repair.
    return and_(Job.kind == "import", Job.payload["auto_novel_cover"].as_boolean().is_(True),
                Job.payload["download"].as_boolean().is_(False))


class NovelCoverScheduler:
    def __init__(self):
        self.cursor = None
        self._next_scan = 0
        self._lock = Lock()

    def tick(self):
        if not self._lock.acquire(blocking=False):
            return 0
        try:
            now = time.monotonic()
            if now < self._next_scan:
                return 0
            self._next_scan = now + SCAN_INTERVAL
            try:
                return self.scan()
            except Exception:
                # Database/connector errors can contain credentials or titles.
                log.warning("Automatic novel cover scheduling temporarily unavailable")
                return 0
        finally:
            self._lock.release()

    def scan(self):
        from .jobs import enqueue_import

        with SessionLocal() as db:
            control = db.get(queue_control.QueueControl, 1)
            if control and (control.paused or queue_control.lane_settings(control)["details"]["paused"]):
                return 0
            if db.scalar(select(SourceAccount.id).where(SourceAccount.platform == "pixiv").limit(1)) is None:
                return 0
            db.rollback()
            control = queue_control.lock_control(db)
            if control.paused or queue_control.lane_settings(control)["details"]["paused"]:
                return 0
            pending = db.scalar(select(func.count(Job.id)).where(automatic_cover_job(), Job.status.in_(("queued", "running"))))
            budget = max(0, BATCH_SIZE - pending)
            if not budget:
                return 0
            # Compare the JSON text explicitly: MySQL JSON equality against a
            # bound "null"/"{}" string differs from SQLite's text storage.
            missing = or_(Item.preview_cover.is_(None), cast(Item.preview_cover, String).in_(("null", "{}")))
            query = select(Item.id, Item.source_id).where(Item.source == "pixiv", Item.type.in_(("novel", "article")),
                Item.source_id.like("novel:%"), missing)
            if self.cursor is not None:
                query = query.where(Item.id > self.cursor)
            rows = db.execute(query.order_by(Item.id).limit(SCAN_SIZE)).all()
            identities = [row.source_id for row in rows]
            # Fetch at most one bounded candidate window's queue history. No
            # ORM Item loading, media reads, or remote requests while locked.
            history = db.scalars(select(Job).where(Job.kind.in_(("import", "import_content")),
                Job.payload["platform"].as_string() == "pixiv", Job.payload["remote_id"].as_string().in_(identities),
                or_(Job.status.in_(("queued", "running")), automatic_cover_job()))
                .order_by(Job.updated_at.desc(), Job.id.desc())).all() if identities else []
            active = {job.payload["remote_id"] for job in history if job.status in ("queued", "running")}
            previous = {}
            for job in history:
                if job.payload.get("auto_novel_cover") and job.payload.get("download") is False:
                    previous.setdefault(job.payload["remote_id"], job)
            now = utcnow()
            queued = 0
            cursor = None
            for item_id, remote_id in rows:
                cursor = item_id
                if remote_id in active:
                    continue
                retry = previous.get(remote_id)
                if retry and retry.updated_at > now - RETRY_AFTER:
                    continue
                if retry:
                    # Reuse the attempt's exact account assignment; failures
                    # never rotate identities to obtain a different permission.
                    retry.status, retry.progress, retry.error, retry.result = "queued", 0, None, None
                    retry.worker_id, retry.lease_until = None, None
                    retry.payload = {**retry.payload, "item_id": item_id}
                    retry.updated_at = now
                else:
                    enqueue_import(db, {"platform": "pixiv", "remote_id": remote_id, "item_id": item_id,
                        "download": False, "auto_novel_cover": True}, commit=False, locked=True)
                queued += 1
                if queued >= budget:
                    break
            db.commit()
            # Continue beyond skipped/cooling-down works instead of starving
            # newer entries behind the same first 200 rows on every scan.
            self.cursor = cursor if rows and (len(rows) == SCAN_SIZE or queued >= budget) else None
            return queued


scheduler = NovelCoverScheduler()
