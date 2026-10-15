"""Explicit Pixiv discovery identity and an uncapped download-account pool."""
from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .auth import require_admin_cookie
from .database import get_db
from .integrations.base import ConnectorError
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import AuthorFeed, Job, NetworkSetting, SourceAccount

KEY = "pixiv_account_roles"
AccountID = Annotated[str, Field(min_length=1, max_length=36)]
router = APIRouter(prefix="/sources/pixiv/account-roles", dependencies=[Depends(require_admin_cookie)])


class RolesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    main_account_id: AccountID | None = None
    content_account_id: AccountID | None = None
    content_account_ids: list[AccountID] | None = None
    detail_account_ids: list[AccountID] = Field(default_factory=list)
    check_account_ids: list[AccountID] = Field(default_factory=list)
    account_limits: dict[AccountID, Annotated[int, Field(strict=True, ge=1, le=8)]] = Field(default_factory=dict)


def normalized(value):
    config = value.model_dump()
    pool = config["content_account_ids"]
    if pool is None:
        pool = [config["content_account_id"]] if config["content_account_id"] else []
    if len(set(pool)) != len(pool):
        raise ValueError("下载账号不能重复选择")
    config["content_account_ids"] = pool
    config["content_account_id"] = pool[0] if pool else None
    for key in ("detail_account_ids", "check_account_ids"):
        if len(set(config[key])) != len(config[key]):
            raise ValueError("账号功能不能重复选择")
    return config


def read_config(db):
    row = db.get(NetworkSetting, KEY)
    try:
        config = normalized(RolesInput.model_validate(decrypt_credentials(row.value_encrypted) if row else {"enabled": False}))
        if config["enabled"] and (not config["main_account_id"] or not (config["content_account_ids"] or config["detail_account_ids"] or config["check_account_ids"])
                                  or config["main_account_id"] in config["content_account_ids"]):
            raise ValueError("Invalid Pixiv roles")
        return config
    except (ConnectorError, ValueError, TypeError):
        raise ConnectorError("Pixiv 账号分工配置无法读取，请在下载账号页重新保存") from None


def resolve_account_id(db, requested=None, *, purpose="metadata", pinned=False):
    if purpose not in ("metadata", "content", "details", "checks"):
        raise ValueError("Unsupported Pixiv account purpose")
    config = read_config(db)
    if not config["enabled"]:
        return requested
    if purpose == "content":
        ident = requested if pinned else config["content_account_id"]
        if pinned and ident not in config["content_account_ids"]:
            raise ConnectorError("Pixiv 下载账号已退出账号池，请重试任务以重新分配；未改用主账号")
        label = "副账号（正文下载）"
    elif purpose in ("details", "checks"):
        pool = config["detail_account_ids" if purpose == "details" else "check_account_ids"] or [config["main_account_id"]]
        ident = requested if pinned else pool[0]
        # Main remains the identity for private directories and follows.
        if ident not in pool and not (purpose == "checks" and ident == config["main_account_id"]):
            raise ConnectorError("Pixiv 账号功能已更改，请重试任务以重新分配")
        label = "详情解析账号" if purpose == "details" else "作者检查账号"
    else:
        ident, label = config["main_account_id"], "主账号（检索资料）"
        if pinned and requested != ident:
            raise ConnectorError("Pixiv 主账号分工已更改，请重新获取作者主页或收藏目录；旧账号目录未改写")
    account = db.get(SourceAccount, ident) if ident else None
    if not account or account.platform != "pixiv":
        raise ConnectorError(f"Pixiv {label}未配置或已移除，请在下载账号页重新设置；未切换至其他账号")
    return ident


def assignment_state(db):
    try:
        config = read_config(db)
    except ConnectorError:
        return None
    fields = ("download_account_id", "details_account_id", "check_account_id", "execution_account_id")
    busy = Counter()
    for values in db.execute(select(*(Job.payload[key].as_string() for key in fields)).where(Job.status == "running")):
        busy.update(set(value for value in values if value))
    records = set(db.scalars(select(SourceAccount.id).where(SourceAccount.platform == "pixiv")))
    return config, busy, records


def assignment_filter(state):
    """Skip saturated identities in SQL, including backlogs beyond one page."""
    from sqlalchemy import and_, not_, or_
    if state is None:
        return True
    config, busy, records = state
    full = [ident for ident, count in busy.items() if count >= config["account_limits"].get(ident, 2)]
    conditions = []
    for field in ("download_account_id", "details_account_id", "check_account_id", "execution_account_id"):
        value = Job.payload[field].as_string()
        conditions.append(or_(value.is_(None), value.not_in(full)))
    if config["enabled"]:
        from .queue_control import lane_expression
        pixiv_checks = or_(Job.kind.in_(("author_profile", "author_feed", "series_sync")),
                           and_(Job.kind == "source_sync", Job.payload["platform"].as_string() == "pixiv"))
        private = or_(and_(Job.kind == "source_sync", Job.payload["subscription_id"].as_string().is_(None)),
                      and_(Job.kind == "author_profile", Job.payload["target"].as_string() == "me"),
                      and_(Job.kind == "author_feed", select(AuthorFeed.id).where(
                          AuthorFeed.id == Job.payload["feed_id"].as_string(), AuthorFeed.restrict == "private").exists()))
        private = func.coalesce(private, False)
        check_pool = config["check_account_ids"] or [config["main_account_id"]]
        for field, pool, need in (
            ("download_account_id", config["content_account_ids"], and_(lane_expression() == "download", Job.payload["platform"].as_string() == "pixiv")),
            ("details_account_id", config["detail_account_ids"] or [config["main_account_id"]], and_(Job.kind == "import", Job.payload["platform"].as_string() == "pixiv")),
            ("check_account_id", check_pool, and_(pixiv_checks, not_(private))),
            ("check_account_id", [config["main_account_id"]], and_(pixiv_checks, private)),
        ):
            if pool and all(ident in full for ident in pool):
                conditions.append(or_(Job.payload[field].as_string().is_not(None), not_(func.coalesce(need, False))))
    return and_(*conditions)


def assign_download_account(db, job, control, *, state=None):
    """Caller holds QueueControl's lock. Prefer least busy, rotate ties.

    Pause/restart retain the assignment; only an explicit user retry clears it.
    An upstream failure never rotates identities.
    """
    if job.payload.get("platform") != "pixiv" and job.kind not in ("author_profile", "author_feed", "series_sync"):
        return True
    state = state if state is not None else assignment_state(db)
    if state is None:
        # Claim the job normally so processing records an actionable failure;
        # the credential resolver will reject the same unreadable config.
        return True
    config, busy, records = state
    if not config["enabled"]:
        requested = job.payload.get("account_id")
        if not requested:
            requested = db.scalar(select(SourceAccount.id).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at))
        pools = {"execution_account_id": [requested]} if requested else {}
    else:
        pools = {}
        if job.kind in ("import", "import_content"):
            if job.kind == "import_content" or job.payload.get("download", True):
                pools["download_account_id"] = config["content_account_ids"]
            if job.kind == "import":
                pools["details_account_id"] = config["detail_account_ids"] or [config["main_account_id"]]
        else:
            private = job.kind == "source_sync" and not job.payload.get("subscription_id")
            private = private or job.kind == "author_profile" and str(job.payload.get("target", "")).lower() == "me"
            if job.kind == "author_feed":
                feed = db.get(AuthorFeed, job.payload.get("feed_id"))
                private = feed is None or feed.restrict == "private"
            pools["check_account_id"] = [config["main_account_id"]] if private else config["check_account_ids"] or [config["main_account_id"]]
    fields = ("download_account_id", "details_account_id", "check_account_id", "execution_account_id")
    payload = dict(job.payload)
    for key in fields:
        if key not in pools:
            payload.pop(key, None)
    for field, pool in pools.items():
        # Invalid configuration is claimed and fails visibly in the resolver.
        if not pool:
            continue
        pinned = payload.get(field)
        candidates = [pinned] if pinned else [ident for ident in pool if ident in records] or pool
        available = [ident for ident in candidates if busy[ident] < config["account_limits"].get(ident, 2)]
        if not available:
            return False
        start = control.download_account_cursor % len(available)
        chosen = min(available[start:] + available[:start], key=lambda ident: busy[ident])
        control.download_account_cursor += 1
        payload[field] = chosen
    job.payload = payload
    return True


def account_status(db, ident, records=None):
    row = (records.get(ident) if records is not None else db.get(SourceAccount, ident)) if ident else None
    return {"id": ident, "label": row.label if row and row.platform == "pixiv" else None,
            "status": "unconfigured" if not ident else "missing" if not row or row.platform != "pixiv" else "verified" if row.verified_at else "unverified",
            "verified_at": row.verified_at.isoformat() + "Z" if row and row.platform == "pixiv" and row.verified_at else None}


def public_config(db):
    config = read_config(db)
    records = {row.id: row for row in db.scalars(select(SourceAccount).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at, SourceAccount.id))}
    known = list(records)
    ids = list(dict.fromkeys(known + config["content_account_ids"] + config["detail_account_ids"] + config["check_account_ids"] + ([config["main_account_id"]] if config["main_account_id"] else [])))
    counts = Counter()
    fields = ("download_account_id", "details_account_id", "check_account_id", "execution_account_id")
    for status, *assigned in db.execute(select(Job.status, *(Job.payload[key].as_string() for key in fields))):
        counts.update((ident, status) for ident in set(assigned) if ident)
    statuses = {ident: account_status(db, ident, records) for ident in ids}
    accounts = [{**statuses[ident], "in_pool": ident in config["content_account_ids"],
                 "concurrency": config["account_limits"].get(ident, 2),
                 "running": counts.get((ident, "running"), 0), "completed": counts.get((ident, "completed"), 0),
                 "failed": counts.get((ident, "failed"), 0)} for ident in known]
    empty = account_status(db, None)
    return {**config, "main": statuses.get(config["main_account_id"], empty),
            "content": statuses.get(config["content_account_id"], empty),
            "contents": [statuses[ident] for ident in config["content_account_ids"]], "download_accounts": accounts,
            "missing_account_ids": [ident for ident in ids if ident not in records]}


@router.get("")
def get_roles(db=Depends(get_db)):
    try:
        return public_config(db)
    except ConnectorError as exc:
        raise HTTPException(503, str(exc)) from None


@router.put("")
def set_roles(value: RolesInput, db=Depends(get_db)):
    from .queue_control import lock_control
    lock_control(db)
    try:
        config = normalized(value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if db.get(NetworkSetting, KEY):
        try:
            previous = read_config(db)
        except ConnectorError:
            previous = {}  # A valid replacement can repair corrupt settings.
        for key in ("detail_account_ids", "check_account_ids", "account_limits"):
            if key not in value.model_fields_set and key in previous:
                config[key] = previous[key]
    pool, main_id = config["content_account_ids"], config["main_account_id"]
    if value.enabled and (not main_id or not (pool or config["detail_account_ids"] or config["check_account_ids"])):
        raise HTTPException(400, "启用分工需要选择主账号和至少一种副账号功能")
    if main_id and main_id in pool:
        raise HTTPException(400, "主账号与下载副账号必须选择不同账号")
    ids = sorted(set(pool + config["detail_account_ids"] + config["check_account_ids"] + list(config["account_limits"]) + ([main_id] if main_id else [])))
    accounts = {row.id: row for row in db.scalars(select(SourceAccount).where(SourceAccount.id.in_(ids)).order_by(SourceAccount.id).with_for_update())} if ids else {}
    missing = [ident for ident in ids if ident not in accounts or accounts[ident].platform != "pixiv"]
    if missing and value.enabled:
        raise HTTPException(400, "所选 Pixiv 账号不存在，请刷新账号列表")
    if not value.enabled:
        config["main_account_id"] = main_id if main_id not in missing else None
        pool = [ident for ident in pool if ident not in missing]
        config["content_account_ids"], config["content_account_id"] = pool, pool[0] if pool else None
        for key in ("detail_account_ids", "check_account_ids"):
            config[key] = [ident for ident in config[key] if ident not in missing]
        config["account_limits"] = {ident: limit for ident, limit in config["account_limits"].items() if ident not in missing}
    if value.enabled:
        tokens, users = set(), set()
        selected = set(pool + config["detail_account_ids"] + config["check_account_ids"] + [main_id])
        try:
            for ident in sorted(selected):
                credentials = decrypt_credentials(accounts[ident].credentials_encrypted)
                token, user = credentials.get("refresh_token"), str(credentials.get("user_id") or "")
                if token and token in tokens or user and user in users:
                    raise HTTPException(400, "多个记录对应同一个 Pixiv 账号，请切换账号重新授权")
                if token: tokens.add(token)
                if user: users.add(user)
        except ConnectorError:
            raise HTTPException(400, "所选账号的会话无法读取，请重新登录") from None
    encrypted = encrypt_credentials(config)
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(NetworkSetting).values(key=KEY, value_encrypted=encrypted)
        db.execute(statement.on_conflict_do_update(index_elements=["key"], set_={"value_encrypted": encrypted}))
    else:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(NetworkSetting).values(key=KEY, value_encrypted=encrypted)
        db.execute(statement.on_duplicate_key_update(value_encrypted=encrypted))
    db.commit()
    return public_config(db)
