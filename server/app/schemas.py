from typing import Literal
from pydantic import BaseModel, Field

ItemType = Literal["manga", "illustration", "novel", "article"]
Rating = Literal["unknown", "general", "sensitive", "adult"]


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class ItemPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=50000)
    author: str | None = Field(default=None, max_length=512)
    language: str | None = Field(default=None, max_length=32)
    rating: Rating | None = None
    tags: list[str] | None = Field(default=None, max_length=200)


class FolderImport(BaseModel):
    path: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=512)
    type: ItemType = "manga"


class ProgressUpdate(BaseModel):
    page: int = Field(ge=0)
    section: int = Field(default=0, ge=0)
    offset: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
