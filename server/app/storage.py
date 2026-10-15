"""Immutable content-addressed files. Files commit before DB references become visible.

A crash before DB commit leaves a harmless unreferenced blob, never a successful
item pointing at an unfinished file. No automatic blob deletion is performed.
"""
import hashlib
import io
import os
import re
import tempfile
from pathlib import Path

from .config import settings

MARKER = ".library-storage"
MARKER_CONTENT = "qiye-library-v1\n"
COLLECTIONS = {
    "pixiv": "Pixiv", "ehentai": "E-Hentai", "picacg": "哔咔 PicACG",
    "jm": "JM 天堂", "local": "本地书库",
}


def initialize_storage() -> None:
    root = settings.storage_root
    if settings.storage_require_marker:
        assert_storage_available()
    else:
        root.mkdir(parents=True, exist_ok=True)
        marker = root / MARKER
        if not marker.exists():
            marker.write_text(MARKER_CONTENT, encoding="utf-8")
        assert_storage_available()
    for source in COLLECTIONS:
        collection_directory(source).mkdir(parents=True, exist_ok=True)


def assert_storage_available() -> None:
    marker = settings.storage_root / MARKER
    try:
        valid = marker.is_file() and marker.read_text(encoding="utf-8").strip() == MARKER_CONTENT.strip()
    except OSError as exc:
        raise OSError("书库存储不可用，请检查磁盘或 NAS 挂载") from exc
    if not valid:
        raise OSError("书库存储标记缺失，请检查 NAS 挂载与 .library-storage 标记")


def resolve_asset_path(relative_path: str) -> Path:
    assert_storage_available()
    root = settings.storage_root.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Invalid storage path")
    return path


def validate_collection(source: str) -> str:
    if source not in COLLECTIONS:
        raise ValueError("不支持的书库来源")
    return source


def collection_directory(source: str) -> Path:
    validate_collection(source)
    relative = f"collections/{source}"
    path = resolve_asset_path(relative)
    # A symlink from one collection to another would defeat physical separation.
    if path != settings.storage_root.resolve() / relative:
        raise OSError("来源书库目录不能重定向至其他路径")
    return path


def collection_blob_path(source: str, digest: str) -> str:
    validate_collection(source)
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("文件 SHA-256 无效")
    return f"collections/{source}/blobs/{digest[:2]}/{digest[2:4]}/{digest}"


def verify_blob(path: Path, digest: str, size: int) -> None:
    if size < 0 or not path.is_file() or path.stat().st_size != size:
        raise OSError("文件缺失或大小校验失败，请检查存储设备")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise OSError("文件完整性校验失败，请检查存储设备")


def _save_stream(stream, *, source: str, digest: str, size: int) -> tuple[str, str]:
    relative = collection_blob_path(source, digest)
    target = resolve_asset_path(relative)
    if target != settings.storage_root.resolve() / relative:
        raise OSError("来源书库文件不能重定向至其他路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        verify_blob(target, digest, size)
        return relative, digest
    # Temporary file is on the destination filesystem so rename is atomic.
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as destination:
            written = 0
            while chunk := stream.read(1024 * 1024):
                written += len(chunk)
                if written > size:
                    raise OSError("源文件在保存期间发生变化")
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        verify_blob(Path(temporary), digest, size)
        assert_storage_available()
        os.replace(temporary, target)
        if os.name != "nt":
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return relative, digest


def save_blob(data: bytes, *, source: str = "local") -> tuple[str, str]:
    """Deduplicate within a collection; identical bytes in other collections copy."""
    assert_storage_available()
    return _save_stream(io.BytesIO(data), source=source, digest=hashlib.sha256(data).hexdigest(), size=len(data))


def copy_blob_to_collection(relative_path: str, *, source: str, digest: str, size: int) -> str:
    """Copy and verify immutable content, retaining its original file forever."""
    original = resolve_asset_path(relative_path)
    verify_blob(original, digest, size)
    with original.open("rb") as stream:
        relative, _ = _save_stream(stream, source=source, digest=digest, size=size)
    return relative
