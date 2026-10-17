"""Queued Pixiv profiles and per-account authored/bookmarked catalogues."""
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select, union_all

from .auth import require_auth
from .database import get_db
from .integrations import get_connector
from .integrations.base import ConnectorError
from .library import build_item_view, iso, save_preview_cover
from .models import AuthorFeed, AuthorFeedEntry, AuthorProfile, Item, Job, SourceAccount, Subscription, utcnow
from .storage import resolve_asset_path
from . import queue_control

router = APIRouter(dependencies=[Depends(require_auth)])
KINDS = ("author_profile", "author_feed")


class OpenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url_or_id: str = Field(min_length=1, max_length=2048)
    account_id: str | None = Field(default=None, max_length=36)


class FeedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: Literal["works", "bookmarks"] = "works"
    category: Literal["illustration", "manga", "novel"] = "illustration"
    restrict: Literal["public", "private"] = "public"
    account_id: str | None = Field(default=None, max_length=36)
    more: bool = False


def checked_id(value):
    try:
        return get_connector("pixiv")._positive_id(value)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from None


def account(db, ident=None, required=False):
    from .pixiv_accounts import resolve_account_id
    try:
        ident = resolve_account_id(db, ident)
    except ConnectorError as exc:
        raise HTTPException(409, str(exc)) from None
    row = db.get(SourceAccount, ident) if ident else db.scalar(select(SourceAccount).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at, SourceAccount.id).limit(1))
    if row and row.platform != "pixiv":
        raise HTTPException(400, "账号与 Pixiv 平台不匹配")
    if not row and (required or ident):
        raise HTTPException(409, "请先在网页内容来源中登录 Pixiv 账号")
    return row


def scope_key(author_id, value, account_id):
    return hashlib.sha256(json.dumps([author_id, value.section, value.category, value.restrict, account_id]).encode()).hexdigest()


def public_job(job):
    return {"id": job.id, "status": job.status, "progress": job.progress, "error": job.error,
            "author_id": (job.result or {}).get("author_id"), "feed_id": (job.result or {}).get("feed_id")}


def enqueue(db, kind, payload, identity):
    existing = db.scalar(select(Job).where(Job.kind == kind, Job.status.in_(("queued", "running")),
        Job.payload["identity"].as_string() == identity).with_for_update())
    if existing:
        return existing
    if db.scalar(select(func.count(Job.id)).where(Job.kind.in_(KINDS), Job.status.in_(("queued", "running")))) >= 100:
        raise HTTPException(429, "作者目录任务过多，请等待队列处理")
    job = Job(kind=kind, status="queued", payload={**payload, "identity": identity})
    db.add(job)
    db.flush()
    return job


@router.get("/pixiv/authors")
def list_authors(q: str = Query("", max_length=200), page: int = Query(1, ge=1), db=Depends(get_db)):
    profiles = select(AuthorProfile.remote_id.label("remote_id"), AuthorProfile.name.label("name"))
    follows = select(Subscription.target.label("remote_id"), func.max(Subscription.label).label("name")).where(
        Subscription.platform == "pixiv", Subscription.target_type == "author",
        ~select(AuthorProfile.remote_id).where(AuthorProfile.remote_id == Subscription.target).exists()).group_by(Subscription.target)
    catalog = union_all(profiles, follows).subquery()
    query = select(catalog)
    if q.strip():
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(or_(catalog.c.name.ilike(f"%{term}%", escape="\\"), catalog.c.remote_id.ilike(f"%{term}%", escape="\\")))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {"items": [dict(row._mapping) for row in db.execute(query.order_by(catalog.c.name, catalog.c.remote_id).offset((page - 1) * 30).limit(30))], "total": total, "page": page}


@router.post("/pixiv/authors/open")
def open_author(value: OpenInput, db=Depends(get_db)):
    connector = get_connector("pixiv")
    try:
        target = connector.normalize_author(value.url_or_id)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from None
    queue_control.lock_control(db)
    selected = account(db, value.account_id, required=True)
    from .jobs import account_credentials
    connector.credentials = account_credentials(db, "pixiv", selected.id)
    connector.validate_credentials()
    job = enqueue(db, "author_profile", {"target": target["remote_id"], "account_id": selected.id}, selected.id + ":" + target["remote_id"])
    db.commit()
    return {**target, "job": public_job(job)}


@router.get("/pixiv/tasks/{job_id}")
def task_status(job_id: str, db=Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None or job.kind not in KINDS:
        raise HTTPException(404, "作者任务不存在")
    return public_job(job)


@router.get("/pixiv/authors/{author_id}")
def author_detail(author_id: str, account_id: str | None = None, db=Depends(get_db)):
    ident = checked_id(author_id)
    profile = db.get(AuthorProfile, ident)
    selected = account(db, account_id)
    stats = profile.statistics or {} if profile else {}
    return {"remote_id": ident, "name": profile.name if profile and profile.name else "Pixiv 作者 " + ident,
            "description": profile.description if profile else "", "source_url": "https://www.pixiv.net/users/" + ident,
            "avatar_url": f"/api/pixiv/authors/{ident}/avatar" if profile and profile.avatar else None,
            "synced_at": iso(profile.synced_at) if profile else None,
            "statistics": {key: value for key, value in stats.items() if not key.startswith("_")},
            "can_read_private": bool(selected and selected.id in stats.get("_owned_accounts", [])),
            "download_settings": author_download_settings(db, ident)}


def author_download_settings(db, ident):
    from .subscription_policy import flags
    row = db.scalar(select(Subscription).where(Subscription.platform == "pixiv", Subscription.target_type == "author",
                                              Subscription.target == ident).order_by(Subscription.created_at, Subscription.id))
    return {"download_works": False, "download_bookmarks": False, "subscribed": False} if row is None else {**flags(row), "subscribed": True}


class AuthorDownloadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    download_works: bool
    download_bookmarks: bool


@router.put("/pixiv/authors/{author_id}/download-settings")
def save_author_downloads(author_id: str, value: AuthorDownloadInput, db=Depends(get_db)):
    ident = checked_id(author_id)
    selected = account(db, required=True)
    queue_control.lock_control(db)
    rows = db.scalars(select(Subscription).where(Subscription.platform == "pixiv", Subscription.target_type == "author",
                                                Subscription.target == ident).with_for_update()).all()
    if not rows:
        profile = db.get(AuthorProfile, ident)
        row = Subscription(platform="pixiv", target_type="author", target=ident,
            label=profile.name if profile and profile.name else "Pixiv 作者 " + ident, enabled=True,
            account_id=selected.id, content_scope="all", auto_download=False)
        db.add(row); rows = [row]
    for row in rows:
        row.download_works, row.download_bookmarks = value.download_works, value.download_bookmarks
        if row.enabled: row.next_check_at = None
    db.commit();return author_download_settings(db,ident)


@router.get("/pixiv/authors/{author_id}/avatar")
def author_avatar(author_id: str, db=Depends(get_db)):
    profile = db.get(AuthorProfile, checked_id(author_id))
    if not profile or not profile.avatar:
        raise HTTPException(404, "作者头像尚未保存")
    path = resolve_asset_path(profile.avatar["relative_path"])
    if not path.is_file() or path.stat().st_size != profile.avatar["size_bytes"]:
        raise HTTPException(503, "作者头像文件缺失，请检查存储")
    return FileResponse(path, media_type="image/webp", headers={"Content-Security-Policy": "default-src 'none'; sandbox"})


def find_feed(db, ident, value):
    selected = account(db, value.account_id)
    return db.scalar(select(AuthorFeed).where(AuthorFeed.scope_key == scope_key(ident, value, selected.id))) if selected else None


@router.get("/pixiv/authors/{author_id}/feed")
def get_feed(author_id: str, section: Literal["works", "bookmarks"] = "works", category: Literal["illustration", "manga", "novel"] = "illustration",
             restrict: Literal["public", "private"] = "public", account_id: str | None = None, page: int = Query(1, ge=1), db=Depends(get_db)):
    feed = find_feed(db, checked_id(author_id), FeedInput(section=section, category=category, restrict=restrict, account_id=account_id))
    if feed is None:
        return {"items": [], "total": 0, "page": page, "has_more": True, "complete": False, "revision": 0, "unavailable": 0, "job": None}
    query = select(Item).join(AuthorFeedEntry, AuthorFeedEntry.item_id == Item.id).where(AuthorFeedEntry.feed_id == feed.id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(AuthorFeedEntry.generation.desc(), AuthorFeedEntry.position, Item.id).offset((page - 1) * 24).limit(24)).all()
    job = db.scalar(select(Job).where(Job.kind == "author_feed", Job.payload["feed_id"].as_string() == feed.id)
        .order_by(Job.status.in_(("queued", "running")).desc(), Job.updated_at.desc(), Job.id.desc()).limit(1))
    return {"items": [build_item_view(item) for item in items], "total": total, "page": page,
            "has_more": feed.next_cursor is not None or feed.generation == 0, "complete": feed.complete,
            "revision": feed.revision, "generation": feed.generation, "unavailable": feed.unavailable, "job": public_job(job) if job else None}


@router.post("/pixiv/authors/{author_id}/feed")
def request_feed(author_id: str, value: FeedInput, db=Depends(get_db)):
    ident = checked_id(author_id)
    if value.section == "works" and value.restrict != "public":
        raise HTTPException(400, "作品目录无需选择私密收藏")
    queue_control.lock_control(db)
    selected = account(db, value.account_id, required=True)
    profile = db.get(AuthorProfile, ident)
    if profile is None:
        profile = AuthorProfile(remote_id=ident)
        db.add(profile)
        db.flush()
    if value.restrict == "private" and selected.id not in (profile.statistics or {}).get("_owned_accounts", []):
        raise HTTPException(403, "请先读取本人主页，只能访问当前登录账号自己的私密收藏")
    key = scope_key(ident, value, selected.id)
    feed = db.scalar(select(AuthorFeed).where(AuthorFeed.scope_key == key).with_for_update())
    if feed is None:
        feed = AuthorFeed(scope_key=key, author_id=ident, account_id=selected.id, section=value.section,
                          category=value.category, restrict=value.restrict)
        db.add(feed)
        db.flush()
    if value.more and feed.generation > 0 and feed.next_cursor is None:
        return {"job": None, "cached": True}
    job = enqueue(db, "author_feed", {"feed_id": feed.id, "account_id": selected.id,
        "more": value.more, "revision": feed.revision}, feed.id)
    db.commit()
    return {"job": public_job(job), "cached": False}


def sync_profile(db, payload, report, guard):
    from .jobs import account_credentials
    # Validate the directory owner before using an assigned author-profile account.
    original = account_credentials(db, "pixiv", payload["account_id"], pinned=True, require_retry=False)
    assigned = payload.get("check_account_id") or payload.get("execution_account_id")
    purpose = "metadata" if str(payload["target"]).lower() == "me" else "checks"
    credentials = account_credentials(db, "pixiv", assigned or payload["account_id"], purpose=purpose, pinned=bool(assigned))
    connector = get_connector("pixiv", credentials)
    db.commit()
    data = connector.fetch_author(payload["target"], checkpoint=report)
    avatar = save_preview_cover(data["avatar"], "pixiv") if data.get("avatar") else None
    queue_control.lock_control(db)
    if db.get(SourceAccount, payload["account_id"]) is None:
        raise ConnectorError("Pixiv 账号已删除，资料未保存")
    profile = db.get(AuthorProfile, data["remote_id"])
    if profile is None:
        profile = AuthorProfile(remote_id=data["remote_id"])
        db.add(profile)
    owners = set((profile.statistics or {}).get("_owned_accounts", []))
    if not assigned or assigned == payload["account_id"]:
        if data["is_self"]:
            owners.add(payload["account_id"])
        else:
            owners.discard(payload["account_id"])
    profile.name, profile.description = data["name"], data["description"]
    profile.statistics = {**data["statistics"], "_owned_accounts": sorted(owners)}
    profile.synced_at = utcnow()
    if avatar:
        profile.avatar = avatar
    guard(db)
    db.commit()
    return {"author_id": profile.remote_id, "message": "作者资料已保存"}


def sync_feed(db, payload, report, guard):
    from .jobs import account_credentials, merge_remote_item, enqueue_import
    from .pixiv_favorites import request_account_id
    feed = db.get(AuthorFeed, payload["feed_id"])
    if feed is None:
        raise ConnectorError("作者目录或账号已被删除")
    if feed.revision != payload["revision"]:
        raise ConnectorError("目录已更新，请重新加载")
    ident, section, category, restrict = feed.author_id, feed.section, feed.category, feed.restrict
    cursor = feed.next_cursor if payload["more"] and feed.generation > 0 else None
    generation = feed.generation if payload["more"] and feed.generation > 0 else feed.generation + 1
    history = list(feed.seen_cursors) if payload["more"] else []
    raw_count = feed.raw_count if payload["more"] else 0
    unavailable = feed.unavailable if payload["more"] else 0
    known = dict(db.execute(select(Item.source_id, Item.id).join(AuthorFeedEntry, AuthorFeedEntry.item_id == Item.id).where(AuthorFeedEntry.feed_id == feed.id)).all())
    original = account_credentials(db, "pixiv", payload["account_id"], pinned=True)
    assigned = payload.get("check_account_id") or payload.get("execution_account_id")
    if restrict == "private" and assigned and assigned != payload["account_id"]:
        raise ConnectorError("私密收藏必须由原账号读取，请重试任务")
    connector = get_connector("pixiv", account_credentials(db, "pixiv", assigned, purpose="metadata", pinned=True) if assigned else original)
    bookmark_owner = request_account_id(db, assigned or payload["account_id"], pinned=True)
    db.commit()
    works = []
    # Bookmark image endpoints mix illustration and manga. Advance through at
    # most three raw pages per click, retaining the cursor even for empty tabs.
    for _ in range(3):
        data = connector.fetch_author_page(ident, section, category, restrict, cursor, checkpoint=report)
        works.extend(data["works"])
        raw_count += data["raw_count"]
        unavailable += data["unavailable"]
        cursor = data["next_cursor"]
        if cursor is not None:
            marker = hashlib.sha256(json.dumps(cursor, sort_keys=True).encode()).hexdigest()
            if marker in history:
                raise ConnectorError("Pixiv 分页返回重复游标，旧目录已保留")
            history.append(marker)
            if len(history) > 5000:
                raise ConnectorError("目录已达到单次扫描 5000 页限制，已有内容保留")
        if works or cursor is None:
            break
    report(85)
    queue_control.lock_control(db)
    current = db.scalar(select(AuthorFeed).where(AuthorFeed.id == payload["feed_id"]).with_for_update().execution_options(populate_existing=True))
    if current is None or current.revision != payload["revision"]:
        raise ConnectorError("目录已删除或更新，结果未保存")
    alive = set(db.scalars(select(Item.id).where(Item.id.in_(list(known.values()))).with_for_update())) if known else set()
    deleted = {remote_id for remote_id, local_id in known.items() if local_id not in alive}
    from .subscription_policy import flags
    subscription = db.scalar(select(Subscription).where(Subscription.platform == "pixiv", Subscription.target_type == "author",
                                                      Subscription.target == ident).order_by(Subscription.created_at, Subscription.id))
    download = False
    if subscription and subscription.enabled and (subscription.content_scope == "all" or
            (subscription.content_scope == "novels") == (current.category == "novel")):
        download = flags(subscription)["download_works" if current.section == "works" else "download_bookmarks"]
        if current.section == "bookmarks" and current.restrict != "public": download = False
    queued = set()
    for index, work in enumerate(works):
        if work.remote_id in deleted:
            unavailable += 1
            continue
        item = merge_remote_item(db, "pixiv", work, bookmark_account_id=bookmark_owner)
        entry = db.get(AuthorFeedEntry, (current.id, item.id))
        if entry is None:
            entry = AuthorFeedEntry(feed_id=current.id, item_id=item.id, generation=generation, position=raw_count - len(works) + index)
            db.add(entry)
        elif entry.generation != generation:
            entry.generation, entry.position = generation, raw_count - len(works) + index
        if not item.assets and (not item.preview_cover or download):
            cover_job = enqueue_import(db, {"platform": "pixiv", "remote_id": work.remote_id, "item_id": item.id,
                "account_id": payload["account_id"], "download": download}, commit=False, locked=True)
            queued.add(cover_job.id)
    complete = cursor is None and unavailable == 0
    if complete:
        db.execute(delete(AuthorFeedEntry).where(AuthorFeedEntry.feed_id == current.id, AuthorFeedEntry.generation != generation))
    current.generation, current.revision = generation, current.revision + 1
    current.next_cursor, current.seen_cursors = cursor, history
    current.raw_count, current.unavailable, current.complete = raw_count, unavailable, complete
    guard(db)
    db.commit()
    return {"author_id": ident, "feed_id": current.id, "queued_covers": len(queued),
            "has_more": cursor is not None, "complete": complete, "message": "目录已保存，缺失正文已进入自动下载队列" if download else "目录已保存，缺失封面已排队；正文需另行下载"}
