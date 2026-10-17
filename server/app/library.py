import io
import re
import stat
import unicodedata
import warnings
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import Asset, Item, ItemTag, Tag, new_id
from .storage import resolve_asset_path, save_blob, validate_collection
from .video_media import VIDEO_EXTENSIONS, video_mime

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md"}
ARCHIVE_EXTENSIONS = {".zip", ".cbz"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | VIDEO_EXTENSIONS | ARCHIVE_EXTENSIONS
IMAGE_MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif", "BMP": "image/bmp"}


def natural_key(value: str):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", value)]


def clean_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name.replace("\\", "/").split("/")[-1])
    name = "".join(c for c in name if ord(c) >= 32).strip()
    if not name or len(name) > 512:
        raise ValueError("文件名为空或过长")
    return name


def _check_archive_path(name: str) -> None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or ":" in normalized or "\x00" in normalized:
        raise ValueError("压缩包包含不安全路径")


def expand_files(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    if not files or len(files) > settings.max_import_files:
        raise ValueError("导入文件数量不符合限制")
    output: list[tuple[str, bytes]] = []
    total = 0
    input_total = 0
    for raw_name, data in sorted(files, key=lambda file: natural_key(file[0])):
        name = clean_filename(raw_name)
        input_total += len(data)
        if len(data) > settings.max_file_bytes or input_total > settings.max_import_bytes:
            raise ValueError("导入文件超过大小限制")
        suffix = Path(name).suffix.lower()
        if suffix in ARCHIVE_EXTENSIONS:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    entries = archive.infolist()
                    if len(entries) > settings.max_import_files:
                        raise ValueError("压缩包文件数量过多")
                    for entry in sorted(entries, key=lambda e: natural_key(e.filename)):
                        _check_archive_path(entry.filename)
                        if stat.S_ISLNK(entry.external_attr >> 16):
                            raise ValueError("压缩包不允许包含符号链接")
                        if entry.is_dir():
                            continue
                        entry_suffix = Path(entry.filename).suffix.lower()
                        if entry_suffix not in IMAGE_EXTENSIONS | TEXT_EXTENSIONS | VIDEO_EXTENSIONS:
                            continue
                        if entry.flag_bits & 1:
                            raise ValueError("不支持加密压缩包")
                        if entry.file_size > settings.max_file_bytes or entry.file_size > max(1, entry.compress_size) * 1000:
                            raise ValueError("压缩包解压大小或压缩比超过限制")
                        total += entry.file_size
                        if total > settings.max_import_bytes or len(output) >= settings.max_import_files:
                            raise ValueError("压缩包解压总量超过限制")
                        with archive.open(entry) as stream:
                            content = stream.read(settings.max_file_bytes + 1)
                        if len(content) != entry.file_size:
                            raise ValueError("压缩包文件大小不匹配")
                        output.append((clean_filename(entry.filename), content))
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise ValueError("无法读取 ZIP/CBZ 压缩包") from exc
        elif suffix in IMAGE_EXTENSIONS | TEXT_EXTENSIONS | VIDEO_EXTENSIONS:
            total += len(data)
            if total > settings.max_import_bytes or len(output) >= settings.max_import_files:
                raise ValueError("导入文件总量或数量超过限制")
            output.append((name, data))
        else:
            raise ValueError(f"不支持的文件格式：{suffix or '无扩展名'}")
    if not output:
        raise ValueError("未找到支持的图片、视频或 UTF-8 文本文件")
    return output


def validate_content(name: str, data: bytes) -> tuple[bytes, str]:
    if not data:
        raise ValueError(f"文件为空：{name}")
    if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
        return data, video_mime(name, data)
    if Path(name).suffix.lower() in TEXT_EXTENSIONS:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文件必须使用 UTF-8 编码") from exc
        if "\x00" in text:
            raise ValueError("文本文件包含二进制数据")
        return text.encode("utf-8"), "text/plain"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                mime = IMAGE_MIMES.get(picture.format)
                if not mime:
                    raise ValueError("不支持该图片格式")
                picture.verify()
    except (OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError(f"图片损坏或像素数超过安全限制：{name}") from exc
    return data, mime


def normalize_tags(names: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(unicodedata.normalize("NFC", name).strip() for name in names))
    if len(cleaned) > 200 or any(len(name) > 191 for name in cleaned):
        raise ValueError("标签过多或标签名称超过 191 字符")
    return [name for name in cleaned if name]


def save_preview_cover(preview, source):
    """A small, inert cover blob, deliberately separate from body Assets."""
    name, data = preview
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("封面超过 8 MiB 限制")
    data, mime = validate_content(clean_filename(name), data)
    if not mime.startswith("image/"):
        raise ValueError("封面必须是图片")
    with Image.open(io.BytesIO(data)) as original:
        if original.width * original.height > 36_000_000:
            raise ValueError("封面像素数量超过限制")
        picture = ImageOps.exif_transpose(original)
        picture.thumbnail((640, 960))
        picture = picture.convert("RGB")
        buffer = io.BytesIO()
        picture.save(buffer, "WEBP", quality=82)
    content = buffer.getvalue()
    relative, digest = save_blob(content, source=source)
    return {"relative_path": relative, "sha256": digest, "size_bytes": len(content), "mime_type": "image/webp"}


def ensure_tag(db: Session, name: str) -> Tag:
    """Atomic creation plus a current read, including under MySQL REPEATABLE READ.

    A savepoint/duplicate exception followed by an ordinary SELECT can still
    see the old snapshot and return None for another transaction's new tag.
    """
    if db.get_bind().dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(Tag).values(id=new_id(), name=name)
        db.execute(statement.on_duplicate_key_update(id=Tag.id))
    else:
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(Tag).values(id=new_id(), name=name)
        db.execute(statement.on_conflict_do_nothing(index_elements=["name"]))
    return db.scalar(select(Tag).where(Tag.name == name).with_for_update().execution_options(populate_existing=True))


def downloaded_content():
    body_count = select(func.count(Asset.id)).where(Asset.item_id == Item.id).correlate(Item).scalar_subquery()
    return (Item.assets.any() & ~Item.assets.any(Asset.status != "available") &
            or_(Item.type.not_in(["manga", "illustration"]), Item.remote_page_count.is_(None),
                Item.remote_page_count <= 0, body_count >= Item.remote_page_count))


def set_tags(db: Session, item: Item, names: list[str], origin: str = "manual") -> None:
    names = normalize_tags(names)
    item.item_tags[:] = [link for link in item.item_tags if link.origin != origin]
    db.flush()
    seen = set()
    for name in sorted(names):
        tag = ensure_tag(db, name)
        if tag.id in seen:
            continue
        seen.add(tag.id)
        item.item_tags.append(ItemTag(tag=tag, origin=origin))


def import_files(db: Session, files: list[tuple[str, bytes]], *, title: str | None = None, item_type: str = "manga", source: str = "local", source_id: str | None = None, source_url: str | None = None, author: str = "", description: str = "", language: str = "", tags: list[str] | None = None, before_commit=None, checkpoint=None) -> Item:
    validate_collection(source)
    if item_type not in {"manga", "illustration", "novel", "article"}:
        raise ValueError("不支持的内容类型")
    existing = None
    if source_id:
        existing = db.scalar(select(Item).where(Item.source == source, Item.source_id == source_id))
        if existing and existing.assets:
            return existing
    normalized = expand_files(files)
    title = (title or Path(files[0][0]).stem).strip()
    if not title or len(title) > 512:
        raise ValueError("标题不能为空且不能超过 512 字符")
    # Validate every input before writing blobs or committing a library item.
    validated = [(name, *validate_content(name, data)) for name, data in normalized]
    if tags is not None:
        normalize_tags(tags)
    item = existing or Item(title=title, type=item_type, source=source, source_id=source_id, source_url=source_url, author=author, description=description, language=language)
    try:
        for position, (name, data, mime) in enumerate(validated):
            if checkpoint:
                checkpoint(93 + int(3 * position / len(validated)))
            relative, digest = save_blob(data, source=source)
            item.assets.append(Asset(position=position, relative_path=relative, original_name=name, mime_type=mime, sha256=digest, size_bytes=len(data), status="available"))
        db.add(item)
        db.flush()
        if tags:
            set_tags(db, item, tags, "source" if source != "local" else "manual")
        item.content_state, item.content_error = "ready", None
        if before_commit:
            before_commit(db)
        db.commit()
    except IntegrityError:
        db.rollback()
        if source_id:
            existing = db.scalar(select(Item).where(Item.source == source, Item.source_id == source_id))
            if existing:
                return existing
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(item)
    return item


def import_local_paths(db: Session, paths: list[Path], **kwargs) -> Item:
    total = 0
    files = []
    for path in paths:
        total += path.stat().st_size
        if path.stat().st_size > settings.max_file_bytes or total > settings.max_import_bytes or len(files) >= settings.max_import_files:
            raise ValueError("导入文件大小或数量超过限制")
        with path.open("rb") as stream:
            files.append((path.name, stream.read(settings.max_file_bytes + 1)))
    return import_files(db, files, **kwargs)


def iso(value):
    return value.isoformat() + "Z" if value else None


def build_item_view(item: Item, include_assets: bool = False) -> dict:
    assets = sorted(item.assets, key=lambda a: a.position)
    cover = next((a for a in assets if a.mime_type.startswith("image/") and a.status == "available"), None)
    value = {"id": item.id, "title": item.title, "type": item.type, "source": item.source, "source_url": item.source_url, "description": item.description, "author": item.author, "language": item.language, "rating": item.rating, "rating_origin": item.rating_origin, "tags": [{"id": link.tag_id, "name": link.tag.name, "origin": link.origin, "score": link.score} for link in item.item_tags], "page_count": len(assets), "size_bytes": sum(a.size_bytes for a in assets), "cover_url": f"/api/assets/{cover.id}/thumbnail" if cover else None, "created_at": iso(item.created_at), "updated_at": iso(item.updated_at), "progress": {"page": item.progress.page} if item.progress else None}
    missing = bool(assets) and any(asset.status != "available" for asset in assets)
    value.update(content_state=("failed" if missing else "ready") if assets else item.content_state,
                 content_error="正文文件缺失，请检查 NAS 或恢复备份" if missing else item.content_error,
                 content_job_id=item.content_job_id, remote_page_count=item.remote_page_count,
                 source_series=item.source_series)
    value["title_zh"] = item.title_zh if item.title_zh_source == item.title else None
    value["display_title"] = value["title_zh"] or item.title
    value["author_source_id"] = item.author_source_id
    value["source_id"] = item.source_id
    value["source_published_at"] = iso(item.source_published_at) if item.source_published_at else None
    value.update(is_favorite=item.favorite is not None, user_score=item.user_score or 0,
                 favorite_group_ids=sorted(link.group_id for link in item.favorite.groups) if item.favorite else [],
                 source_bookmark_count=item.source_bookmark_count)
    if not assets and item.type not in ("novel", "article") and item.remote_page_count:
        value["page_count"] = item.remote_page_count
    for tag_view, link in zip(value["tags"], item.item_tags):
        tag_view.update(name_zh=link.tag.name_zh, display_name=link.tag.name_zh or link.tag.name)
    if item.progress:
        value["progress"].update(section=item.progress.section, offset=item.progress.offset)
    if item.preview_cover:
        value["cover_url"] = f"/api/items/{item.id}/cover"
    if include_assets:
        value["assets"] = [{"id": a.id, "position": a.position, "original_name": a.original_name, "mime_type": a.mime_type, "size_bytes": a.size_bytes, "status": a.status, "url": f"/api/assets/{a.id}/content"} for a in assets]
    return value


def reconcile_assets(db: Session) -> None:
    """Recover missing/restored blobs without deleting files or entire items."""
    for asset in db.scalars(select(Asset)).yield_per(500):
        path = resolve_asset_path(asset.relative_path)
        asset.status = "available" if path.is_file() and path.stat().st_size == asset.size_bytes else "missing"
    db.commit()
