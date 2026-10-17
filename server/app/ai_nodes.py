"""Encrypted remote AI node registry and bounded companion-controller calls."""
import hashlib
import io
import ipaddress
import json
import re
import time
from typing import Literal
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select

from .auth import require_admin_cookie
from .database import get_db
from .device_auth import normalize_server_url
from .integrations.base import ConnectorError
from .integrations.credentials import decrypt_credentials, encrypt_credentials
from .models import NetworkSetting
from .queue_control import lock_control

KEY = "ai_controller_nodes"
router = APIRouter(prefix="/ai/nodes", dependencies=[Depends(require_admin_cookie)])
MAX_RESPONSE = 256 * 1024


def read_config(db):
    row = db.get(NetworkSetting, KEY)
    try:
        return {"nodes": [], "image_node_id": None, **(decrypt_credentials(row.value_encrypted) if row else {})}
    except ConnectorError:
        raise ConnectorError("AI 节点配置无法解密，请检查书库加密密钥") from None


def save_config(db, config):
    row = db.get(NetworkSetting, KEY)
    if row is None:
        row = NetworkSetting(key=KEY)
        db.add(row)
    row.value_encrypted = encrypt_credentials(config)
    db.commit()


def node_by_id(db, ident, *, enabled=True):
    node = next((node for node in read_config(db)["nodes"] if node["id"] == ident), None)
    if node is None or enabled and not node["enabled"]:
        raise ConnectorError("AI 节点不存在或已停用，请重新选择；未切换到其他节点")
    return dict(node)


def image_target(db):
    config = read_config(db)
    return node_by_id(db, config["image_node_id"]) if config["image_node_id"] else None


def signature(node):
    return hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()


def public_node(node):
    return {key: node[key] for key in ("id", "label", "base_url", "enabled")} | {"has_key": bool(node["key"])}


class NodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    key: SecretStr | None = Field(default=None, min_length=32, max_length=256)

    @field_validator("base_url")
    @classmethod
    def address(cls, value):
        value = normalize_server_url(value)
        from urllib.parse import urlsplit
        try:
            if ipaddress.ip_address(urlsplit(value).hostname).is_link_local:
                raise ValueError("不支持链路本地地址")
        except ValueError as exc:
            if str(exc) == "不支持链路本地地址":
                raise
        return value.rstrip("/")

    @field_validator("key")
    @classmethod
    def key_value(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value.get_secret_value()):
            raise ValueError("控制器密钥须为 32–256 位字母、数字、下划线或短横线")
        return value

    @field_validator("label")
    @classmethod
    def name(cls, value):
        if not value.strip():
            raise ValueError("节点名称不能为空")
        return value.strip()


class RoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_node_id: str | None = Field(default=None, max_length=36)


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,190}$", max_length=191)


def request_node(node, method, path, *, body=None, content=None, timeout=20):
    try:
        headers = {"Authorization": "Bearer " + node["key"], "Accept": "application/json"}
        if content is not None:
            headers["Content-Type"] = "application/octet-stream"
        deadline, raw = time.monotonic() + timeout, bytearray()
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=5), trust_env=False, follow_redirects=False) as client:
            payload = {"content": content} if content is not None else {"json": body}
            with client.stream(method, node["base_url"] + path, headers=headers, **payload) as response:
                if response.status_code not in (200, 201, 202):
                    raise ConnectorError(f"AI 控制器请求失败（HTTP {response.status_code}），请检查节点密钥、容器和模型")
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE or time.monotonic() > deadline:
                        raise ConnectorError("AI 控制器响应超过大小或时间限制")
                document = json.loads(raw)
                if not isinstance(document, dict):
                    raise ValueError()
                return document
    except ConnectorError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise ConnectorError("无法连接 AI 控制器，或控制器返回无效数据，请检查地址与网络") from None


def relay(db, ident, method, path, *, body=None, timeout=20):
    try:
        node = node_by_id(db, ident)
        db.commit()
        return request_node(node, method, path, body=body, timeout=timeout)
    except ConnectorError as exc:
        raise HTTPException(502, str(exc)) from None


@router.get("")
def list_nodes(db=Depends(get_db)):
    config = read_config(db)
    return {"items": [public_node(node) for node in config["nodes"]], "image_node_id": config["image_node_id"]}


@router.get("/deployment")
def deployment():
    from pathlib import Path
    import zipfile
    from fastapi.responses import Response
    root = Path(__file__).resolve().parents[2]
    patterns = ("server/ai_controller/*.py", "server/app/*.py", "server/app/ai/*.py", "server/app/integrations/*.py",
                "server/requirements-controller.txt", "server/requirements-ai*.txt", "deploy/compose.ai-node*.yaml",
                "deploy/Dockerfile.ai-controller*", "deploy/ai-node.env.example", "scripts/ai_node.py",
                "scripts/bootstrap_ai_node.py", "scripts/install_intel_opencl.py", "docs/AI_NODES.md")
    paths = sorted({path for pattern in patterns for path in root.glob(pattern) if path.is_file()})
    if not (root / "deploy/compose.ai-node.yaml").is_file():
        raise HTTPException(503, "此服务器镜像缺少 AI 节点部署文件，请更新镜像")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(root).as_posix())
        if (root / "docs/AI_NODES.md").is_file():
            archive.write(root / "docs/AI_NODES.md", "README.md")
        archive.writestr(".gitignore", "deploy/ai-node.env\nmodels/\n**/__pycache__/\n")
    return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="qiye-ai-node-0.1.0.zip"', "Cache-Control": "no-store"})


@router.post("")
def add_node(value: NodeInput, db=Depends(get_db)):
    if value.key is None:
        raise HTTPException(400, "添加节点需要控制器密钥")
    lock_control(db)
    config = read_config(db)
    if len(config["nodes"]) >= 100:
        raise HTTPException(409, "最多保存 100 个 AI 节点")
    node = {**value.model_dump(exclude={"key"}), "id": str(uuid.uuid4()), "key": value.key.get_secret_value()}
    config["nodes"].append(node)
    save_config(db, config)
    return public_node(node)


@router.put("/routing")
def set_routing(value: RoutingInput, db=Depends(get_db)):
    lock_control(db)
    config = read_config(db)
    if value.image_node_id:
        try:
            node_by_id(db, value.image_node_id)
        except ConnectorError as exc:
            raise HTTPException(400, str(exc)) from None
    config["image_node_id"] = value.image_node_id
    save_config(db, config)
    return list_nodes(db)


@router.put("/{node_id}")
def update_node(node_id: str, value: NodeInput, db=Depends(get_db)):
    lock_control(db)
    config = read_config(db)
    current = next((node for node in config["nodes"] if node["id"] == node_id), None)
    if current is None:
        raise HTTPException(404, "AI 节点不存在")
    if current["base_url"] != value.base_url and value.key is None:
        raise HTTPException(400, "更换控制器地址时请重新输入密钥")
    current.update(value.model_dump(exclude={"key"}))
    if value.key is not None:
        current["key"] = value.key.get_secret_value()
    save_config(db, config)
    return public_node(current)


@router.delete("/{node_id}")
def delete_node(node_id: str, db=Depends(get_db)):
    lock_control(db)
    config = read_config(db)
    if config["image_node_id"] == node_id:
        raise HTTPException(409, "请先将图片识别切回本机或其他节点")
    from .translation import read_config as translation_config
    if translation_config(db).get("controller_node_id") == node_id:
        raise HTTPException(409, "请先将汉化模型切换到其他节点或直接接口")
    if not any(node["id"] == node_id for node in config["nodes"]):
        raise HTTPException(404, "AI 节点不存在")
    config["nodes"] = [node for node in config["nodes"] if node["id"] != node_id]
    save_config(db, config)
    return {"ok": True}


@router.post("/{node_id}/test")
def test_node(node_id: str, db=Depends(get_db)):
    data = relay(db, node_id, "GET", "/v1/status")
    if data.get("protocol") != "qiye-ai-controller-v1":
        raise HTTPException(502, "地址不是兼容的栖页 AI 控制器")
    return data


@router.get("/{node_id}/vision")
def vision_status(node_id: str, db=Depends(get_db)):
    return relay(db, node_id, "GET", "/v1/vision/status")


@router.post("/{node_id}/services/{service}/{action}")
def service_action(node_id: str, service: Literal["ollama", "vision"], action: Literal["start", "stop", "restart"], db=Depends(get_db)):
    return relay(db, node_id, "POST", f"/v1/services/{service}/{action}", timeout=35)


@router.get("/{node_id}/models")
def models(node_id: str, db=Depends(get_db)):
    return relay(db, node_id, "GET", "/v1/ollama/models")


@router.post("/{node_id}/models/pull")
def pull_model(node_id: str, value: ModelInput, db=Depends(get_db)):
    return relay(db, node_id, "POST", "/v1/ollama/models/pull", body=value.model_dump())


@router.post("/{node_id}/models/unload")
def unload_model(node_id: str, value: ModelInput, db=Depends(get_db)):
    return relay(db, node_id, "POST", "/v1/ollama/models/unload", body=value.model_dump(), timeout=100)


@router.delete("/{node_id}/models")
def delete_model(node_id: str, value: ModelInput, db=Depends(get_db)):
    return relay(db, node_id, "DELETE", "/v1/ollama/models", body=value.model_dump())


@router.get("/{node_id}/operations/{operation_id}")
def model_operation(node_id: str, operation_id: uuid.UUID, db=Depends(get_db)):
    return relay(db, node_id, "GET", f"/v1/operations/{operation_id}")


@router.get("/{node_id}/operations")
def model_operations(node_id: str, db=Depends(get_db)):
    return relay(db, node_id, "GET", "/v1/operations")


@router.post("/{node_id}/translation")
def use_translation(node_id: str, value: ModelInput, db=Depends(get_db)):
    from .translation import SettingsInput, save_settings
    try:
        node = node_by_id(db, node_id)
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from None
    return save_settings(SettingsInput(enabled=True, provider="controller", base_url=node["base_url"],
        model=value.model, controller_node_id=node_id, api_key="", use_proxy=False), db)


class PredictionTag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=191)
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    category: Literal[0, 4]


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_version: str = Field(min_length=1, max_length=100)
    rating: Literal["general", "sensitive", "adult", "unknown"]
    rating_scores: dict[str, float] = Field(min_length=1, max_length=4)
    tags: list[PredictionTag] = Field(max_length=100)
    execution: dict = Field(default_factory=dict)


def infer_remote(node, path):
    from PIL import Image, ImageOps
    if path.stat().st_size > 16 * 1024 * 1024:
        with Image.open(path) as original:
            image = ImageOps.exif_transpose(original)
            image.thumbnail((2048, 2048))
            rgba = image.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            stream = io.BytesIO(); white.convert("RGB").save(stream, "JPEG", quality=95)
            content = stream.getvalue()
    else:
        content = path.read_bytes()
    if len(content) > 16 * 1024 * 1024:
        raise ConnectorError("图片过大，不能发送到 AI 节点")
    document = request_node(node, "POST", "/v1/vision/analyze", content=content, timeout=190)
    try:
        prediction = Prediction.model_validate(document).model_dump()
        ratings = prediction["rating_scores"]
        if set(ratings) - {"general", "sensitive", "questionable", "explicit"} or any(not 0 <= value <= 1 for value in ratings.values()):
            raise ValueError()
        strongest = max(ratings, key=ratings.get)
        expected = {"general": "general", "sensitive": "sensitive", "questionable": "adult", "explicit": "adult"}[strongest] if ratings[strongest] >= .6 else "unknown"
        if prediction["rating"] != expected:
            raise ValueError()
        execution = prediction["execution"]
        allowed = {"requested_provider", "actual_provider", "device", "fallback_reason", "execution_state"}
        if set(execution) - allowed or any(value is not None and (not isinstance(value, str) or len(value) > 512) for value in execution.values()):
            raise ValueError()
        return prediction
    except (ValueError, TypeError, KeyError):
        raise ConnectorError("远程图像识别结果格式或分级不完整，未保存 AI 结果") from None


def image_status(db):
    try:
        node = image_target(db)
    except ConnectorError as exc:
        return {"available": False, "models": [{"id": "remote-wd-tagger", "label": "远程图片识别", "available": False, "message": str(exc)}],
                "provider": "remote", "message": str(exc), "execution_state": "unavailable"}
    if node is None:
        return None
    return {"available": True, "models": [{"id": "remote-wd-tagger", "label": "远程标签／角色／R18 分级", "available": True,
            "message": "已选择节点；请测试连接并确认图像模型就绪"}], "provider": "remote", "requested_provider": "remote",
            "actual_provider": None, "device": node["label"], "available_providers": [], "execution_state": "configured",
            "message": "图片发送到所选 AI 节点识别；连接失败不会自动切换节点或本机"}
