from __future__ import annotations
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select

from app.auth import require_auth
from app.database import get_db
from app.models import Item, Job, SourceAccount, Subscription
from app.integrations import PLATFORMS, get_connector
from app.integrations.base import ConnectorError
from app.integrations.credentials import encrypt_credentials
from app.jobs import account_credentials, enqueue, enqueue_import, job_view, sync_content_state
from app import queue_control
from app.ai.service import model_status


router = APIRouter(dependencies=[Depends(require_auth)])


class AccountInput(BaseModel):
    label: str = Field(min_length=1, max_length=191)
    credentials: dict[str, str] = Field(max_length=20)

    @field_validator("label")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("账号名称不能为空")
        return value.strip()


class ImportInput(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    remote_id: str | None = Field(default=None, max_length=512)
    account_id: str | None = None


class SyncInput(BaseModel):
    account_id: str | None = None


class QueueSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool | None = None
    rate_limit_enabled: bool | None = Field(default=None, strict=True)
    concurrency: int | None = Field(default=None, strict=True, ge=1, le=queue_control.MAX_CONCURRENCY)
    min_interval_seconds: float | None = Field(default=None, ge=0, le=60, allow_inf_nan=False)
    max_interval_seconds: float | None = Field(default=None, ge=0, le=60, allow_inf_nan=False)
    request_interval_seconds: float | None = Field(default=None, ge=0, le=60, allow_inf_nan=False)


class LanePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool | None = Field(default=None, strict=True)
    concurrency: int | None = Field(default=None, strict=True, ge=1, le=8)


class CoverBackfillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["pixiv", "ehentai", "picacg", "jm", "kemono"] | None = None


class SubscriptionInput(BaseModel):
    platform: Literal["pixiv", "ehentai", "picacg", "jm", "kemono"]
    target_type: Literal["author", "tag", "search"]
    target: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=191)
    enabled: bool = True
    auto_download: bool = False
    download_works: bool | None = None
    download_bookmarks: bool | None = None
    content_scope: Literal["images", "novels", "all"] | None = None
    account_id: str | None = None
    interval_minutes: int = Field(default=360, ge=30, le=10080)


class SubscriptionPatch(BaseModel):
    target: str | None = Field(default=None, min_length=1, max_length=512)
    label: str | None = Field(default=None, max_length=191)
    enabled: bool | None = None
    auto_download: bool | None = None
    download_works: bool | None = None
    download_bookmarks: bool | None = None
    content_scope: Literal["images", "novels", "all"] | None = None
    account_id: str | None = None
    interval_minutes: int | None = Field(default=None, ge=30, le=10080)


class AnalyzeInput(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=100)


def connector_or_404(platform):
    if platform not in PLATFORMS:
        raise HTTPException(404, "未知平台")
    return get_connector(platform)


def validate_account(db, platform, account_id):
    if account_id:
        account = db.get(SourceAccount, account_id)
        if not account or account.platform != platform:
            raise HTTPException(400, "账号不存在或与平台不匹配")


def subscription_view(value, *, default_works=False):
    from .subscription_policy import flags
    options = flags(value, default_works=default_works)
    ready = get_connector(value.platform).supports_subscriptions
    scope_message = ("检查作者作品及公开收藏的最新一页；不扩展收藏作品的作者；历史补全待实现"
                     if value.platform == "pixiv" and value.target_type == "author" else "定时检查最新一页；历史补全待实现")
    return {"id": value.id, "platform": value.platform, "target_type": value.target_type,
            "target": value.target, "label": value.label, "enabled": value.enabled,
            "auto_download": any(options.values()) if value.platform == "pixiv" and value.target_type == "author" else value.auto_download,
            **options, "account_id": value.account_id, "content_scope": value.content_scope,
            "interval_minutes": value.interval_minutes,
            "created_at": value.created_at.isoformat() + "Z", "updated_at": value.updated_at.isoformat() + "Z",
            "last_checked_at": value.last_checked_at.isoformat() + "Z" if value.last_checked_at else None,
            "next_check_at": value.next_check_at.isoformat() + "Z" if value.next_check_at else None,
            "last_error": value.last_error,
            "runtime_status": "paused" if not value.enabled else ("active" if ready else "pending"),
            "status_message": scope_message if ready else "规则已保存，平台定时检查待实现"}


@router.get("/sources")
def sources(db=Depends(get_db)):
    result = []
    for platform, label in PLATFORMS.items():
        connector = get_connector(platform)
        accounts = db.scalars(select(SourceAccount).where(SourceAccount.platform == platform)).all()
        result.append({"platform": platform, "label": label,
            "status": "ready" if connector.supports_import else "unavailable",
            "capabilities": connector.capabilities(), "accounts": [{"id": a.id, "label": a.label,
                "auth_status": "verified" if a.verified_at else "unverified",
                "verified_at": a.verified_at.isoformat() + "Z" if a.verified_at else None} for a in accounts],
            "message": connector.message})
    return result


@router.post("/sources/{platform}/accounts")
def add_account(platform: str, value: AccountInput, db=Depends(get_db)):
    connector = connector_or_404(platform)
    if sum(len(v) for v in value.credentials.values()) > 16000:
        raise HTTPException(400, "账号凭据过长")
    connector.credentials = value.credentials
    try:
        connector.validate_credentials()
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    account = SourceAccount(platform=platform, label=value.label.strip(),
                            credentials_encrypted=encrypt_credentials(value.credentials))
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"id": account.id, "label": account.label, "platform": platform, "auth_status": "unverified", "verified_at": None}


@router.delete("/sources/{platform}/accounts/{account_id}")
def remove_account(platform: str, account_id: str, db=Depends(get_db)):
    account = db.get(SourceAccount, account_id)
    if not account or account.platform != platform:
        raise HTTPException(404, "账号不存在")
    for subscription in db.scalars(select(Subscription).where(Subscription.account_id == account_id)):
        subscription.enabled = False
        subscription.account_id = None
        subscription.last_error = "账号已删除，请重新指定账号并启用订阅"
    db.delete(account)
    db.commit()
    return {"ok": True}


@router.post("/sources/{platform}/import")
def source_import(platform: str, value: ImportInput, db=Depends(get_db)):
    connector = connector_or_404(platform)
    if not connector.supports_import:
        raise HTTPException(501, connector.message)
    validate_account(db, platform, value.account_id)
    try:
        remote_id = connector.normalize_id(value.url or value.remote_id or "")
        if platform in ("pixiv", "picacg"):
            connector.credentials = account_credentials(db, platform, value.account_id)
            connector.validate_credentials()
    except (ConnectorError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if platform == "pixiv":
        try:
            account_credentials(db, platform, value.account_id, purpose="manual")
        except ConnectorError as exc:
            raise HTTPException(409, str(exc)) from None
    return job_view(enqueue_import(db, {"platform": platform, "remote_id": remote_id, "account_id": value.account_id,
                                     "download_origin": "manual"}))


@router.post("/items/{item_id}/download")
def download_item(item_id: str, value: SyncInput | None = None, db=Depends(get_db)):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(404, "作品不存在")
    if item.source not in PLATFORMS or not item.source_id:
        raise HTTPException(400, "本地作品没有可重新获取的远端来源")
    if item.assets:
        raise HTTPException(409, "正文已经保存；缺失文件请检查存储或恢复备份")
    account_id = value.account_id if value else None
    validate_account(db, item.source, account_id)
    if item.source == "pixiv":
        try:
            account_credentials(db, "pixiv", account_id, purpose="manual")
        except ConnectorError as exc:
            raise HTTPException(409, str(exc)) from None
    return job_view(enqueue_import(db, {"platform": item.source, "remote_id": item.source_id,
                                     "item_id": item.id, "account_id": account_id, "download": True, "download_origin": "manual"}))


@router.get("/queue/settings")
def queue_settings():
    return queue_control.read_settings()


@router.put("/queue/settings")
def set_queue_settings(value: QueueSettingsPatch):
    return queue_control.update_settings(value.model_dump(exclude_none=True, exclude_unset=True))


@router.get("/queue/status")
def queue_status(db=Depends(get_db)):
    from .worker_runtime import status as runtime_status
    control = queue_control.read_settings()
    counts = dict(db.execute(select(Job.status, func.count(Job.id)).where(
        Job.kind.in_(queue_control.ACQUISITION_KINDS)).group_by(Job.status)).all())
    active = db.scalars(select(Job).where(Job.kind.in_(queue_control.ACQUISITION_KINDS),
                                      Job.status == "running").order_by(Job.created_at, Job.id)).all()
    running = [job_view(job) for job in active]
    expression = queue_control.lane_expression()
    lane_counts = {(lane, status): count for lane, status, count in db.execute(select(expression, Job.status, func.count(Job.id)).group_by(expression, Job.status))}
    row = db.get(queue_control.QueueControl, 1)
    lanes = [{"id": key, "label": label, **queue_control.lane_settings(row)[key],
              "blocked_by_master": control["paused"] and key in queue_control.COLLECTION_LANES,
              "counts": {status: lane_counts.get((key, status), 0) for status in ("queued", "running", "failed", "completed")}}
             for key, label in queue_control.LANES.items()]
    return {"settings": control, "counts": {key: counts.get(key, 0) for key in ("queued", "running", "failed")},
            "active": running[0] if running else None, "active_jobs": running, "lanes": lanes, "runtime": runtime_status()}


@router.put("/queue/lanes/{lane}")
def set_lane(lane: str, value: LanePatch):
    if lane not in queue_control.LANES:
        raise HTTPException(404, "队列不存在")
    return queue_control.update_lane(lane, value.model_dump(exclude_none=True, exclude_unset=True))


@router.post("/queue/covers")
def backfill_covers(value: CoverBackfillInput | None = None, db=Depends(get_db)):
    queue_control.lock_control(db)
    query = select(Item).where(Item.source.in_(tuple(PLATFORMS)), Item.source_id.is_not(None),
        or_(~Item.assets.any(), Item.type.in_(("novel", "article"))))
    if value and value.source:
        query = query.where(Item.source == value.source)
    active_ids = set(db.scalars(select(Job.id).where(Job.kind.in_(("import", "import_content")), Job.status.in_(("queued", "running")))))
    queued = skipped = 0
    has_more = False
    for item in db.scalars(query.order_by(Item.created_at, Item.id)).all():
        if item.preview_cover:
            skipped += 1
            continue
        if queued >= 500:
            has_more = True
            break
        job = enqueue_import(db, {"platform": item.source, "remote_id": item.source_id,
                                 "item_id": item.id, "download": False}, commit=False, locked=True)
        if job.id in active_ids:
            skipped += 1
        else:
            queued += 1
            active_ids.add(job.id)
    db.commit()
    return {"queued": queued, "skipped": skipped, "has_more": has_more,
            "message": "仅为缺失封面排队获取资料与封面；未开启正文下载或修改订阅设置"}


@router.post("/sources/{platform}/sync")
def sync_source(platform: str, value: SyncInput, db=Depends(get_db)):
    connector = connector_or_404(platform)
    if not connector.supports_sync:
        raise HTTPException(501, "此平台关注同步待实现")
    validate_account(db, platform, value.account_id)
    try:
        connector.credentials = account_credentials(db, platform, value.account_id)
        connector.validate_credentials()
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    return job_view(enqueue(db, "source_sync", {"platform": platform, "account_id": value.account_id}))


def job_history_query(queue: str | None = None):
    query = select(Job)
    if queue:
        query = query.where(queue_control.lane_expression() == queue)
    # MySQL can prefer the queue claim index and sort the entire lane. History
    # always reads a bounded recent page; its index also preserves the ID tie order.
    index = "ix_jobs_lane_recent" if queue else "ix_jobs_recent"
    return query.with_hint(Job, f"FORCE INDEX ({index})", dialect_name="mysql").order_by(Job.created_at.desc(), Job.id).limit(200)


@router.get("/jobs")
def jobs(queue: str | None = None, db=Depends(get_db)):
    if queue and queue not in queue_control.LANES:
        raise HTTPException(422, "队列不存在")
    return {"items": [job_view(j) for j in db.scalars(job_history_query(queue))]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db=Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job_view(job)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, db=Depends(get_db)):
    queue_control.lock_control(db)
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(409, "只能重试失败或已取消的任务")
    if job.kind in ("author_profile", "author_feed"):
        duplicate = db.scalar(select(Job).where(Job.id != job.id, Job.kind == job.kind,
            Job.status.in_(("queued", "running")), Job.payload["identity"].as_string() == job.payload.get("identity")).with_for_update())
        if duplicate:
            return job_view(duplicate)
        if job.kind == "author_feed":
            from .models import AuthorFeed
            feed = db.get(AuthorFeed, job.payload.get("feed_id"))
            if feed is None:
                raise HTTPException(409, "作者目录或账号已删除，请重新打开作者主页")
            job.payload = {**job.payload, "revision": feed.revision}
    if job.kind == "series_sync":
        duplicate = db.scalar(select(Job).where(Job.id != job.id, Job.kind == "series_sync",
            Job.status.in_(("queued", "running")), Job.payload["remote_id"].as_string() == job.payload.get("remote_id"),
            Job.payload["series_kind"].as_string() == job.payload.get("series_kind")).with_for_update())
        if duplicate:
            return job_view(duplicate)
    if job.kind == "pixiv_bookmark":
        from .pixiv_favorites import main_account_id
        main = main_account_id(db)
        if not main:
            raise HTTPException(409, "请先配置 Pixiv 主账号")
        duplicate = db.scalar(select(Job).where(Job.id != job.id, Job.kind == "pixiv_bookmark",
            Job.status.in_(("queued", "running")), Job.payload["item_id"].as_string() == job.payload.get("item_id"),
            Job.payload["main_account_id"].as_string() == main).with_for_update())
        if duplicate:
            return job_view(duplicate)
        job.payload = {**job.payload, "main_account_id": main, "account_id": main}
    if job.kind in ("import", "import_content"):
        duplicate = db.scalar(select(Job).where(Job.id != job.id,
            Job.kind.in_(("import", "import_content")), Job.status.in_(("queued", "running")),
            Job.payload["platform"].as_string() == job.payload.get("platform"),
            Job.payload["remote_id"].as_string() == job.payload.get("remote_id")).with_for_update())
        if duplicate:
            return job_view(duplicate)
        item_id = job.payload.get("item_id")
        item = db.scalar(select(Item).where(Item.id == item_id).with_for_update()) if item_id else None
        if item is not None and not item.assets and (job.kind == "import_content" or job.payload.get("download", True)):
            item.content_job_id = job.id
    job.payload = {**job.payload, "retry_queue": True,
                   "retry_last_error": job.error or job.payload.get("retry_last_error")}
    job.status, job.error, job.result, job.progress = "queued", None, None, 0
    if job.payload.get("platform") == "pixiv" or job.kind in ("author_profile", "author_feed", "series_sync"):
        job.payload = {key: value for key, value in job.payload.items() if key not in ("download_account_id", "details_account_id", "check_account_id", "execution_account_id")}
    job.lease_until, job.worker_id = None, None
    db.commit()
    sync_content_state(db, job.id)
    db.refresh(job)
    return job_view(job)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db=Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.status not in ("queued", "running"):
        raise HTTPException(409, "该任务已结束")
    job.status = "cancelled"
    job.lease_until, job.worker_id = None, None
    db.commit()
    sync_content_state(db, job.id)
    db.refresh(job)
    return job_view(job)


@router.get("/subscriptions")
def subscriptions(db=Depends(get_db)):
    from .pixiv_follow_download import default_works
    default = default_works(db)
    return {"items": [subscription_view(v, default_works=default) for v in db.scalars(select(Subscription).order_by(Subscription.created_at.desc()))]}


def check_target(platform, target_type, target):
    if not target.strip():
        raise HTTPException(400, "订阅目标不能为空")
    if platform == "pixiv" and target_type == "author" and not target.isdigit():
        raise HTTPException(400, "Pixiv 作者订阅使用数字作者 ID")
    if platform == "picacg" and target_type == "author":
        raise HTTPException(400, "哔咔作者订阅尚未实现，请使用 tag 或 search")
    if platform == "kemono":
        if target_type == "author":
            try:
                get_connector(platform).normalize_author(target)
            except (ConnectorError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from None
        elif len(target) > 200 or target_type == "search" and len(target.strip()) < 3:
            raise HTTPException(400, "Kemono 搜索至少 3 个字符；标签和搜索最多 200 个字符")


@router.post("/subscriptions")
def add_subscription(value: SubscriptionInput, db=Depends(get_db)):
    scope = value.content_scope or ("all" if value.platform == "pixiv" and value.target_type == "author" else "images")
    if value.platform != "pixiv" and scope != "images":
        raise HTTPException(400, "当前仅 Pixiv 支持小说订阅范围")
    validate_account(db, value.platform, value.account_id)
    check_target(value.platform, value.target_type, value.target)
    item = Subscription(**{**value.model_dump(), "content_scope": scope})
    if 'auto_download' in value.model_fields_set and not {'download_works', 'download_bookmarks'} & value.model_fields_set:
        item.download_works = item.download_bookmarks = None if value.auto_download else False
    item.label = value.label.strip() or value.target[:191]
    if not get_connector(value.platform).supports_subscriptions:
        item.last_error = "该平台定时检查待实现；规则已保存"
    db.add(item)
    db.commit()
    db.refresh(item)
    from .pixiv_follow_download import default_works
    return subscription_view(item, default_works=default_works(db))


@router.patch("/subscriptions/{subscription_id}")
def patch_subscription(subscription_id: str, value: SubscriptionPatch, db=Depends(get_db)):
    item = db.get(Subscription, subscription_id)
    if not item:
        raise HTTPException(404, "订阅不存在")
    changes = value.model_dump(exclude_unset=True)
    if changes.get("auto_download") is not None and not {"download_works", "download_bookmarks"} & changes.keys():
        item.download_works = item.download_bookmarks = None if changes['auto_download'] else False
    if item.platform != "pixiv" and changes.get("content_scope", "images") not in (None, "images"):
        raise HTTPException(400, "当前仅 Pixiv 支持小说订阅范围")
    if "account_id" in changes:
        validate_account(db, item.platform, changes["account_id"])
    if changes.get("target") is not None:
        check_target(item.platform, item.target_type, changes["target"])
    for key, field_value in changes.items():
        if field_value is not None or key in ("account_id", "download_works", "download_bookmarks"):
            setattr(item, key, field_value)
    item.next_check_at = None
    db.commit()
    db.refresh(item)
    from .pixiv_follow_download import default_works
    return subscription_view(item, default_works=default_works(db))


@router.delete("/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: str, db=Depends(get_db)):
    item = db.get(Subscription, subscription_id)
    if not item:
        raise HTTPException(404, "订阅不存在")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/ai/status")
def ai_status(db=Depends(get_db)):
    from .ai_nodes import image_status
    return image_status(db) or model_status()


@router.post("/ai/analyze")
def analyze(value: AnalyzeInput, db=Depends(get_db)):
    item_ids = list(dict.fromkeys(value.item_ids))
    for item_id in item_ids:
        if not db.get(Item, item_id):
            raise HTTPException(404, "作品不存在")
    return {"jobs": [job_view(enqueue(db, "ai", {"item_id": item_id})) for item_id in item_ids]}
