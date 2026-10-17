"""WD Tagger service; image files exist only for the duration of a request."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from fastapi import Depends, FastAPI, HTTPException, Request
from PIL import Image

from .common import MAX_IMAGE, ControllerGate, authorized, controller_key

_inference = BoundedSemaphore(1)


@asynccontextmanager
async def lifespan(app):
    controller_key()
    yield


app = FastAPI(title="栖页图像识别", lifespan=lifespan, dependencies=[Depends(authorized)])
app.add_middleware(ControllerGate)


@app.get("/v1/status")
def status():
    from app.ai.service import model_status
    return model_status()


@app.post("/v1/vision/analyze")
async def analyze(request: Request):
    if not _inference.acquire(blocking=False):
        raise HTTPException(429, "图像识别服务繁忙，请稍后重试")
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_IMAGE:
                raise HTTPException(413, "单张图片超过 16 MiB")
        with TemporaryDirectory(prefix="qiye-vision-") as directory:
            path = Path(directory) / "image"
            path.write_bytes(raw)
            try:
                with Image.open(path) as image:
                    if image.width * image.height > 36_000_000:
                        raise ValueError()
                    image.verify()
                from app.ai.service import infer_image
                from starlette.concurrency import run_in_threadpool
                return await run_in_threadpool(infer_image, path)
            except Exception as exc:
                from app.integrations.base import ConnectorError
                if isinstance(exc, ConnectorError):
                    raise HTTPException(503, str(exc)) from None
                raise HTTPException(422, "图片或图像模型不可用，请检查节点模型配置") from None
    finally:
        _inference.release()
