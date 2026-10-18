"""Explicit Pixiv discovery identity and an uncapped download-account pool."""
from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, union_all

from .auth import require_admin_cookie
from .database import get_db
from .integrations.base import ConnectorError
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import Job, NetworkSetting, SourceAccount

KEY = "pixiv_account_roles"
AccountID = Annotated[str, Field(min_length=1, max_length=36)]
router = APIRouter(prefix="/sources/pixiv/account-roles", dependencies=[Depends(require_admin_cookie)])


def is_novel_payload(payload):
    """Accept canonical IDs and legacy direct links without making a request."""
    from .integrations.pixiv import PixivConnector
    value = payload.get("remote_id")
    if not isinstance(value, str):
        return False
    try:
        return PixivConnector({}).normalize_id(value).startswith("novel:")
    except (ConnectorError, ValueError):
        return False


class RolesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    main_account_id: AccountID | None = None
    content_account_id: AccountID | None = None
    content_account_ids: list[AccountID] | None = None
    manual_account_ids: list[AccountID] = Field(default_factory=list)
    retry_account_ids: list[AccountID] = Field(default_factory=list)
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
    for key in ("manual_account_ids", "retry_account_ids", "detail_account_ids", "check_account_ids"):
        if len(set(config[key])) != len(config[key]):
            raise ValueError("账号功能不能重复选择")
    return config


def read_config(db, *, for_update=False):
    row = db.scalar(select(NetworkSetting).where(NetworkSetting.key == KEY).with_for_update()
        .execution_options(populate_existing=True)) if for_update else db.get(NetworkSetting, KEY)
    try:
        config = normalized(RolesInput.model_validate(decrypt_credentials(row.value_encrypted) if row else {"enabled": False}))
        if config["enabled"] and (not config["main_account_id"] or not (config["content_account_ids"] or config["manual_account_ids"] or config["detail_account_ids"] or config["check_account_ids"])
                                  or config["main_account_id"] in config["content_account_ids"]):
            raise ValueError("Invalid Pixiv roles")
        return config
    except (ConnectorError, ValueError, TypeError):
        raise ConnectorError("Pixiv 账号分工配置无法读取，请在下载账号页重新保存") from None


def resolve_account_id(db, requested=None, *, purpose="metadata", pinned=False, require_retry=True):
    if purpose not in ("metadata", "content", "manual", "details", "checks"):
        raise ValueError("Unsupported Pixiv account purpose")
    config = read_config(db)
    from .queue_control import current_lane
    retry = require_retry and current_lane() == "retry"
    if not config["enabled"]:
        if retry:
            ident = requested or db.scalar(select(SourceAccount.id).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at))
            if not ident or ident not in config["retry_account_ids"]:
                raise ConnectorError("Pixiv 执行账号未获得重试权限，请在账号管理中勾选「重试」")
            return ident
        return requested
    if purpose == "content":
        pool = config["content_account_ids"]
        if retry:
            pool = [ident for ident in pool if ident in config["retry_account_ids"]]
        ident = requested if pinned else pool[0] if pool else None
        if pinned and ident not in config["content_account_ids"]:
            raise ConnectorError("Pixiv 下载账号已退出账号池，请重试任务以重新分配；未改用主账号")
        label = "图片下载账号"
    elif purpose in ("manual", "details", "checks"):
        pool = config[{"manual": "manual_account_ids", "details": "detail_account_ids", "checks": "check_account_ids"}[purpose]]
        if retry:
            pool = [ident for ident in pool if ident in config["retry_account_ids"]]
        ident = requested if pinned else pool[0] if pool else None
        if pinned and ident not in pool:
            raise ConnectorError("Pixiv 账号功能已更改，请重试任务以重新分配")
        label = {"manual": "手动下载账号", "details": "作品内容账号", "checks": "作者资料账号"}[purpose]
    else:
        ident, label = config["main_account_id"], "主账号（检索资料）"
        if pinned and requested != ident:
            raise ConnectorError("Pixiv 主账号分工已更改，请重新获取作者主页或收藏目录；旧账号目录未改写")
    if retry and (not ident or ident not in config["retry_account_ids"]):
        raise ConnectorError(f"Pixiv {label}未获得重试权限，请为具备原任务功能的账号勾选「重试」")
    account = db.get(SourceAccount, ident) if ident else None
    if not account or account.platform != "pixiv":
        raise ConnectorError(f"Pixiv {label}未配置或已移除，请在下载账号页重新设置；未切换至其他账号")
    return ident


def _assignment_pools(db, job, config):
    if not config["enabled"]:
        requested = job.payload.get("account_id") or db.scalar(
            select(SourceAccount.id).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at))
        return {"execution_account_id": [requested] if requested else []}
    pools = {}
    if job.kind in ("import", "import_content"):
        novel = is_novel_payload(job.payload)
        body = job.kind == "import_content" or job.payload.get("download", True)
        manual = body and job.payload.get("download_origin") == "manual"
        if manual:
            pools["download_account_id"] = config["manual_account_ids"]
        elif not novel and body:
            pools["download_account_id"] = config["content_account_ids"]
        if (job.kind == "import" or novel) and not (manual and novel):
            pools["details_account_id"] = config["detail_account_ids"]
    else:
        profile = job.kind == "author_profile" and str(job.payload.get("target", "")).lower() != "me"
        pools["check_account_id"] = config["check_account_ids"] if profile else [config["main_account_id"]]
    return pools


def validate_retry_assignment(db, job, *, for_update=False):
    if job.payload.get("retry_queue") is not True or (
        job.payload.get("platform") != "pixiv" and job.kind not in ("author_profile", "author_feed", "series_sync")
    ):
        return
    config = read_config(db, for_update=for_update)
    labels = {"execution_account_id": "执行", "download_account_id": "下载",
              "details_account_id": "作品详情与正文", "check_account_id": "作者资料／检索"}
    for field, pool in _assignment_pools(db, job, config).items():
        ident = job.payload.get(field)
        if not ident or ident not in pool or ident not in config["retry_account_ids"]:
            raise ConnectorError(f"Pixiv {labels[field]}账号未同时获得原任务功能与重试权限，请在账号管理中设置后重试")
        account = db.scalar(select(SourceAccount).where(SourceAccount.id == ident).with_for_update()
            .execution_options(populate_existing=True)) if for_update else db.get(SourceAccount, ident)
        if not account or account.platform != "pixiv":
            raise ConnectorError("Pixiv 重试账号已移除，请重新配置后重试")


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
    from .queue_control import download_requested
    if state is None:
        return True
    config, busy, records = state
    full = [ident for ident, count in busy.items() if count >= config["account_limits"].get(ident, 2)]
    pixiv = Job.payload["platform"].as_string() == "pixiv"
    remote_id = func.trim(func.coalesce(Job.payload["remote_id"].as_string(), ""))
    # Enqueueing stores canonical IDs. Also recognize old direct novel links.
    novel = or_(remote_id.like("novel:%"), remote_id.like("https://%/novel/show.php?%"))
    manual = func.coalesce(Job.payload["download_origin"].as_string(), "") == "manual"
    retry = func.coalesce(Job.payload["retry_queue"].as_boolean(), False)
    conditions = []
    for field in ("download_account_id", "details_account_id", "check_account_id", "execution_account_id"):
        value = Job.payload[field].as_string()
        available = or_(value.is_(None), value.not_in(full))
        # Missing retry permission is a visible configuration error, not a wait
        # for that identity's capacity (including retained paused assignments).
        available = or_(available, and_(retry, value.not_in(config["retry_account_ids"])))
        if config["enabled"] and field == "download_account_id":
            # Old paused novels can still carry an image identity. Assignment
            # releases it below; do not make them wait for an unused account.
            available = or_(and_(pixiv, Job.kind.in_(("import", "import_content")), novel, not_(manual)), available)
        conditions.append(available)
    if config["enabled"]:
        body = or_(Job.kind == "import_content", and_(Job.kind == "import", download_requested()))
        own_profile = and_(Job.kind == "author_profile", func.lower(func.coalesce(Job.payload["target"].as_string(), "")) == "me")
        discovery = or_(Job.kind.in_(("author_feed", "series_sync")), own_profile,
                        and_(Job.kind.in_(("source_sync", "pixiv_bookmark")), pixiv))
        author_profile = and_(Job.kind == "author_profile", not_(own_profile))
        for field, pool, need in (
            ("download_account_id", config["content_account_ids"], and_(pixiv, body, not_(novel), not_(manual))),
            ("download_account_id", config["manual_account_ids"], and_(pixiv, body, manual)),
            ("details_account_id", config["detail_account_ids"], and_(pixiv, not_(and_(body, manual, novel)),
                or_(Job.kind == "import", and_(Job.kind == "import_content", novel)))),
            ("check_account_id", config["check_account_ids"], author_profile),
            ("check_account_id", [config["main_account_id"]], discovery),
        ):
            for scope, candidates in ((not_(retry), pool), (retry, [ident for ident in pool if ident in config["retry_account_ids"]])):
                # An empty eligible pool must fail visibly; a busy pool waits.
                if candidates and all(ident in full for ident in candidates):
                    conditions.append(or_(Job.payload[field].as_string().is_not(None),
                        not_(and_(scope, func.coalesce(need, False)))))
    else:
        fallback = select(SourceAccount.id).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at).limit(1).scalar_subquery()
        identity = func.coalesce(func.nullif(Job.payload["execution_account_id"].as_string(), ""),
            func.nullif(Job.payload["account_id"].as_string(), ""), fallback)
        needs_account = and_(retry, or_(func.coalesce(pixiv, False), Job.kind.in_(("author_profile", "author_feed", "series_sync"))))
        conditions.append(or_(not_(needs_account), not_(func.coalesce(identity.in_(config["retry_account_ids"]), False)),
            identity.not_in(full)))
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
    pools = _assignment_pools(db, job, config)
    if job.payload.get("retry_queue") is True:
        pools = {field: [ident for ident in pool if ident in config["retry_account_ids"]] for field, pool in pools.items()}
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
    ids = list(dict.fromkeys(known + config["content_account_ids"] + config["manual_account_ids"] + config["retry_account_ids"] + config["detail_account_ids"] + config["check_account_ids"] + ([config["main_account_id"]] if config["main_account_id"] else [])))
    counts = Counter()
    fields = ("download_account_id", "details_account_id", "check_account_id", "execution_account_id")
    if known:
        # Deduplicated generated identities let each covering index aggregate
        # before UNION: only a few counts, not every historic task, are merged.
        columns = [getattr(Job, key if index == 0 else 'stats_' + key) for index, key in enumerate(fields)]
        assigned = union_all(*(select(col.label('account_id'), Job.status, func.count().label('count'))
            .where(col.in_(known), Job.status.in_(('running', 'completed', 'failed'))).group_by(col, Job.status) for col in columns)).subquery()
        for ident, status, count in db.execute(select(assigned.c.account_id, assigned.c.status, func.sum(assigned.c.count))
                                             .group_by(assigned.c.account_id, assigned.c.status)):
            counts[ident, status] = int(count)
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
        for key in ("manual_account_ids", "retry_account_ids", "detail_account_ids", "check_account_ids", "account_limits"):
            if key not in value.model_fields_set and key in previous:
                config[key] = previous[key]
    pool, main_id = config["content_account_ids"], config["main_account_id"]
    if value.enabled and (not main_id or not (pool or config["manual_account_ids"] or config["detail_account_ids"] or config["check_account_ids"])):
        raise HTTPException(400, "启用分工需要选择主账号和至少一种副账号功能")
    if main_id and main_id in pool:
        raise HTTPException(400, "主账号与下载副账号必须选择不同账号")
    ids = sorted(set(pool + config["manual_account_ids"] + config["retry_account_ids"] + config["detail_account_ids"] + config["check_account_ids"] + list(config["account_limits"]) + ([main_id] if main_id else [])))
    accounts = {row.id: row for row in db.scalars(select(SourceAccount).where(SourceAccount.id.in_(ids)).order_by(SourceAccount.id).with_for_update())} if ids else {}
    missing = [ident for ident in ids if ident not in accounts or accounts[ident].platform != "pixiv"]
    if missing and value.enabled:
        raise HTTPException(400, "所选 Pixiv 账号不存在，请刷新账号列表")
    if not value.enabled:
        config["main_account_id"] = main_id if main_id not in missing else None
        pool = [ident for ident in pool if ident not in missing]
        config["content_account_ids"], config["content_account_id"] = pool, pool[0] if pool else None
        for key in ("manual_account_ids", "retry_account_ids", "detail_account_ids", "check_account_ids"):
            config[key] = [ident for ident in config[key] if ident not in missing]
        config["account_limits"] = {ident: limit for ident, limit in config["account_limits"].items() if ident not in missing}
    if value.enabled:
        tokens, users = set(), set()
        selected = set(pool + config["manual_account_ids"] + config["retry_account_ids"] + config["detail_account_ids"] + config["check_account_ids"] + [main_id])
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
