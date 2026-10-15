"""Single-library collections and history, shared by its administrator's devices."""
import unicodedata
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .auth import require_auth
from .database import get_db
from .models import FavoriteGroup, FavoriteGroupItem, Item, ItemFavorite, ItemVisit, utcnow

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/items/{item_id}/cover-refresh")
def refresh_cover(item_id: str, db=Depends(get_db)):
    from .jobs import enqueue_import
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "作品不存在")
    if item.source != "pixiv" or not item.source_id:
        raise HTTPException(400, "此作品暂不支持补全远端封面")
    job = enqueue_import(db, {"platform": item.source, "remote_id": item.source_id, "item_id": item.id, "download": False})
    return {"job_id": job.id, "message": "已加入封面队列，正文保持不变"}


def seed_default_groups(db):
    for i, name in enumerate(("想看", "在看", "已看", "推荐"), 1):
        db.add(FavoriteGroup(id=f"57135c87-cd5f-4cbd-9e46-{i:012d}", name=name))
    db.commit()


class GroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = unicodedata.normalize("NFC", value).strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("分组名称不能为空或包含控制字符")
        return value


class FavoriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    favorite: StrictBool
    group_ids: list[Annotated[str, Field(min_length=1, max_length=36)]] | None = Field(default=None, max_length=200)
    score: Annotated[int, Field(strict=True, ge=0, le=5)] | None = None


@router.get("/favorite-groups")
def groups(db=Depends(get_db)):
    rows = db.execute(select(FavoriteGroup.id, FavoriteGroup.name, func.count(FavoriteGroupItem.item_id))
        .outerjoin(FavoriteGroupItem).group_by(FavoriteGroup.id, FavoriteGroup.name)
        .order_by(FavoriteGroup.created_at, FavoriteGroup.id)).all()
    return {"items": [{"id": ident, "name": name, "count": count} for ident, name, count in rows],
            "total_favorites": db.scalar(select(func.count()).select_from(ItemFavorite)),
            "ungrouped_count": db.scalar(select(func.count()).select_from(ItemFavorite)
                .where(~ItemFavorite.groups.any()))}


def save_group(db, group):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "分组名称已存在") from None
    return {"id": group.id, "name": group.name}


@router.post("/favorite-groups")
def create_group(value: GroupInput, db=Depends(get_db)):
    group = FavoriteGroup(name=value.name)
    db.add(group)
    return save_group(db, group)


@router.put("/favorite-groups/{group_id}")
def rename_group(group_id: str, value: GroupInput, db=Depends(get_db)):
    group = db.scalar(select(FavoriteGroup).where(FavoriteGroup.id == group_id).with_for_update())
    if group is None:
        raise HTTPException(404, "收藏分组不存在")
    group.name = value.name
    return save_group(db, group)


@router.delete("/favorite-groups/{group_id}")
def remove_group(group_id: str, db=Depends(get_db)):
    group = db.scalar(select(FavoriteGroup).where(FavoriteGroup.id == group_id).with_for_update())
    if group is None:
        raise HTTPException(404, "收藏分组不存在")
    db.delete(group)
    db.commit()
    return {"ok": True}


@router.put("/items/{item_id}/favorite")
def favorite(item_id: str, value: FavoriteInput, db=Depends(get_db)):
    ids = sorted(set(value.group_ids or []))
    if ids and not value.favorite:
        raise HTTPException(400, "取消收藏时不能指定分组")
    if ids:
        existing = db.scalars(select(FavoriteGroup.id).where(FavoriteGroup.id.in_(ids))
            .order_by(FavoriteGroup.id).with_for_update()).all()
        if existing != ids:
            raise HTTPException(400, "收藏分组已删除，请刷新后重新选择")
    item = db.scalar(select(Item).where(Item.id == item_id).with_for_update())
    if item is None:
        raise HTTPException(404, "作品不存在")
    if value.score is not None:
        item.user_score = value.score
    if not value.favorite:
        if item.favorite:
            db.delete(item.favorite)
        db.commit()
        return {"id": item_id, "is_favorite": False, "favorite_group_ids": [], "user_score": item.user_score}
    if item.favorite is None:
        item.favorite = ItemFavorite()
    if value.group_ids is not None:
        wanted = set(ids)
        retained = {link.group_id for link in item.favorite.groups}
        item.favorite.groups[:] = [link for link in item.favorite.groups if link.group_id in wanted]
        item.favorite.groups.extend(FavoriteGroupItem(group_id=ident) for ident in ids if ident not in retained)
    db.commit()
    return {"id": item_id, "is_favorite": True, "favorite_group_ids": sorted(link.group_id for link in item.favorite.groups), "user_score": item.user_score}


@router.post("/items/{item_id}/visit")
def record_visit(item_id: str, db=Depends(get_db)):
    if db.scalar(select(Item.id).where(Item.id == item_id).with_for_update()) is None:
        raise HTTPException(404, "作品不存在")
    values = {"item_id": item_id, "viewed_at": utcnow()}
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(ItemVisit).values(**values)
        db.execute(statement.on_conflict_do_update(index_elements=["item_id"], set_={"viewed_at": values["viewed_at"]}))
    else:
        from sqlalchemy.dialects.mysql import insert
        statement = insert(ItemVisit).values(**values)
        db.execute(statement.on_duplicate_key_update(viewed_at=values["viewed_at"]))
    db.commit()
    return {"ok": True}


@router.delete("/history")
def clear_history(db=Depends(get_db)):
    db.execute(delete(ItemVisit))
    db.commit()
    return {"ok": True}
