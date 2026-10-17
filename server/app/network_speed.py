"""Bounded, authenticated measurements of the client-to-library connection."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from threading import Lock
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, StreamingResponse

from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
DEFAULT_BYTES = 8 * 1024 * 1024
MAX_BYTES = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
TRANSFER_SECONDS = 30
MAX_TRANSFERS = 4
_active_lock = Lock()
_active: set[str] = set()
_HEADERS = {"Cache-Control": "no-store, no-transform", "Pragma": "no-cache"}


@contextmanager
def _transfer_slot(request: Request):
    # The set contains only active requests, never raw credentials. All admin
    # sessions share one slot; paired devices each have their own slot.
    identity = getattr(request.state, "device_id", None) or "administrator"
    with _active_lock:
        if identity in _active or len(_active) >= MAX_TRANSFERS:
            raise HTTPException(429, "服务器测速正在进行，请稍后重试", headers={"Retry-After": "5"})
        _active.add(identity)
    try:
        yield
    finally:
        with _active_lock:
            _active.discard(identity)


class _SpeedDownload(StreamingResponse):
    def __init__(self, request: Request, size: int):
        self._slot = _transfer_slot(request)
        self._slot.__enter__()

        async def chunks():
            remaining = size
            while remaining:
                length = min(CHUNK_BYTES, remaining)
                # Fresh entropy keeps compression from inflating measured speed;
                # generation and network backpressure retain only a small chunk.
                yield os.urandom(length)
                remaining -= length

        super().__init__(chunks(), media_type="application/octet-stream", headers={
            **_HEADERS, "Content-Length": str(size), "Content-Encoding": "identity",
            "X-Accel-Buffering": "no",
        })

    async def __call__(self, scope, receive, send):
        try:
            # Cover both producing bytes and waiting for a slow socket to accept
            # them. Disconnects, cancellations and timeouts all release the slot.
            async with asyncio.timeout(TRANSFER_SECONDS):
                await super().__call__(scope, receive, send)
        finally:
            self._slot.__exit__(None, None, None)


@router.get("/network/speed/ping")
async def ping():
    return JSONResponse({"ok": True, "max_bytes": MAX_BYTES,
                         "default_bytes": DEFAULT_BYTES, "timeout_seconds": TRANSFER_SECONDS}, headers=_HEADERS)


@router.get("/network/speed/download")
async def download(request: Request, size: int = Query(DEFAULT_BYTES, alias="bytes", ge=1, le=MAX_BYTES)):
    return _SpeedDownload(request, size)


@router.post("/network/speed/upload")
async def upload(request: Request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
        raise HTTPException(415, "测速上传须使用 application/octet-stream")
    if request.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise HTTPException(415, "测速上传不支持压缩内容")
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdecimal():
            raise HTTPException(400, "测速数据长度无效")
        if len(declared) > 10 or int(declared) > MAX_BYTES:
            raise HTTPException(413, "测速数据不能超过 32 MiB")
        declared = int(declared)
    received = 0
    start = time.monotonic()
    with _transfer_slot(request):
        try:
            async with asyncio.timeout(TRANSFER_SECONDS):
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > MAX_BYTES:
                        raise HTTPException(413, "测速数据不能超过 32 MiB")
        except TimeoutError:
            raise HTTPException(408, "测速上传超时，请检查手机到服务器的连接") from None
        except ClientDisconnect:
            raise HTTPException(400, "测速上传已中断") from None
        if declared is not None and received != declared:
            raise HTTPException(400, "测速上传数据不完整")
        return JSONResponse({"received_bytes": received,
                             "elapsed_ms": round((time.monotonic() - start) * 1000, 3)}, headers=_HEADERS)
