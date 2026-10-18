"""Attempt-scoped disk storage for downloaded pages, instead of whole-book RAM.

Anonymous temporary files close on success, failure, cancellation or process
exit. They are never exposed as library assets until all files validate.
"""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
import os
from tempfile import TemporaryFile
from typing import BinaryIO

from .config import settings


_attempt = ContextVar("download_staging", default=None)


@dataclass
class StagedFile:
    stream: BinaryIO
    offset: int
    size: int

    def read(self):
        self.stream.seek(self.offset)
        data = self.stream.read(min(self.size, settings.max_file_bytes + 1))
        if len(data) != self.size or len(data) > settings.max_file_bytes:
            raise ValueError("下载暂存文件大小校验失败")
        return data

    def reader(self):
        return _SliceReader(self)


class _SliceReader:
    def __init__(self, file):
        self.file, self.position = file, 0

    def read(self, size=-1):
        remaining = self.file.size - self.position
        size = remaining if size < 0 else min(remaining, size)
        self.file.stream.seek(self.file.offset + self.position)
        data = self.file.stream.read(size)
        self.position += len(data)
        return data


class _StagingArea:
    def __init__(self, stack):
        self.stack, self.stream = stack, None

    def store(self, data):
        if self.stream is None:
            self.stream = self.stack.enter_context(TemporaryFile(dir=os.getenv("LIBRARY_DOWNLOAD_TEMP_DIR") or None))
        self.stream.seek(0, 2)
        offset = self.stream.tell()
        self.stream.write(data)
        self.stream.flush()
        return StagedFile(self.stream, offset, len(data))


def stage(data):
    area = _attempt.get()
    if area is None:
        return data
    if len(data) > settings.max_file_bytes:
        raise ValueError("文件超过单文件大小限制")
    return area.store(data)


class DownloadFiles(list):
    def append(self, entry):
        name, data = entry
        super().append((name, stage(data)))


@contextmanager
def staging_context():
    with ExitStack() as stack:
        token = _attempt.set(_StagingArea(stack))
        try:
            yield
        finally:
            _attempt.reset(token)
