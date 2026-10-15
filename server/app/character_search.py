"""User-triggered text research against AniList, never image recognition/upload.

Official schema: https://docs.anilist.co/reference/object/character
Only confirmed text is sent. Search results are not persisted or treated as facts
about a library item; the separate confirmation action adds one manual role tag.
"""
from collections import deque
from html.parser import HTMLParser
import json
import math
import re
from threading import Lock
import time
from typing import Literal
import unicodedata
import zlib

from fastapi import APIRouter, Depends, HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .auth import require_auth
from .database import get_db
from .models import Item, ItemTag, Tag, new_id
from .network import httpx_proxy

PROVIDER = "AniList"
ENDPOINT = "https://graphql.anilist.co"
MAX_RESPONSE_BYTES = 1024 * 1024
NOTICE = "这些是公开角色资料候选，不是对书页人物的识别结论；请核对作品和来源后再人工确认。"
_requests = deque()
_request_lock = Lock()

CHARACTER_FIELDS = """
  id name { full native alternative } description(asHtml: false) siteUrl
"""
CHARACTER_QUERY = """
query LibraryCharacterSearch($query: String!, $limit: Int!) {
  Page(page: 1, perPage: $limit) {
    characters(search: $query, sort: SEARCH_MATCH) {
      %s
      media(perPage: 3, sort: POPULARITY_DESC) {
        nodes { id type title { romaji english native } siteUrl }
      }
    }
  }
}
""" % CHARACTER_FIELDS
WORK_QUERY = """
query LibraryWorkCharacters($query: String!, $limit: Int!) {
  Page(page: 1, perPage: 3) {
    media(search: $query, type: MANGA, sort: SEARCH_MATCH) {
      id type title { romaji english native } siteUrl
      characters(perPage: $limit, sort: [ROLE, RELEVANCE]) { nodes { %s } }
    }
  }
}
""" % CHARACTER_FIELDS


class CharacterSearchError(Exception):
    def __init__(self, message, status=502, retry_after=None):
        self.message, self.status, self.retry_after = message, status, retry_after
        super().__init__(message)


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=120)
    mode: Literal["character", "work"] = "character"
    limit: int = Field(default=6, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def clean_query(cls, value):
        value = unicodedata.normalize("NFC", value).strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("请输入角色名或漫画名，不能包含控制字符")
        return value


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=191)

    @field_validator("name")
    @classmethod
    def character_tag(cls, value):
        value = unicodedata.normalize("NFC", value).strip()
        if value.startswith("角色:"):
            value = value[3:].strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("角色名称不能为空或包含控制字符")
        value = "角色:" + value
        if len(value) > 191:
            raise ValueError("角色标签不能超过 191 字符（含角色前缀）")
        return value


def _throttle():
    now = time.monotonic()
    with _request_lock:
        while _requests and _requests[0] <= now - 60:
            _requests.popleft()
        if len(_requests) >= 20:
            retry = max(1, math.ceil(60 - (now - _requests[0])))
            raise CharacterSearchError("角色搜索较频繁，请稍后重试", 429, retry)
        _requests.append(now)


def _read_response(response):
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding not in {"identity", "", "gzip", "deflate"}:
        raise CharacterSearchError("角色资料服务返回不支持的压缩格式，请稍后重试")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
    received, output = 0, bytearray()
    try:
        for chunk in response.iter_raw(chunk_size=65536):
            received += len(chunk)
            if received > MAX_RESPONSE_BYTES:
                raise CharacterSearchError("角色资料响应过大，请缩小搜索范围")
            output.extend(decoder.decompress(chunk, MAX_RESPONSE_BYTES + 1 - len(output)) if decoder else chunk)
            if len(output) > MAX_RESPONSE_BYTES:
                raise CharacterSearchError("角色资料响应过大，请缩小搜索范围")
            if decoder and decoder.unused_data:
                raise CharacterSearchError("角色资料压缩响应格式无效")
        if decoder and not decoder.eof:
            raise CharacterSearchError("角色资料响应不完整，请重试")
        return json.loads(output)
    except (zlib.error, ValueError, UnicodeError, RecursionError):
        raise CharacterSearchError("角色资料响应格式无效，请稍后重试") from None


def _request_provider(value):
    query = CHARACTER_QUERY if value.mode == "character" else WORK_QUERY
    data = _provider_data(query, value.model_dump(exclude={"mode"}))
    page = data.get("Page")
    if not isinstance(page, dict):
        raise CharacterSearchError("角色资料服务响应缺少结果，请稍后重试")
    return page


def _provider_data(query, variables):
    _throttle()
    try:
        with httpx.Client(proxy=httpx_proxy(), timeout=httpx.Timeout(20, connect=8),
                          trust_env=False, follow_redirects=False,
                          headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                   "User-Agent": "QiyeLibrary/0.1 character-research"}) as client:
            with client.stream("POST", ENDPOINT, json={"query": query, "variables": variables}) as response:
                if response.status_code == 429:
                    wait = response.headers.get("retry-after", "60")
                    wait = max(1, min(int(wait), 3600)) if wait.isdigit() else 60
                    raise CharacterSearchError("AniList 暂时限制请求频率，请稍后重试", 429, wait)
                if response.status_code != 200:
                    raise CharacterSearchError("AniList 角色资料服务暂时不可用，请稍后重试或检查代理")
                result = _read_response(response)
    except httpx.TimeoutException:
        raise CharacterSearchError("角色搜索超时，请检查代理或稍后重试", 504) from None
    except (httpx.HTTPError, ValueError, ImportError):
        raise CharacterSearchError("无法连接角色资料服务，请检查代理设置与网络") from None
    if not isinstance(result, dict) or result.get("errors"):
        errors = result.get("errors", []) if isinstance(result, dict) else []
        if isinstance(errors, list) and any(isinstance(error, dict) and error.get("status") == 429 for error in errors):
            raise CharacterSearchError("AniList 暂时限制请求频率，请稍后重试", 429, 60)
        raise CharacterSearchError("角色资料服务未完成搜索，请稍后重试")
    data = result.get("data")
    if not isinstance(data, dict):
        raise CharacterSearchError("角色资料服务响应缺少结果，请稍后重试")
    return data


def fetch_character_profile(ident):
    query = "query LibraryCharacterProfile($id: Int!) { Character(id: $id) { " + CHARACTER_FIELDS + " media(perPage: 3, sort: POPULARITY_DESC) { nodes { id type title { romaji english native } siteUrl } } } }"
    result = _candidate(_provider_data(query, {"id": ident}).get("Character"))
    if not result or result["id"] != str(ident):
        raise CharacterSearchError("人物资料不存在或返回了不匹配的人物", 404)
    return result


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.ignored = [], 0

    def handle_starttag(self, tag, _attributes):
        if tag in {"script", "style"}:
            self.ignored += 1
        elif tag in {"p", "br", "li", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, value):
        if not self.ignored:
            self.parts.append(value)


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    # Output is plain text: never render provider HTML or load embedded images.
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value[:50000])
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    parser = _TextOnly()
    parser.feed(value)
    value = "".join(parser.parts)
    value = "".join(c for c in value if ord(c) >= 32 or c in "\n\t")
    return value.strip()[:limit]


def _id(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**31 - 1 else None


def _work(value):
    if not isinstance(value, dict) or not _id(value.get("id")) or value.get("type") not in {"ANIME", "MANGA"}:
        return None
    titles = value.get("title")
    if not isinstance(titles, dict):
        return None
    title = next((_text(titles.get(key), 300) for key in ("native", "english", "romaji") if _text(titles.get(key), 300)), "")
    if not title:
        return None
    kind = value["type"].lower()
    # Construct known canonical URLs from typed IDs; never follow provider URLs.
    return {"title": title, "type": kind, "source_url": f"https://anilist.co/{kind}/{value['id']}"}


def _candidate(value, works=None):
    if not isinstance(value, dict) or not _id(value.get("id")) or not isinstance(value.get("name"), dict):
        return None
    names = value["name"]
    name = _text(names.get("full") or names.get("native"), 188)
    if not name:
        return None
    raw_aliases = names.get("alternative")
    aliases = list(dict.fromkeys(_text(alias, 188) for alias in raw_aliases[:30] if _text(alias, 188))) if isinstance(raw_aliases, list) else []
    if works is None:
        media = value.get("media")
        nodes = media.get("nodes", []) if isinstance(media, dict) else []
        works = [work for raw in nodes[:3] if (work := _work(raw))] if isinstance(nodes, list) else []
    return {"id": str(value["id"]), "name": name, "native_name": _text(names.get("native"), 188) or None,
            "aliases": aliases, "works": works, "description": _text(value.get("description"), 1600),
            "source_url": f"https://anilist.co/character/{value['id']}", "provider": PROVIDER}


def search_characters(value: SearchInput):
    page = _request_provider(value)
    key = "characters" if value.mode == "character" else "media"
    rows = page.get(key)
    if not isinstance(rows, list):
        raise CharacterSearchError("角色资料服务响应格式已变化，请稍后重试")
    candidates = {}
    if value.mode == "character":
        for row in rows[:value.limit]:
            candidate = _candidate(row)
            if candidate:
                candidates[candidate["id"]] = candidate
    else:
        # Round-robin the three matching manga so one result cannot hide every
        # alternative title. The source work is shown beside each candidate.
        books = []
        for row in rows[:3]:
            work = _work(row)
            children = row.get("characters", {}).get("nodes") if isinstance(row, dict) and isinstance(row.get("characters"), dict) else None
            if work and isinstance(children, list):
                books.append((work, children[:value.limit]))
        for index in range(value.limit):
            for work, children in books:
                candidate = _candidate(children[index], [work]) if index < len(children) else None
                if not candidate:
                    continue
                if candidate["id"] in candidates:
                    if work not in candidates[candidate["id"]]["works"]:
                        candidates[candidate["id"]]["works"].append(work)
                elif len(candidates) < value.limit:
                    candidates[candidate["id"]] = candidate
    if rows and not candidates:
        # A nonempty but wholly unusable provider result is a schema/data
        # failure, not evidence that the user's search found no characters.
        raise CharacterSearchError("角色资料服务返回的候选无法解析，请稍后重试")
    return {"provider": PROVIDER, "query": value.query, "mode": value.mode,
            "candidates": list(candidates.values()), "notice": NOTICE}


def confirm_character(db, item_id, tag_name):
    """Append atomically, preserving every existing manual/source/AI tag."""
    dialect = db.get_bind().dialect.name
    try:
        if dialect == "sqlite":
            # Serialize before the initial read, avoiding SQLite WAL snapshot
            # upgrade failures between concurrent confirmations.
            db.execute(text("BEGIN IMMEDIATE"))
        item = db.scalar(select(Item.id).where(Item.id == item_id).with_for_update())
        if not item:
            raise HTTPException(404, "作品不存在")
        insert = mysql_insert if dialect == "mysql" else sqlite_insert
        statement = insert(Tag).values(id=new_id(), name=tag_name)
        statement = statement.on_duplicate_key_update(id=Tag.id) if dialect == "mysql" else statement.on_conflict_do_nothing(index_elements=["name"])
        db.execute(statement)
        tag = db.scalar(select(Tag).where(Tag.name == tag_name).with_for_update())
        statement = insert(ItemTag).values(item_id=item_id, tag_id=tag.id, origin="manual", score=None)
        statement = statement.on_duplicate_key_update(origin="manual") if dialect == "mysql" else statement.on_conflict_do_nothing(index_elements=["item_id", "tag_id", "origin"])
        db.execute(statement)
        db.commit()
        return {"item_id": item_id, "tag": {"id": tag.id, "name": tag.name, "origin": "manual", "score": None}}
    except Exception:
        db.rollback()
        raise


router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/ai/characters/search")
def search(value: SearchInput):
    try:
        return search_characters(value)
    except CharacterSearchError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(exc.status, exc.message, headers=headers) from None


@router.post("/ai/characters/confirm")
def confirm(value: ConfirmInput, db=Depends(get_db)):
    return confirm_character(db, value.item_id, value.name)
