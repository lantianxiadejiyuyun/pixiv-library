"""Authenticated management and inference gateway for one Docker AI host."""
from contextlib import asynccontextmanager
import os
from threading import BoundedSemaphore
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import MAX_IMAGE, ControllerGate, ModelInput, authorized, controller_key, json_request
from .docker_engine import DockerEngine, SERVICES
from .operations import Operations

operations = Operations()
_chat_slots = BoundedSemaphore(2)


def service_url(service):
    value = os.getenv("QIYE_OLLAMA_URL" if service == "ollama" else "QIYE_VISION_URL",
                      "http://ollama:11434" if service == "ollama" else "http://vision:9011").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise HTTPException(503, "AI 节点内部服务地址配置无效")
    return value


@asynccontextmanager
async def lifespan(app):
    controller_key()
    operations.recover()
    yield


app = FastAPI(title="栖页 AI 控制器", lifespan=lifespan, dependencies=[Depends(authorized)])
app.add_middleware(ControllerGate)


@app.get("/v1/status")
def status():
    services = []
    for name in SERVICES:
        try:
            services.append(DockerEngine().status(name))
        except HTTPException as exc:
            services.append({"service": name, "state": "unavailable", "status": exc.detail})
    return {"protocol": "qiye-ai-controller-v1", "services": services,
            "capabilities": ["ollama", "vision", "container-control", "model-pull"]}


@app.get("/v1/vision/status")
def vision_status():
    return json_request("GET", service_url("vision") + "/v1/status", headers={"Authorization": "Bearer " + controller_key()})


@app.post("/v1/services/{service}/{action}")
def service_action(service: Literal["ollama", "vision"], action: Literal["start", "stop", "restart"]):
    return DockerEngine().action(service, action)


@app.get("/v1/ollama/models")
def models():
    installed = json_request("GET", service_url("ollama") + "/api/tags")
    running = json_request("GET", service_url("ollama") + "/api/ps")
    if not isinstance(installed.get("models"), list) or not isinstance(running.get("models"), list):
        raise HTTPException(502, "Ollama 模型目录格式无效")
    return {"models": installed["models"][:1000], "running": running["models"][:1000]}


@app.post("/v1/ollama/models/pull")
def pull(value: ModelInput):
    return operations.enqueue(value.model, service_url("ollama"))


@app.get("/v1/operations/{operation_id}")
def operation(operation_id: str):
    return operations.read(operation_id)


@app.get("/v1/operations")
def recent_operations():
    paths = sorted(operations.root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:20]
    return {"items": [operations.read(path.stem) for path in paths]}


@app.post("/v1/ollama/models/unload")
def unload(value: ModelInput):
    json_request("POST", service_url("ollama") + "/api/generate", body={"model": value.model, "keep_alive": 0, "stream": False}, timeout=90)
    return {"ok": True}


@app.delete("/v1/ollama/models")
def delete_model(value: ModelInput):
    json_request("DELETE", service_url("ollama") + "/api/delete", body={"model": value.model})
    return {"ok": True}


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=60000)


class ChatInput(ModelInput):
    messages: list[Message] = Field(min_length=1, max_length=64)
    stream: Literal[False] = False
    format: Literal["json"] = "json"
    options: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        if sum(len(message.content) for message in self.messages) > 100000:
            raise ValueError("对话内容过长")
        if set(self.options) - {"temperature", "num_predict"}:
            raise ValueError("不支持的推理参数")
        temperature, tokens = self.options.get("temperature", 0), self.options.get("num_predict", 4096)
        if type(temperature) not in (int, float) or not 0 <= temperature <= 2 or type(tokens) is not int or not 1 <= tokens <= 8192:
            raise ValueError("推理参数无效")
        return self


@app.post("/v1/ollama/api/chat")
def chat(value: ChatInput):
    if not _chat_slots.acquire(blocking=False):
        raise HTTPException(429, "Ollama 正在处理其他任务，请稍后重试")
    try:
        return json_request("POST", service_url("ollama") + "/api/chat", body=value.model_dump(), timeout=180)
    finally:
        _chat_slots.release()


@app.post("/v1/vision/analyze")
async def analyze(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_IMAGE:
            raise HTTPException(413, "单张图片超过 16 MiB")
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(json_request, "POST", service_url("vision") + "/v1/vision/analyze",
        headers={"Authorization": "Bearer " + controller_key(), "Content-Type": "application/octet-stream"},
        content=bytes(raw), timeout=180)
