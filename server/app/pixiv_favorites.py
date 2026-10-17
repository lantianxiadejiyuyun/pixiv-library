"""One-way import of the main Pixiv identity's positive bookmark observations."""
from sqlalchemy import select

from .integrations.base import ConnectorError
from .models import Item, ItemFavorite, Job, SourceAccount, utcnow


def main_account_id(db):
    from .pixiv_accounts import read_config
    config = read_config(db)
    return config["main_account_id"] if config["enabled"] else db.scalar(
        select(SourceAccount.id).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at))


def request_account_id(db, requested=None, *, purpose="metadata", pinned=False):
    from .pixiv_accounts import resolve_account_id
    ident = resolve_account_id(db, requested, purpose=purpose, pinned=pinned)
    return ident or db.scalar(select(SourceAccount.id).where(SourceAccount.platform == "pixiv").order_by(SourceAccount.created_at))


def add_local_favorite(db, item):
    """Never update existing favorite timestamps, groups, scores or remote state."""
    db.flush()
    values = {"item_id": item.id, "created_at": utcnow()}
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(ItemFavorite).values(**values).on_conflict_do_nothing(index_elements=["item_id"]))
    else:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(ItemFavorite).values(**values)
        db.execute(statement.on_duplicate_key_update(item_id=statement.inserted.item_id))
    db.expire(item, ["favorite"])


def observe(db, item, work, account_id, hint=None):
    if item.source != "pixiv":
        return
    from .pixiv_collection import policy, queue_download
    if item.favorite is not None and not policy(db).get('enabled'):
        return
    main = main_account_id(db)
    if not main:
        return
    state = getattr(work, "is_bookmarked", None)
    if isinstance(hint, dict) and hint.get("account_id") == main and type(hint.get("bookmarked")) is bool:
        state, account_id = hint["bookmarked"], main
    if account_id == main and type(state) is bool:
        if state:
            add_local_favorite(db, item)
            queue_download(db, item, main)
        return
    # An auxiliary identity's bookmark flag describes that identity, not the
    # library owner's. Verify it with the main identity on the checks lane.
    active = db.scalar(select(Job.id).where(Job.kind == "pixiv_bookmark", Job.status.in_(("queued", "running")),
        Job.payload["item_id"].as_string() == item.id, Job.payload["main_account_id"].as_string() == main))
    if active is None:
        db.add(Job(kind="pixiv_bookmark", status="queued", payload={"platform": "pixiv", "item_id": item.id,
            "remote_id": item.source_id, "account_id": main, "main_account_id": main}))


def process(db, payload, report, guard):
    from . import queue_control
    from .jobs import account_credentials, get_connector
    item = db.get(Item, payload["item_id"])
    if item is None or item.source != "pixiv" or item.source_id != payload["remote_id"]:
        return {"skipped": True, "message": "作品已移除，收藏检查已跳过"}
    from .pixiv_collection import policy, queue_download
    if item.favorite is not None and not policy(db).get('enabled'):
        return {"item_id": item.id, "is_favorite": True, "message": "本地已收藏，原分组与评分保持"}
    main = main_account_id(db)
    if main != payload.get("main_account_id"):
        raise ConnectorError("Pixiv 主账号已更改，请重新采集作品后检查收藏；本地收藏保持")
    requested = payload.get("check_account_id") or payload.get("execution_account_id") or main
    connector = get_connector("pixiv", account_credentials(db, "pixiv", requested, purpose="metadata", pinned=True))
    db.commit()
    work = connector.fetch(payload["remote_id"], download=False, include_preview=False, checkpoint=report)
    if work.remote_id != payload["remote_id"] or type(getattr(work, "is_bookmarked", None)) is not bool:
        raise ConnectorError("Pixiv 未返回匹配作品的明确收藏状态，本地收藏保持")
    db.rollback()
    queue_control.lock_control(db)
    if main_account_id(db) != main:
        raise ConnectorError("Pixiv 主账号已更改，未使用旧账号的收藏状态")
    item = db.scalar(select(Item).where(Item.id == payload["item_id"], Item.source == "pixiv",
        Item.source_id == payload["remote_id"]).with_for_update().execution_options(populate_existing=True))
    if item is None:
        return {"skipped": True, "message": "作品已移除，收藏检查已跳过"}
    if work.is_bookmarked:
        add_local_favorite(db, item)
        queue_download(db, item, main)
    guard(db)
    db.commit()
    return {"item_id": item.id, "is_favorite": item.favorite is not None,
            "message": "主账号收藏已加入本地收藏" if work.is_bookmarked else "主账号未收藏，本地收藏保持"}
