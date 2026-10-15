import inspect
import io
import os
import tempfile
import warnings
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from PIL import Image, ImageOps
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .auth import COOKIE_NAME, check_password, is_authenticated, issue_session, require_auth, verify_origin
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .library import SUPPORTED_EXTENSIONS, build_item_view, import_files, import_local_paths, iso, natural_key, reconcile_assets, set_tags
from .models import Asset, Item, ItemTag, Job, ReadingProgress, Tag, utcnow
from .schemas import FolderImport, ItemPatch, LoginRequest, ProgressUpdate
from .storage import assert_storage_available, initialize_storage, resolve_asset_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    from .worker_runtime import prepare
    prepare()  # Pin API before proxy, request-pool or queue threads are created.
    initialize_storage()
    # Production schema changes are handled by Alembic before launching uvicorn.
    if settings.environment != "production":
        from sqlalchemy import inspect as inspect_schema
        fresh_favorites = not inspect_schema(engine).has_table("favorite_groups")
        Base.metadata.create_all(engine)
        if fresh_favorites:
            from .favorites_api import seed_default_groups
            with SessionLocal() as db:
                seed_default_groups(db)
    with SessionLocal() as db:
        reconcile_assets(db)
    from .managed_proxy import start_service, stop_service
    start_service()
    try:
        from .jobs import start_worker, stop_worker
    except ModuleNotFoundError as exc:
        if exc.name != "app.jobs":
            raise
        start_worker = stop_worker = None
    if start_worker:
        result = start_worker()
        if inspect.isawaitable(result):
            await result
    try:
        yield
    finally:
        if stop_worker:
            result = stop_worker()
            if inspect.isawaitable(result):
                await result
        stop_service()


app = FastAPI(title="栖页 · 私人图书馆", version="0.1.0", lifespan=lifespan, docs_url="/api/docs" if settings.environment != "production" else None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        if request.url.path.startswith("/api/"):
            verify_origin(request)
        if request.headers.get("authorization") is not None:
            from .device_auth import authenticate_device
            await run_in_threadpool(authenticate_device, request)
        response = await call_next(request)
    except HTTPException as exc:
        response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def value_error(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc):
    # Pydantic normally includes raw input, which can be a password, token or code.
    details = [{key: entry[key] for key in ("type", "loc", "msg") if key in entry}
               for entry in exc.errors()]
    return JSONResponse({"detail": details}, status_code=422)


@app.exception_handler(OSError)
async def storage_error(_request, _exc):
    return JSONResponse({"detail": "文件存储不可用，请检查磁盘空间、权限与 NAS 挂载"}, status_code=503)


@app.get("/api/health")
def health():
    db_ok = storage_ok = False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        pass
    try:
        assert_storage_available()
        storage_ok = True
    except OSError:
        pass
    return JSONResponse({"status": "ok" if db_ok and storage_ok else "degraded", "database": "ok" if db_ok else "unavailable", "storage": "ok" if storage_ok else "unavailable"}, status_code=200 if db_ok and storage_ok else 503)


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"authenticated": is_authenticated(request)}


@app.post("/api/auth/login")
def login(data: LoginRequest, request: Request, response: Response):
    check_password(data.password, request)
    response.set_cookie(COOKIE_NAME, issue_session(), max_age=settings.session_seconds, httponly=True, secure=settings.secure_cookie, samesite="strict", path="/")
    return {"ok": True}


api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


@api.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.secure_cookie, samesite="strict")
    return {"ok": True}


def get_item(item_id: str, db: Session) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "作品不存在")
    return item


@api.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    recent = db.scalars(select(Item).order_by(Item.created_at.desc()).limit(8)).all()
    return {"total_items": db.scalar(select(func.count(Item.id))) or 0, "total_pages": db.scalar(select(func.count(Asset.id))) or 0, "total_bytes": db.scalar(select(func.sum(Asset.size_bytes))) or 0, "pending_jobs": db.scalar(select(func.count(Job.id)).where(Job.status.in_(["queued", "running"]))) or 0, "untagged_items": db.scalar(select(func.count(Item.id)).where(~Item.item_tags.any())) or 0, "recent_items": [build_item_view(item) for item in recent], "source_counts": dict(db.execute(select(Item.source, func.count(Item.id)).group_by(Item.source)).all()), "rating_counts": dict(db.execute(select(Item.rating, func.count(Item.id)).group_by(Item.rating)).all())}


@api.get("/items")
def list_items(q: str = Query("", max_length=512), type: str = "", source: str = "", tag: str = "", author: str = Query("", max_length=512), rating: str = "", downloaded: bool = False, favorite: bool = False, favorite_group: str = Query("", max_length=36), history: bool = False, page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100), sort: str = "latest", db: Session = Depends(get_db)):
    query = select(Item)
    if favorite or favorite_group:
        from .models import FavoriteGroup, FavoriteGroupItem, ItemFavorite
        query = query.where(Item.favorite.has())
        if favorite_group == "ungrouped":
            query = query.where(Item.favorite.has(~ItemFavorite.groups.any()))
        elif favorite_group:
            if db.get(FavoriteGroup, favorite_group) is None:
                raise HTTPException(404, "收藏分组不存在")
            query = query.where(Item.favorite.has(ItemFavorite.groups.any(FavoriteGroupItem.group_id == favorite_group)))
    if history:
        from .models import ItemVisit
        query = query.join(ItemVisit)
    if downloaded:
        from .library import downloaded_content
        query = query.where(downloaded_content())
    if q.strip():
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(or_(Item.title.ilike(f"%{term}%", escape="\\"), and_(Item.title_zh_source == Item.title, Item.title_zh.ilike(f"%{term}%", escape="\\")), Item.author.ilike(f"%{term}%", escape="\\"), Item.description.ilike(f"%{term}%", escape="\\"), Item.item_tags.any(ItemTag.tag.has(or_(Tag.name.ilike(f"%{term}%", escape="\\"), Tag.name_zh.ilike(f"%{term}%", escape="\\"))))))
    if author:
        query = query.where(Item.author == author)
    for field, value in [(Item.type, type), (Item.source, source), (Item.rating, rating)]:
        if value:
            query = query.where(field == value)
    if tag:
        query = query.where(Item.item_tags.any(ItemTag.tag.has(or_(Tag.name == tag, Tag.id == tag))))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    ordering = Item.title.asc() if sort == "title" else Item.created_at.asc() if sort == "oldest" else Item.created_at.desc()
    if history:
        ordering = ItemVisit.viewed_at.desc()
    items = db.scalars(query.order_by(ordering, Item.id).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [build_item_view(item) for item in items], "total": total, "page": page, "page_size": page_size}


@api.get("/items/{item_id}")
def item_detail(item_id: str, db: Session = Depends(get_db)):
    from .series_service import item_series
    item = build_item_view(get_item(item_id, db), True)
    item["series"] = item_series(db, item_id)
    if item["source"] == "pixiv" and not item["author_source_id"] and item["author"]:
        from .models import Subscription
        candidates = db.scalars(select(Subscription.target).where(Subscription.platform == "pixiv",
            Subscription.target_type == "author", Subscription.label == item["author"]).distinct().limit(2)).all()
        if len(candidates) == 1:
            item["author_source_id"] = candidates[0]
    return item


@api.get("/items/{item_id}/cover")
def preview_cover(item_id: str, db: Session = Depends(get_db)):
    preview = get_item(item_id, db).preview_cover
    if not isinstance(preview, dict) or preview.get("mime_type") != "image/webp":
        raise HTTPException(404, "封面尚未保存")
    path = resolve_asset_path(preview["relative_path"])
    if not path.is_file() or path.stat().st_size != preview["size_bytes"]:
        raise HTTPException(503, "封面文件缺失或存储不可用，请检查 NAS 后重试")
    return FileResponse(path, media_type="image/webp", headers={"Content-Security-Policy": "default-src 'none'; sandbox"})


@api.patch("/items/{item_id}")
def patch_item(item_id: str, data: ItemPatch, db: Session = Depends(get_db)):
    item = get_item(item_id, db)
    values = data.model_dump(exclude_unset=True)
    for key, value in values.items():
        if key == "tags":
            if value is not None:
                set_tags(db, item, value)
        elif value is not None:
            if key == "title" and not value.strip():
                raise HTTPException(400, "标题不能为空")
            if key == "title" and value != item.title:
                item.title_zh, item.title_zh_source = None, None
            setattr(item, key, value)
            if key == "rating":
                item.rating_origin = "manual"
    item.updated_at = utcnow()
    db.commit()
    db.refresh(item)
    return build_item_view(item, True)


@api.delete("/items/{item_id}")
def delete_item(item_id: str, db: Session = Depends(get_db)):
    from .models import Series, SeriesEntry
    item = get_item(item_id, db)
    for series in db.scalars(select(Series).join(SeriesEntry).where(SeriesEntry.item_id == item_id).with_for_update()):
        series.revision += 1
        if series.origin == "source":
            series.complete = False
    db.delete(item)
    db.commit()
    return {"ok": True}


@api.post("/imports/upload")
def upload(files: list[UploadFile] = File(...), title: str | None = Form(None), type: str = Form("manga"), db: Session = Depends(get_db)):
    if len(files) > settings.max_import_files:
        raise HTTPException(413, "文件数量超过限制")
    entries, total = [], 0
    for file in files:
        data = file.file.read(settings.max_file_bytes + 1)
        total += len(data)
        if len(data) > settings.max_file_bytes or total > settings.max_import_bytes:
            raise HTTPException(413, "导入文件超过大小限制")
        entries.append((file.filename or "upload", data))
    return build_item_view(import_files(db, entries, title=title, item_type=type), True)


@api.post("/imports/folder")
def folder_import(data: FolderImport, db: Session = Depends(get_db)):
    root = settings.import_root.resolve()
    requested = Path(data.path)
    folder = (requested if requested.is_absolute() else root / requested).resolve()
    if not folder.is_relative_to(root) or not folder.is_dir():
        raise HTTPException(400, "只能导入 LIBRARY_IMPORT_ROOT 内存在的文件夹")
    files = []
    for path in folder.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise HTTPException(400, "导入目录不允许符号链接或外部路径")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
            if len(files) > settings.max_import_files:
                raise HTTPException(413, "文件数量超过限制")
    files.sort(key=lambda p: natural_key(str(p.relative_to(folder))))
    return build_item_view(import_local_paths(db, files, title=data.title or folder.name, item_type=data.type), True)


def get_asset(asset_id: str, db: Session) -> tuple[Asset, Path]:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "文件不存在")
    path = resolve_asset_path(asset.relative_path)
    if not path.is_file() or path.stat().st_size != asset.size_bytes:
        raise HTTPException(503, "原文件缺失或存储不可用，请检查 NAS 后重试")
    return asset, path


@api.get("/assets/{asset_id}/content")
def asset_content(asset_id: str, db: Session = Depends(get_db)):
    asset, path = get_asset(asset_id, db)
    # Text is always served as plain text, never active HTML or SVG.
    return FileResponse(path, media_type=asset.mime_type, headers={"Content-Security-Policy": "default-src 'none'; sandbox"})


@api.get("/assets/{asset_id}/text-sections")
def asset_text_sections(asset_id: str, db: Session = Depends(get_db)):
    from .novel_text import text_sections
    asset, path = get_asset(asset_id, db)
    if not asset.mime_type.startswith("text/"):
        raise HTTPException(400, "仅文本文件支持章节阅读")
    return {"sections": text_sections(path)}


@api.get("/assets/{asset_id}/thumbnail")
def asset_thumbnail(asset_id: str, db: Session = Depends(get_db)):
    asset, path = get_asset(asset_id, db)
    if not asset.mime_type.startswith("image/"):
        raise HTTPException(400, "该文件不是图片")
    cached = resolve_asset_path(f"thumbnails/{asset.sha256[:2]}/{asset.sha256}.webp")
    if not cached.exists():
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as original:
                picture = ImageOps.exif_transpose(original)
                picture.thumbnail((480, 640))
                picture = picture.convert("RGB")
                buffer = io.BytesIO()
                picture.save(buffer, "WEBP", quality=80)
        assert_storage_available()
        cached.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=cached.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(buffer.getvalue())
            os.replace(temporary, cached)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return FileResponse(cached, media_type="image/webp")


@api.get("/items/{item_id}/export")
def export_item(item_id: str, db: Session = Depends(get_db)):
    item = get_item(item_id, db)
    if not item.assets:
        raise HTTPException(409, "作品仅含元数据，请先下载原文件后再导出")
    fd, temporary = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for asset in item.assets:
                _, path = get_asset(asset.id, db)
                archive.write(path, f"{asset.position + 1:04d}_{asset.original_name}")
        extension = "cbz" if all(a.mime_type.startswith("image/") for a in item.assets) else "zip"
        return FileResponse(temporary, media_type="application/zip", filename=f"{item.title[:100]}.{extension}", background=BackgroundTask(os.unlink, temporary))
    except Exception:
        os.unlink(temporary)
        raise


@api.put("/items/{item_id}/progress")
def update_progress(item_id: str, data: ProgressUpdate, db: Session = Depends(get_db)):
    item = get_item(item_id, db)
    if data.page >= len(item.assets):
        raise HTTPException(400, "阅读位置超出页数范围")
    asset = sorted(item.assets, key=lambda entry: entry.position)[data.page]
    if asset.mime_type.startswith("text/"):
        from .novel_text import text_sections
        _, path = get_asset(asset.id, db)
        if data.section >= len(text_sections(path)):
            raise HTTPException(400, "阅读位置超出章节范围")
    elif data.section != 0:
        raise HTTPException(400, "图片页不支持小说章节位置")
    if item.progress is None:
        item.progress = ReadingProgress(page=data.page, section=data.section, offset=data.offset)
    else:
        item.progress.page = data.page
        item.progress.section, item.progress.offset = data.section, data.offset
        item.progress.updated_at = utcnow()
    db.commit()
    return {"page": item.progress.page, "section": item.progress.section, "offset": item.progress.offset,
            "updated_at": iso(item.progress.updated_at)}


@api.get("/tags")
def list_tags(q: str = Query("", max_length=191), source: str = "", limit: int | None = Query(None, ge=1, le=1000), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = select(Tag.id, Tag.name, Tag.name_zh, func.count(func.distinct(ItemTag.item_id))).join(ItemTag).join(Item, Item.id == ItemTag.item_id)
    if q.strip():
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(or_(Tag.name.ilike(f"%{term}%", escape="\\"), Tag.name_zh.ilike(f"%{term}%", escape="\\")))
    if source:
        query = query.where(Item.source == source)
    query = query.group_by(Tag.id, Tag.name, Tag.name_zh).order_by(Tag.name, Tag.id).offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return [{"id": id_, "name": name, "name_zh": name_zh, "display_name": name_zh or name, "count": count} for id_, name, name_zh, count in db.execute(query)]


@api.get("/settings")
def public_settings():
    available = True
    try:
        assert_storage_available()
    except OSError:
        available = False
    return {"storage_root": str(settings.storage_root), "import_root": str(settings.import_root), "database_kind": engine.dialect.name, "storage_available": available, "ai_mode": "local"}


app.include_router(api)
from .pixiv_accounts import router as pixiv_accounts_router
app.include_router(pixiv_accounts_router, prefix="/api")
from .app_updates import router as app_updates_router
app.include_router(app_updates_router, prefix="/api")
from .database_export import router as database_export_router
app.include_router(database_export_router, prefix="/api")
from .device_api import router as device_router
app.include_router(device_router, prefix="/api")
from .favorites_api import router as favorites_router
app.include_router(favorites_router, prefix="/api")
from .series_api import router as series_router
app.include_router(series_router, prefix="/api")
from .managed_proxy_api import router as managed_proxy_router
app.include_router(managed_proxy_router, prefix="/api")
from .collections_api import router as collections_router
app.include_router(collections_router, prefix="/api")
from .source_auth_api import router as source_auth_router
app.include_router(source_auth_router, prefix="/api")
from .network_api import router as network_router
app.include_router(network_router, prefix="/api")
from .character_search import router as character_search_router
app.include_router(character_search_router, prefix="/api")
from .character_wall import router as character_wall_router
app.include_router(character_wall_router, prefix="/api")
from .character_image_search import router as character_image_router
app.include_router(character_image_router, prefix="/api")
from .translation import router as translation_router
app.include_router(translation_router, prefix="/api")
from .author_api import router as author_router
app.include_router(author_router, prefix="/api")
try:
    from .integration_api import router as integration_router
except ModuleNotFoundError as exc:
    if exc.name != "app.integration_api":
        raise
else:
    app.include_router(integration_router, prefix="/api")


FRONTEND_DIST = Path(os.getenv("LIBRARY_FRONTEND_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist"))).resolve()


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "API 路由不存在")
    requested = (FRONTEND_DIST / path).resolve()
    if not requested.is_relative_to(FRONTEND_DIST):
        raise HTTPException(404, "页面不存在")
    if requested.is_file():
        return FileResponse(requested)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(404, "前端尚未构建，请先运行 npm run build")
