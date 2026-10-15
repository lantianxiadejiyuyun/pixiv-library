"""Explicit, single-image reverse search. Local inference never calls this module."""
from __future__ import annotations

from collections import deque
from io import BytesIO
import ipaddress
import json
import math
import socket
from threading import Lock
import time
from urllib.parse import urlsplit
import warnings

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.orm import Session

from .auth import require_auth
from .database import get_db
from .integrations.base import ConnectorError
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import Asset, NetworkSetting
from . import network
from .storage import resolve_asset_path


router = APIRouter(dependencies=[Depends(require_auth)])
KEY = "character_image_search"
ENDPOINT = "https://saucenao.com/search.php"
MAX_FILE = 8 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_PIXELS = 36_000_000
_attempts: deque[float] = deque()
_rate_lock = Lock()


class ImageSearchSettings(BaseModel):
    api_key: SecretStr | None = Field(default=None, max_length=256)


def _read_key(db):
    row = db.get(NetworkSetting, KEY)
    try:
        return decrypt_credentials(row.value_encrypted).get("api_key", "") if row else ""
    except ConnectorError:
        raise HTTPException(503, "图搜密钥无法解密，请重新保存密钥") from None


@router.get("/ai/characters/image/settings")
def image_settings(db: Session = Depends(get_db)):
    return {"provider": "SauceNAO", "has_api_key": bool(_read_key(db)), "max_file_bytes": MAX_FILE,
            "notice": "仅在点击上传搜索时向 SauceNAO 发送选中的一张图片；未设置密钥时使用匿名额度。图片会缩小并移除元数据。"}


@router.put("/ai/characters/image/settings")
def save_image_settings(value: ImageSearchSettings, db: Session = Depends(get_db)):
    if value.api_key is not None:
        key = value.api_key.get_secret_value().strip()
        if key and (not key.isascii() or any(not c.isalnum() and c not in "_-" for c in key)):
            raise HTTPException(400, "图搜密钥格式无效，请复制完整 API Key")
        row = db.get(NetworkSetting, KEY)
        if row is None:
            row = NetworkSetting(key=KEY)
            db.add(row)
        row.value_encrypted = encrypt_credentials({"api_key": key})
        db.commit()
    return image_settings(db)


def _rate_limit():
    with _rate_lock:
        now = time.monotonic()
        while _attempts and now - _attempts[0] >= 60:
            _attempts.popleft()
        if len(_attempts) >= 6:
            raise HTTPException(429, "图搜请求过于频繁，请稍后再试")
        _attempts.append(now)


def prepare_image(content: bytes) -> bytes:
    if not content or len(content) > MAX_FILE:
        raise HTTPException(413, "请使用不超过 8 MiB 的图片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as original:
                if original.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    raise HTTPException(400, "仅支持 JPEG、PNG、WebP 或 GIF 图片")
                if original.width * original.height > MAX_PIXELS:
                    raise HTTPException(413, "图片像素过多，请先截取人物或缩小图片")
                original.seek(0)
                oriented = ImageOps.exif_transpose(original)
                oriented.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                rgba = oriented.convert("RGBA")
                # A new canvas removes EXIF, comments, names and embedded metadata.
                canvas = Image.new("RGB", rgba.size, "white")
                canvas.paste(rgba, mask=rgba.getchannel("A"))
                result = BytesIO()
                canvas.save(result, "JPEG", quality=90)
                return result.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise HTTPException(400, "无法读取图片，请使用有效的漫画截图") from None


def _text(value, limit=300):
    if isinstance(value, list):
        value = " / ".join(str(v) for v in value[:12] if isinstance(v, (str, int)))
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    return " ".join(str(value).split())[:limit]


def _source_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or any(c.isspace() or ord(c) < 32 or c == "\\" for c in value)
                or "." not in host or host.endswith((".local", ".localhost", ".lan", ".internal", ".home", ".localdomain"))):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                address = None
        if address is not None and (not address.is_global or address.is_multicast):
            return None
        return value
    except ValueError:
        return None


def parse_matches(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("header"), dict):
        raise HTTPException(502, "图搜服务返回了无法识别的结果")
    header = payload["header"]
    try:
        status = int(header.get("status", -1))
    except (TypeError, ValueError):
        raise HTTPException(502, "图搜服务返回了无效状态") from None
    if status != 0:
        # Never reflect vendor error strings: they may echo a secret or input.
        raise HTTPException(502, "图搜服务未完成搜索，请检查 API Key、搜索额度或稍后再试")
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise HTTPException(502, "图搜服务返回了无效候选列表")
    matches = []
    for row in rows[:20]:
        if not isinstance(row, dict) or not isinstance(row.get("header"), dict) or not isinstance(row.get("data"), dict):
            continue
        metadata, data = row["header"], row["data"]
        try:
            similarity = float(metadata.get("similarity", 0))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(similarity) or not 0 <= similarity <= 100:
            continue
        characters = data.get("characters", [])
        if isinstance(characters, str):
            characters = characters.split(",")
        if not isinstance(characters, list):
            characters = []
        links = data.get("ext_urls", [])
        if not isinstance(links, list):
            links = []
        urls = list(dict.fromkeys(url for link in links[:10] if (url := _source_url(link))))
        matches.append({"similarity": round(similarity, 2), "index_name": _text(metadata.get("index_name"), 120),
            "title": _text(data.get("title") or data.get("eng_name") or data.get("jp_name") or data.get("source")) or "未提供标题",
            "creator": _text(data.get("creator") or data.get("member_name") or data.get("author_name"), 160),
            "characters": list(dict.fromkeys(text for name in characters[:20] if (text := _text(name, 120)))),
            "material": _text(data.get("material"), 200), "source_urls": urls})
    if rows and not matches:
        raise HTTPException(502, "图搜服务返回的候选无法解析，请稍后再试")
    return matches[:10]


def search_image(image: bytes, api_key: str):
    data = {"output_type": "2", "numres": "10", "db": "999"}
    if api_key:
        data["api_key"] = api_key
    try:
        proxy = network.httpx_proxy()
        with httpx.Client(proxy=proxy, trust_env=False, timeout=httpx.Timeout(30, connect=10),
                follow_redirects=False, headers={"User-Agent": "QiyeLibrary/1.0", "Accept": "application/json", "Accept-Encoding": "identity"}) as client:
            with client.stream("POST", ENDPOINT, data=data, files={"file": ("image.jpg", image, "image/jpeg")}) as response:
                if response.status_code == 429:
                    raise HTTPException(429, "SauceNAO 搜索额度不足或请求过多，请稍后再试")
                if response.status_code in (401, 403):
                    raise HTTPException(502, "SauceNAO 拒绝了请求，请检查 API Key 或当前代理；也可在其官网完成图搜")
                if response.status_code != 200:
                    raise HTTPException(502, "图搜服务暂不可用，请稍后再试")
                if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    raise HTTPException(502, "图搜服务返回了不支持的压缩响应")
                content = bytearray()
                for chunk in response.iter_raw(chunk_size=65536):
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise HTTPException(502, "图搜服务响应过大，已停止读取")
                payload = json.loads(content)
    except (httpx.HTTPError, ValueError, RecursionError, ConnectorError):
        raise HTTPException(502, "图搜连接失败或返回格式异常，请检查网络与代理配置") from None
    return {"provider": "SauceNAO", "matches": parse_matches(payload), "uploaded_size": len(image),
        "notice": "相似度表示图片匹配程度，不是人物身份置信度。请结合出处核对；没有角色字段时可用作品名继续文字检索。"}


@router.post("/ai/characters/image-search")
def image_search(file: UploadFile | None = File(default=None), asset_id: str | None = Form(default=None, max_length=36),
                 consent: bool = Form(default=False), db: Session = Depends(get_db)):
    if not consent:
        raise HTTPException(400, "请先确认将选中的这一张图片上传至 SauceNAO")
    if (file is None) == (not asset_id):
        raise HTTPException(400, "请选择一个书页或上传一张图片，不能同时提交")
    api_key = _read_key(db)
    if file is not None:
        content = file.file.read(MAX_FILE + 1)
    else:
        asset = db.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(404, "书页不存在")
        if not asset.mime_type.startswith("image/") or asset.status != "available":
            raise HTTPException(400, "请选择已保存的图片书页")
        try:
            with resolve_asset_path(asset.relative_path).open("rb") as handle:
                content = handle.read(MAX_FILE + 1)
        except (OSError, ValueError):
            raise HTTPException(409, "书页原文件不可用，请检查本地或 NAS 存储") from None
    image = prepare_image(content)
    _rate_limit()
    return search_image(image, api_key)
