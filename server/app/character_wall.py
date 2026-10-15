"""Character-tag work collections and explicitly confirmed online biographies."""
from typing import Literal
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select

from .auth import require_auth
from .database import get_db
from . import character_search as research
from .library import downloaded_content, ensure_tag, iso
from .models import Asset, CharacterProfile, Item, ItemTag, Tag, utcnow

router = APIRouter(prefix="/characters", dependencies=[Depends(require_auth)])
PREFIXES = ("角色:", "角色：", "人物:", "人物：", "character:")


def role_name(value):
    for prefix in PREFIXES:
        if value.lower().startswith(prefix):
            return value[len(prefix):].strip()
    return ""


def character_tag(db, ident):
    row = db.get(Tag, ident)
    if row is None or not role_name(row.name):
        raise HTTPException(404, "人物不存在，请先添加角色标签或人物资料")
    return row


def filtered_links(source, downloaded):
    items = select(Item.id, Item.created_at)
    if source:
        items = items.where(Item.source == source)
    if downloaded:
        items = items.where(downloaded_content())
    eligible = items.subquery()
    return select(ItemTag.tag_id, ItemTag.item_id, eligible.c.created_at).join(eligible, eligible.c.id == ItemTag.item_id).distinct().subquery()


def wall_query(links, include_empty):
    totals = select(links.c.tag_id, func.count().label("count"), func.max(links.c.created_at).label("latest")).group_by(links.c.tag_id).subquery()
    recognized = or_(*(and_(Tag.name.like(prefix + "%"), func.trim(func.substr(Tag.name, len(prefix)+1)) != "") for prefix in PREFIXES))
    return select(Tag, CharacterProfile, func.coalesce(totals.c.count, 0).label("count")).outerjoin(CharacterProfile, CharacterProfile.tag_id == Tag.id).outerjoin(totals, totals.c.tag_id == Tag.id).where(
        recognized, or_(totals.c.count > 0, CharacterProfile.tag_id.is_not(None) if include_empty else False))


def covers(db, identifiers, links):
    ranks = select(links.c.tag_id, links.c.item_id, func.row_number().over(partition_by=links.c.tag_id, order_by=(links.c.created_at.desc(), links.c.item_id)).label("rank")).where(links.c.tag_id.in_(identifiers)).subquery()
    pairs = db.execute(select(ranks.c.tag_id, ranks.c.item_id).where(ranks.c.rank <= 3).order_by(ranks.c.tag_id, ranks.c.rank)).all()
    if not pairs:
        return {}
    first_image = select(Asset.id).where(Asset.item_id == Item.id, Asset.status == "available", Asset.mime_type.like("image/%")).order_by(Asset.position, Asset.id).limit(1).correlate(Item).scalar_subquery()
    records = {}
    for row in db.execute(select(Item.id, Item.title, Item.title_zh, Item.title_zh_source, Item.rating, Item.preview_cover, first_image.label("cover_asset")).where(Item.id.in_({ident for _, ident in pairs}))):
        cover = f"/api/assets/{row.cover_asset}/thumbnail" if row.cover_asset else f"/api/items/{row.id}/cover" if row.preview_cover else None
        records[row.id] = {"id": row.id, "title": row.title_zh if row.title_zh and row.title_zh_source == row.title else row.title, "cover_url": cover, "rating": row.rating}
    result = {}
    for tag_id, item_id in pairs:
        if item_id in records:
            result.setdefault(tag_id, []).append(records[item_id])
    return result


def profile_view(profile):
    if profile is None:
        return None
    return {"provider": profile.provider, "id": profile.provider_id, "name": profile.name, "native_name": profile.native_name,
            "aliases": profile.aliases, "description": profile.description, "works": profile.works,
            "source_url": profile.source_url, "updated_at": iso(profile.updated_at)}


def view(tag, profile, count, samples):
    name = role_name(tag.name)
    chinese = role_name(tag.name_zh) or tag.name_zh if tag.name_zh else None
    return {"id": tag.id, "tag_name": tag.name, "name": name, "display_name": chinese or (profile.native_name if profile else None) or name,
            "count": count, "profile": profile_view(profile), "covers": samples}


@router.get("")
def list_characters(q: str = Query("", max_length=200), source: str = "", downloaded: bool = False,
                    page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=60), sort: Literal["count", "name"] = "count", db=Depends(get_db)):
    links = filtered_links(source, downloaded)
    query = wall_query(links, not source and not downloaded)
    if q.strip():
        term = unicodedata.normalize("NFC", q.strip()).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(or_(*(field.ilike(f"%{term}%", escape="\\") for field in (Tag.name, Tag.name_zh, CharacterProfile.search_text))))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    ordering = query.selected_columns["count"].desc() if sort == "count" else func.coalesce(Tag.name_zh, Tag.name).asc()
    rows = db.execute(query.order_by(ordering, Tag.id).offset((page-1)*page_size).limit(page_size)).all()
    samples = covers(db, [tag.id for tag, _, _ in rows], links)
    return {"items": [view(tag, profile, count, samples.get(tag.id, [])) for tag, profile, count in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{tag_id}")
def character_detail(tag_id: str, source: str = "", downloaded: bool = False, db=Depends(get_db)):
    tag = character_tag(db, tag_id)
    links = filtered_links(source, downloaded)
    count = db.scalar(select(func.count()).select_from(links).where(links.c.tag_id == tag.id)) or 0
    return view(tag, db.get(CharacterProfile, tag.id), count, covers(db, [tag.id], links).get(tag.id, []))


@router.post("/search")
def search(value: research.SearchInput):
    try:
        return research.search_characters(value)
    except research.CharacterSearchError as exc:
        raise HTTPException(exc.status, exc.message, headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None) from None


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: int = Field(strict=True, ge=1, le=2**31-1)


def save_profile(db, value, tag_id=None):
    # Validate the target before networking, then re-read inside the save.
    if tag_id:
        character_tag(db, tag_id)
    db.rollback()
    try:
        candidate = research.fetch_character_profile(value.candidate_id)
    except research.CharacterSearchError as exc:
        raise HTTPException(exc.status, exc.message, headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None) from None
    tag = character_tag(db, tag_id) if tag_id else ensure_tag(db, "角色:" + unicodedata.normalize("NFC", candidate["native_name"] or candidate["name"])[:188])
    values = {"tag_id": tag.id, "provider": candidate["provider"], "provider_id": candidate["id"], "name": candidate["name"], "native_name": candidate["native_name"],
              "aliases": candidate["aliases"], "description": candidate["description"], "works": candidate["works"], "source_url": candidate["source_url"],
              "search_text": "\n".join(filter(None, [candidate["name"], candidate["native_name"], *candidate["aliases"]])), "created_at": utcnow(), "updated_at": utcnow()}
    if db.bind.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(CharacterProfile).values(**values)
        db.execute(statement.on_duplicate_key_update(**{key: value for key, value in values.items() if key not in ("tag_id", "created_at")}))
    else:
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(CharacterProfile).values(**values)
        db.execute(statement.on_conflict_do_update(index_elements=["tag_id"], set_={key: value for key, value in values.items() if key not in ("tag_id", "created_at")}))
    db.commit()
    db.expire_all()
    return character_detail(tag.id, db=db)


@router.post("")
def add_character(value: ProfileInput, db=Depends(get_db)):
    return save_profile(db, value)


@router.post("/{tag_id}/profile")
def update_profile(tag_id: str, value: ProfileInput, db=Depends(get_db)):
    return save_profile(db, value, tag_id)
