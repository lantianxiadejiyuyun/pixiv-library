import json
import os
import re
import secrets
import time

import httpx
from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

MAX_RESPONSE = 256 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MODEL = r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,190}"


def controller_key():
    value = os.getenv("QIYE_CONTROLLER_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value):
        raise RuntimeError("QIYE_CONTROLLER_KEY must contain 32–256 URL-safe characters")
    return value


def authorized(authorization: str = Header(default="")):
    expected = "Bearer " + controller_key()
    if not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "AI 控制器密钥无效")


class ControllerGate:
    """Authenticate before FastAPI parses a body and bound chunked requests."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        if not secrets.compare_digest(headers.get(b"authorization", b""), ("Bearer " + controller_key()).encode()):
            return await JSONResponse({"detail": "AI 控制器密钥无效"}, status_code=401)(scope, receive, send)
        maximum = MAX_IMAGE if scope.get("path") == "/v1/vision/analyze" else 512 * 1024
        try:
            if int(headers.get(b"content-length", b"0")) > maximum:
                return await JSONResponse({"detail": "请求体过大"}, status_code=413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({"detail": "请求长度无效"}, status_code=400)(scope, receive, send)
        size = 0
        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > maximum:
                raise HTTPException(413, "请求体过大")
            return message
        await self.app(scope, bounded_receive, send)


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(pattern="^" + MODEL + "$", max_length=191)


def json_request(method, url, *, headers=None, body=None, content=None, timeout=15, maximum=MAX_RESPONSE, transport=None):
    """No redirects or ambient proxies; remote errors never include response text."""
    try:
        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=5), trust_env=False,
                          follow_redirects=False, transport=transport) as client:
            payload = {"content": content} if content is not None else {"json": body}
            with client.stream(method, url, headers=headers, **payload) as response:
                if response.status_code not in (200, 201, 202, 204, 304):
                    raise HTTPException(502, f"AI 服务请求失败（HTTP {response.status_code}）")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > maximum or time.monotonic() > deadline:
                        raise HTTPException(502, "AI 服务响应超过大小或时间限制")
                return json.loads(raw) if raw else {}
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, "无法连接 AI 服务，或服务返回无效数据") from None
