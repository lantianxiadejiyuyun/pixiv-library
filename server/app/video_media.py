"""Recognize supported video containers without executing or transcoding them."""
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}


def video_mime(name, data):
    suffix = Path(name).suffix.lower()
    if suffix == ".webm":
        # EBML header + WebM DocType + Segment. Codec support is the player's
        # responsibility; HTML/error responses renamed as videos are rejected.
        head = data[:4096]
        if not (head.startswith(b"\x1a\x45\xdf\xa3") and b"\x42\x82\x84webm" in head
                and b"\x18\x53\x80\x67" in head and len(data) > 32):
            raise ValueError("WebM 视频容器无效或文件不完整")
        return "video/webm"
    if suffix not in VIDEO_EXTENSIONS:
        raise ValueError("不支持的视频格式")
    offset, atoms, count = 0, {}, 0
    while offset < len(data):
        if len(data) - offset < 8 or count > 100000:
            raise ValueError("视频容器无效或文件不完整")
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise ValueError("视频容器无效或文件不完整")
            size, header = int.from_bytes(data[offset + 8:offset + 16], "big"), 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise ValueError("视频文件不完整，未保存")
        atoms[kind] = max(atoms.get(kind, 0), size - header)
        offset += size
        count += 1
    if not atoms.get(b"moov") or not atoms.get(b"mdat") or (suffix != ".mov" and atoms.get(b"ftyp", 0) < 8):
        raise ValueError("视频缺少索引或媒体数据，未保存")
    return "video/quicktime" if suffix == ".mov" else "video/mp4"
