from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .auth import require_auth
from .database import get_db
from .models import Item, Series
from .integrations import get_connector
from .integrations.base import ConnectorError
from .jobs import account_credentials, job_view
from .series_service import detail, enqueue_series, replace_entries, MAX_SERIES_ENTRIES

router = APIRouter(dependencies=[Depends(require_auth)])


class EntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=36)
    label: str = Field(default="", max_length=512)


class ManualSeriesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=512)
    kind: Literal["novel", "manga"]
    entries: list[EntryInput] = Field(min_length=1, max_length=MAX_SERIES_ENTRIES)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("系列名称不能为空")
        return value.strip()


class ManualSeriesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=512)
    entries: list[EntryInput] = Field(max_length=MAX_SERIES_ENTRIES)


class SeriesImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["pixiv"] = "pixiv"
    url_or_id: str = Field(min_length=1, max_length=2048)
    kind: Literal["novel", "manga"] | None = None
    account_id: str | None = Field(default=None, max_length=36)


def checked_entries(db, entries):
    ids = [entry.item_id for entry in entries]
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "同一系列不能重复添加同一作品")
    if ids:
        existing = set(db.scalars(select(Item.id).where(Item.id.in_(ids)).with_for_update()))
        if existing != set(ids):
            raise HTTPException(404, "部分作品已不存在，请刷新后重试")
    return [(entry.item_id, entry.label.strip()) for entry in entries]


@router.post("/series/import")
def import_series(value: SeriesImportInput, db=Depends(get_db)):
    connector = get_connector("pixiv")
    try:
        remote_id, kind = connector.normalize_series(value.url_or_id, kind=value.kind)
        connector.credentials = account_credentials(db, "pixiv", value.account_id)
        connector.validate_credentials()
    except (ConnectorError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return job_view(enqueue_series(db, {"platform": "pixiv", "remote_id": remote_id,
                                      "series_kind": kind, "account_id": value.account_id}))


@router.get("/series/{series_id}")
def get_series(series_id: str, db=Depends(get_db)):
    series = db.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "系列不存在")
    return detail(db, series)


@router.post("/series")
def create_series(value: ManualSeriesInput, db=Depends(get_db)):
    entries = checked_entries(db, value.entries)
    series = Series(title=value.title, kind=value.kind, origin="manual", source="local", complete=True, revision=1)
    db.add(series)
    db.flush()
    replace_entries(db, series, entries)
    db.commit()
    return detail(db, series)


@router.put("/series/{series_id}")
def edit_series(series_id: str, value: ManualSeriesUpdate, db=Depends(get_db)):
    series = db.scalar(select(Series).where(Series.id == series_id).with_for_update().execution_options(populate_existing=True))
    if series is None:
        raise HTTPException(404, "系列不存在")
    if series.origin != "manual":
        raise HTTPException(409, "官方系列按平台目录排序；请创建手工系列进行自定义关联")
    if series.revision != value.revision:
        raise HTTPException(409, "系列已被其他页面修改，请刷新后重试")
    if not value.title.strip():
        raise HTTPException(400, "系列名称不能为空")
    entries = checked_entries(db, value.entries)
    replace_entries(db, series, entries)
    series.title, series.revision = value.title.strip(), series.revision + 1
    db.commit()
    return detail(db, series)
