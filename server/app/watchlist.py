"""Official Pixiv watchlists, scoped to the discovery account and existing queues."""
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from .auth import require_auth, require_admin_cookie
from .database import get_db
from .integrations.base import ConnectorError
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import Job, NetworkSetting, PixivWatchlist, Series, SourceAccount, utcnow
from . import queue_control

router = APIRouter(prefix="/pixiv/watchlist", dependencies=[Depends(require_auth)])
KEY = "pixiv_watchlist_sync"


def owner(db):
    from .pixiv_accounts import read_config
    config = read_config(db)
    ident = config["main_account_id"]
    if config["enabled"]:
        row = db.get(SourceAccount, ident) if ident else None
    else:
        row = db.scalar(select(SourceAccount).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at, SourceAccount.id))
    if row is None or row.platform != "pixiv":
        raise ConnectorError("请先登录并配置 Pixiv 主账号，再同步追更列表")
    return row.id


def read_state(db):
    row = db.get(NetworkSetting, KEY)
    return {"enabled": True, "interval_minutes": 60, "account_id": None, "next_check_at": None,
            "last_checked_at": None, "last_error": None, **(decrypt_credentials(row.value_encrypted) if row else {})}


def save_state(db, state):
    row = db.get(NetworkSetting, KEY)
    if row is None:
        row = NetworkSetting(key=KEY); db.add(row)
    row.value_encrypted = encrypt_credentials(state)


def due(db):
    if db.get(NetworkSetting, KEY) is None:
        return False
    try:
        state, ident = read_state(db), owner(db)
        if not state["enabled"]: return False
        return state["account_id"] != ident or not state["next_check_at"] or datetime.fromisoformat(state["next_check_at"]) <= utcnow()
    except (ConnectorError, ValueError):
        return False


def queue_sync(db, *, locked=False, automatic=False):
    if not locked: queue_control.lock_control(db)
    ident = owner(db)
    state = read_state(db)
    if automatic and not state["enabled"]: return None
    job = db.scalar(select(Job).where(Job.kind == "source_sync", Job.status.in_(("queued", "running")),
        Job.payload["watchlist"].as_boolean().is_(True), Job.payload["account_id"].as_string() == ident).with_for_update())
    if job is None:
        job = Job(kind="source_sync", status="queued", payload={"platform": "pixiv", "account_id": ident, "watchlist": True})
        db.add(job); db.flush()
    state.update(account_id=ident, next_check_at=(utcnow()+timedelta(minutes=state["interval_minutes"])).isoformat())
    save_state(db, state)
    return job


def schedule(db):
    if due(db): queue_sync(db, locked=True, automatic=True)


def queue_series(db, entry):
    job = db.scalar(select(Job).where(Job.kind == "series_sync", Job.status.in_(("queued", "running")),
        Job.payload["watchlist_entry_id"].as_string() == entry.id).with_for_update())
    if job is None:
        job = Job(kind="series_sync", status="queued", payload={"platform": "pixiv", "account_id": entry.account_id,
            "remote_id": entry.remote_id, "series_kind": entry.kind, "watchlist_entry_id": entry.id})
        db.add(job); db.flush()
    return job


def process(db, payload, report, guard):
    from .jobs import get_connector, account_credentials
    ident = owner(db)
    if payload.get("account_id") != ident or payload.get("check_account_id", ident) != ident:
        raise ConnectorError("Pixiv 主账号已更改，旧账号追更结果未保存")
    connector = get_connector("pixiv", account_credentials(db, "pixiv", ident, pinned=True))
    db.commit()
    snapshot = connector.watchlist(checkpoint=report)
    report(90)
    db.rollback(); queue_control.lock_control(db)
    if owner(db) != ident: raise ConnectorError("Pixiv 主账号已更改，旧账号追更结果未保存")
    rows = {(row.kind, row.remote_id): row for row in db.scalars(select(PixivWatchlist).where(PixivWatchlist.account_id == ident).with_for_update())}
    seen, queued, added, changed = set(), [], 0, 0
    for record in snapshot:
        key = (record["kind"], record["remote_id"])
        if key in seen: raise ConnectorError("Pixiv 追更系列重复，未保存列表")
        seen.add(key)
        entry = rows.get(key)
        needs_check = entry is None or entry.latest_content_id != record["latest_content_id"] or entry.content_count != record["content_count"] or not entry.present_remote or entry.last_checked_at is None
        if entry is None:
            entry = PixivWatchlist(account_id=ident, **record)
            db.add(entry); db.flush(); added += 1
        else:
            if entry.latest_content_id != record["latest_content_id"]: changed += 1
            for field, value in record.items(): setattr(entry, field, value)
        entry.present_remote = True
        series = db.scalar(select(Series).where(Series.source == "pixiv", Series.source_id == entry.remote_id, Series.kind == entry.kind).with_for_update())
        if series is None:
            series = Series(origin="source", source="pixiv", source_id=entry.remote_id, kind=entry.kind,
                            title=entry.title, complete=False, revision=0)
            db.add(series); db.flush()
        entry.series_id = series.id
        if entry.enabled and needs_check: queued.append(queue_series(db, entry).id)
    for key, entry in rows.items():
        if key not in seen: entry.present_remote = False
    state = read_state(db)
    state.update(account_id=ident, last_checked_at=utcnow().isoformat(), last_error=None,
                 next_check_at=(utcnow()+timedelta(minutes=state["interval_minutes"])).isoformat())
    save_state(db,state); guard(db); db.commit()
    return {"watchlist": True, "series_count": len(seen), "added": added, "updated": changed, "queued_series": len(queued),
            "message": "漫画与小说追更列表已同步；系列目录进入检查队列，正文按各系列设置下载"}


def series_context(db, payload):
    ident = payload.get("watchlist_entry_id")
    if not ident: return None
    entry = db.scalar(select(PixivWatchlist).where(PixivWatchlist.id == ident).with_for_update().execution_options(populate_existing=True))
    if not entry or entry.account_id != owner(db) or not entry.present_remote or not entry.enabled:
        raise ConnectorError("追更系列已暂停、已移出远端列表或主账号已更改，任务未写入")
    if entry.remote_id != payload["remote_id"] or entry.kind != payload["series_kind"]:
        raise ConnectorError("追更系列任务不匹配")
    return entry


def failure(db, job, message):
    if job.payload.get("watchlist"):
        state = read_state(db)
        if state["account_id"] == job.payload.get("account_id"):
            state["last_error"] = message[:1500]; save_state(db,state)
    if job.payload.get("watchlist_entry_id"):
        row = db.get(PixivWatchlist, job.payload["watchlist_entry_id"])
        if row: row.last_error = message[:1500]


def view(row):
    return {**{key:getattr(row,key) for key in ("id","kind","remote_id","title","author","author_id","content_count",
        "latest_content_id","remote_updated_at","present_remote","enabled","auto_download","series_id","last_error")},
        "has_update": bool(row.latest_content_id and row.latest_content_id != row.seen_content_id),
        "last_checked_at": row.last_checked_at.isoformat()+"Z" if row.last_checked_at else None}


def owned_entry(db, ident):
    try: account = owner(db)
    except ConnectorError as exc: raise HTTPException(409,str(exc)) from None
    row = db.scalar(select(PixivWatchlist).where(PixivWatchlist.id == ident, PixivWatchlist.account_id == account).with_for_update())
    if row is None: raise HTTPException(404,"追更系列不存在")
    return row


@router.get("")
def listing(kind: Literal["novel","manga"] | None = None, q: str = Query(default="",max_length=100),
            updates: bool = False, page: int = Query(default=1,ge=1,le=1000), db=Depends(get_db)):
    state = read_state(db)
    try: ident = owner(db)
    except ConnectorError: ident = None
    clauses = [PixivWatchlist.account_id == ident, PixivWatchlist.present_remote.is_(True)]
    if kind: clauses.append(PixivWatchlist.kind == kind)
    if q.strip(): clauses.append(or_(PixivWatchlist.title.contains(q.strip(),autoescape=True), PixivWatchlist.author.contains(q.strip(),autoescape=True)))
    if updates: clauses.append(PixivWatchlist.latest_content_id.is_not(None) & or_(PixivWatchlist.seen_content_id.is_(None),PixivWatchlist.latest_content_id != PixivWatchlist.seen_content_id))
    total = db.scalar(select(func.count()).select_from(PixivWatchlist).where(*clauses))
    rows = db.scalars(select(PixivWatchlist).where(*clauses).order_by(PixivWatchlist.remote_updated_at.desc(),PixivWatchlist.id).offset((page-1)*30).limit(30)).all()
    public = {key:state[key] for key in ("enabled","interval_minutes","last_checked_at","next_check_at","last_error")}
    public["configured"] = ident is not None
    for key in ("last_checked_at", "next_check_at"):
        if public[key]: public[key] += "Z"
    public["pending_tasks"] = db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("queued","running")),
        Job.payload["account_id"].as_string() == ident,
        or_(Job.payload["watchlist"].as_boolean().is_(True),Job.payload["watchlist_entry_id"].as_string().is_not(None)))) if ident else 0
    return {"items":[view(row) for row in rows],"total":total,"page":page,"page_size":30,"sync":public}


@router.post("/sync")
def sync(db=Depends(get_db)):
    from .jobs import job_view
    try: job = queue_sync(db)
    except ConnectorError as exc: raise HTTPException(409,str(exc)) from None
    db.commit(); return job_view(job)


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    interval_minutes: int = Field(default=60,ge=30,le=10080)


@router.put("/settings",dependencies=[Depends(require_admin_cookie)])
def settings(value: SettingsInput,db=Depends(get_db)):
    queue_control.lock_control(db); state=read_state(db)
    state.update(value.model_dump()); save_state(db,state);db.commit()
    return {key:state[key] for key in ("enabled","interval_minutes")}


class EntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    auto_download: bool | None = None


@router.put("/{entry_id}",dependencies=[Depends(require_admin_cookie)])
def edit(entry_id: str,value: EntryInput,db=Depends(get_db)):
    queue_control.lock_control(db); row=owned_entry(db,entry_id)
    for key,val in value.model_dump(exclude_none=True).items(): setattr(row,key,val)
    if row.enabled and row.present_remote and (value.auto_download is True or value.enabled is True):
        queue_series(db,row)
    db.commit();return view(row)


@router.post("/{entry_id}/read")
def mark_read(entry_id: str,db=Depends(get_db)):
    queue_control.lock_control(db); row=owned_entry(db,entry_id)
    row.seen_content_id=row.latest_content_id;db.commit();return view(row)


@router.post("/{entry_id}/check")
def check(entry_id: str,db=Depends(get_db)):
    from .jobs import job_view
    queue_control.lock_control(db); row=owned_entry(db,entry_id)
    if not row.present_remote or not row.enabled: raise HTTPException(409,"请先恢复该系列追更")
    job=queue_series(db,row);db.commit();return job_view(job)


@router.get("/{entry_id}")
def get_detail(entry_id: str,db=Depends(get_db)):
    from .series_service import detail
    row=owned_entry(db,entry_id); series=db.get(Series,row.series_id) if row.series_id else None
    return {**view(row),"catalog":detail(db,series) if series else None}
