"""Administrator-published APKs, verified by Android's apksigner before publication.

Highest versionCode is always current, including concurrent uploads. Blobs commit
before database references and are covered by the existing collection backup.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .auth import require_admin_cookie, require_auth
from .database import get_db
from .models import AndroidRelease
from .storage import _save_stream, assert_storage_available, resolve_asset_path, verify_blob

PACKAGE = "app.qiye.qiye_library"
CERTIFICATE = "5a93e25be4ef0f0e68033ab47cc8a767e8844c518a2714eaf5b3d97c976188e2"
MAX_APK_BYTES = 256 * 1024 * 1024
_upload_lock = Lock()
router = APIRouter(prefix="/app-updates", tags=["app-updates"])


def tool(command):
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=90, check=False)
            output.seek(0)
            data = output.read(128 * 1024 + 1)
        if result.returncode or len(data) > 128 * 1024:
            raise HTTPException(400, "APK 无效或签名校验失败，请上传正式签名的完整 APK")
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    except (FileNotFoundError, PermissionError):
        raise HTTPException(503, "服务器未安装 APK 校验工具，请安装 apksigner、aapt 和 Java")
    except subprocess.TimeoutExpired:
        raise HTTPException(400, "APK 校验超时")


def inspect_apk(path):
    signature = tool([os.getenv("LIBRARY_APKSIGNER", "apksigner"), "verify", "--verbose", "--print-certs", str(path)])
    certs = re.findall(r"^Signer #\d+ certificate SHA-256 digest: ([a-fA-F0-9]{64})$", signature, re.M)
    expected = os.getenv("LIBRARY_ANDROID_CERT_SHA256", CERTIFICATE).lower()
    if certs != [expected] or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise HTTPException(400, "APK 签名与栖页发布证书不一致")
    if "Verified using v2 scheme (APK Signature Scheme v2): true" not in signature:
        raise HTTPException(400, "APK 必须包含有效的 v2 签名")
    metadata = tool([os.getenv("LIBRARY_AAPT", "aapt"), "dump", "badging", str(path)])
    package = re.search(r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'", metadata, re.M)
    sdk = re.search(r"^sdkVersion:'(\d+)'", metadata, re.M)
    if (not package or package[1] != PACKAGE or not sdk or "application-debuggable" in metadata
            or not 1 <= int(package[2]) <= 2100000000 or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,79}", package[3])):
        raise HTTPException(400, "APK 包名、版本或正式发布配置无效")
    return dict(version_code=int(package[2]), version_name=package[3], min_sdk=int(sdk[1]), certificate_sha256=expected)


def view(release):
    return {"package_name": PACKAGE, "version_code": release.version_code, "version_name": release.version_name,
            "sha256": release.sha256, "size_bytes": release.size_bytes, "min_sdk": release.min_sdk,
            "certificate_sha256": release.certificate_sha256, "notes": release.notes,
            "published_at": release.created_at.isoformat() + "Z",
            "download_path": f"/api/app-updates/android/{release.sha256}/apk"}


@router.get("/android", dependencies=[Depends(require_auth)])
def latest(db=Depends(get_db)):
    release = db.scalar(select(AndroidRelease).order_by(AndroidRelease.version_code.desc()).limit(1))
    return {"release": view(release) if release else None}


def publish(path, notes, db):
    metadata = inspect_apk(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        previous = db.get(AndroidRelease, metadata["version_code"])
        if previous:
            if previous.sha256 != digest:
                raise HTTPException(409, "此版本号已发布其他安装包，请提高 versionCode 后重新构建")
            return {"release": view(previous)}
        latest_code = db.scalar(select(AndroidRelease.version_code).order_by(AndroidRelease.version_code.desc()).limit(1))
        if latest_code and metadata["version_code"] <= latest_code:
            raise HTTPException(409, "只能发布更高版本号的安装包")
        relative, _ = _save_stream(stream, source="local", digest=digest, size=size)
    release = AndroidRelease(**metadata, sha256=digest, size_bytes=size, relative_path=relative, notes=notes.strip())
    db.add(release)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "此版本已由另一请求发布，请刷新后查看")
    db.refresh(release)
    return {"release": view(release)}


@router.post("/android", dependencies=[Depends(require_admin_cookie)])
async def upload(request: Request, notes: str = Query("", max_length=2000), db=Depends(get_db)):
    if request.headers.get("content-type", "").split(";")[0] != "application/vnd.android.package-archive":
        raise HTTPException(415, "请选择 APK 安装包")
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(400, "文件大小无效")
    if length < 0 or length > MAX_APK_BYTES:
        raise HTTPException(413, "APK 不能超过 256 MiB")
    if not _upload_lock.acquire(blocking=False):
        raise HTTPException(409, "另一安装包正在发布，请稍后重试")
    temporary = None
    try:
        assert_storage_available()
        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as target:
            temporary = Path(target.name)
            size = 0
            async with asyncio.timeout(300):
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_APK_BYTES:
                        raise HTTPException(413, "APK 不能超过 256 MiB")
                    await run_in_threadpool(target.write, chunk)
            if not size or (length and length != size):
                raise HTTPException(400, "APK 文件为空或上传不完整")
        return await run_in_threadpool(publish, temporary, notes, db)
    except TimeoutError:
        raise HTTPException(408, "上传超时，请重试")
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        _upload_lock.release()


@router.get("/android/{digest}/apk", dependencies=[Depends(require_auth)])
def download(digest: str, db=Depends(get_db)):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise HTTPException(404, "安装包不存在")
    release = db.scalar(select(AndroidRelease).where(AndroidRelease.sha256 == digest))
    if release is None:
        raise HTTPException(404, "安装包不存在")
    path = resolve_asset_path(release.relative_path)
    verify_blob(path, release.sha256, release.size_bytes)
    return FileResponse(path, media_type="application/vnd.android.package-archive",
                        filename=f"qiye-android-{release.version_code}.apk")
