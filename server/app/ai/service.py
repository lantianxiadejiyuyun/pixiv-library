from __future__ import annotations
import csv
import hashlib
import os
from pathlib import Path
from threading import Lock

from sqlalchemy import delete, select, update, or_
from app.config import settings
from app.models import Item, ItemTag, Tag
from app.storage import resolve_asset_path
from app.integrations.base import ConnectorError
from .providers import AIRuntimeError, CUDA, INTEL, MODES, InferenceSession, runtime_info


_cache = None
_load_error = None
_lock = Lock()
TEXT_RULES = {
    "分类:科技": ("编程", "算法", "数据库", "人工智能", "python", "software", "technology"),
    "分类:历史": ("历史", "朝代", "考古", "史料", "history", "historical"),
    "分类:文学": ("小说", "诗歌", "散文", "文学", "fiction", "poetry"),
    "分类:生活": ("食谱", "烹饪", "园艺", "旅行", "生活", "recipe", "cooking"),
    "分类:艺术": ("绘画", "摄影", "艺术", "插画", "painting", "photography"),
}


def model_status():
    directory = settings.model_dir / "wd-tagger"
    files = (directory / "model.onnx", directory / "selected_tags.csv")
    if not os.getenv("LIBRARY_WORKER_PROCESS"):
        from ..worker_runtime import supervisor
        model_key = [path.stat().st_mtime_ns if path.is_file() else None for path in files]
        snapshots = []
        for lane in ("ai", "retry"):
            snapshot = supervisor.worker_data(lane)
            if snapshot and snapshot.get("model_key") == model_key:
                cached = snapshot.get("ai_status")
                if cached and cached.get("requested_provider") == os.getenv("LIBRARY_AI_PROVIDER", "auto").lower():
                    snapshots.append(cached)
        # Keep the ordinary AI worker authoritative once it has run, but do not
        # report "not loaded" when only the retry process performed inference.
        if snapshots:
            return next((value for value in snapshots if value.get("execution_state") != "not_loaded"), snapshots[0])
    runtime = runtime_info()
    requested_provider = os.getenv("LIBRARY_AI_PROVIDER", "auto").lower()
    providers = runtime["available_providers"]
    provider_valid = requested_provider in MODES and (requested_provider != "cuda" or CUDA in providers)
    available = runtime["runtime_available"] and provider_valid and all(p.is_file() for p in files)
    message = "本地文件与推理依赖就绪；首次任务会验证模型兼容性" if available else (
        runtime["runtime_error"] if not runtime["runtime_available"] else
        "请求的推理设备不可用；可设置 LIBRARY_AI_PROVIDER=auto、cpu、intel，或安装兼容的 CUDA 运行时" if not provider_valid else
        "请将同一 WD Tagger 版本的 model.onnx 和 selected_tags.csv 放入模型目录的 wd-tagger 子目录")
    execution = {"requested_provider": requested_provider, "actual_provider": None, "device": None,
                 "fallback_reason": None, "execution_state": "not_loaded"}
    if requested_provider == "intel" and runtime["runtime_available"] and INTEL not in providers:
        execution["fallback_reason"] = "当前运行时不含 OpenVINO Intel GPU 支持；首次任务将使用 CPU"
    key = _model_key(directory, requested_provider) if all(p.is_file() for p in files) else None
    if _cache and _cache[0] == key:
        execution.update(_cache[1]["session"].status())
    if _load_error and _load_error[0] == key:
        execution["execution_state"] = "error"
        message = _load_error[1]
    return {"available": available, "models": [
        {"id": "wd-tagger", "label": "图片标签 / 二次元角色 / R18 分级", "available": available, "message": message},
        {"id": "text-rules", "label": "文章关键词规则", "available": True, "message": "本地可解释规则；并非 AI 模型"},
        {"id": "ocr", "label": "扫描正文 OCR", "available": False, "message": "尚未接入 OCR"}],
        "provider": requested_provider, "available_providers": providers, "runtime_version": runtime["runtime_version"],
        **execution,
        "message": "本机图片识别使用 ONNX；不会自动下载权重。可在远程 AI 控制器中切换执行节点。"}


def _model_key(directory, provider):
    return (str(directory), (directory / "model.onnx").stat().st_mtime_ns,
            (directory / "selected_tags.csv").stat().st_mtime_ns, provider)


def _load_model():
    global _cache, _load_error
    if not model_status()["available"]:
        raise ConnectorError(model_status()["models"][0]["message"])
    directory = settings.model_dir / "wd-tagger"
    model, tags_path = directory / "model.onnx", directory / "selected_tags.csv"
    provider = os.getenv("LIBRARY_AI_PROVIDER", "auto").lower()
    key = _model_key(directory, provider)
    with _lock:
        if _cache and _cache[0] == key and _cache[1]["session"].status()["execution_state"] != "error":
            return _cache[1]
        if _cache:
            _cache[1]["session"].close()
            _cache = None
        try:
            session = InferenceSession(model, provider)
        except AIRuntimeError as exc:
            _load_error = (key, str(exc))
            raise ConnectorError(str(exc)) from None
        try:
            shape = session.get_inputs()[0].shape
            if len(shape) != 4 or shape[-1] != 3 or not isinstance(shape[1], int) or shape[1] != shape[2]:
                raise ConnectorError("模型输入不兼容，需要 WD Tagger NHWC 正方形三通道 ONNX 模型")
            with tags_path.open(encoding="utf-8") as handle:
                tags = [(row["name"], int(row["category"])) for row in csv.DictReader(handle)]
            sha = hashlib.sha256()
            for path in (model, tags_path):
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        sha.update(chunk)
        except Exception as exc:
            session.close()
            message = str(exc) if isinstance(exc, ConnectorError) else "模型输入或标签文件不兼容，请检查 model.onnx 与 selected_tags.csv"
            _load_error = (key, message)
            raise ConnectorError(message) from None
        loaded = {"session": session, "size": shape[1], "tags": tags, "version": "wd-tagger:" + sha.hexdigest()[:24]}
        _cache = (key, loaded)
        _load_error = None
        return loaded


def infer_image(path: Path):
    import numpy as np
    from PIL import Image, ImageOps
    loaded = _load_model()
    with Image.open(path) as original:
        original = ImageOps.exif_transpose(original).convert("RGBA")
        white = Image.new("RGBA", original.size, "white")
        white.alpha_composite(original)
        rgb = white.convert("RGB")
        edge = max(rgb.size)
        padded = Image.new("RGB", (edge, edge), "white")
        padded.paste(rgb, ((edge - rgb.width) // 2, (edge - rgb.height) // 2))
        scaled = padded.resize((loaded["size"], loaded["size"]), Image.Resampling.BICUBIC)
        tensor = np.asarray(scaled, dtype=np.float32)[:, :, ::-1].copy()[None, ...]
    session = loaded["session"]
    try:
        scores = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
    except AIRuntimeError as exc:
        raise ConnectorError(str(exc)) from None
    if len(scores) != len(loaded["tags"]) or not np.isfinite(scores).all():
        raise ConnectorError("模型输出与 selected_tags.csv 不匹配或包含无效分数")
    ratings, predictions = {}, []
    for (name, category), raw_score in zip(loaded["tags"], scores):
        score = float(raw_score)
        if category == 9:
            ratings[name] = score
        elif score >= (0.85 if category == 4 else 0.35):
            predictions.append({"name": ("角色:" if category == 4 else "") + name, "score": score, "category": category})
    if not ratings or not set(ratings).intersection({"general", "sensitive", "questionable", "explicit"}):
        raise ConnectorError("标签文件不包含 WD 分级类别，不能完成 R18 检测")
    strongest = max(ratings, key=ratings.get)
    rating = {"general": "general", "sensitive": "sensitive", "questionable": "adult", "explicit": "adult"}.get(strongest)
    if rating is None:
        raise ConnectorError("模型包含未知分级，不能完成 R18 检测")
    if ratings[strongest] < 0.6:
        rating = "unknown"
    return {"model_version": loaded["version"], "rating": rating, "rating_scores": ratings,
            "tags": sorted(predictions, key=lambda p: p["score"], reverse=True)[:100], "execution": session.status()}


def analyze_item(db, item_id, checkpoint=lambda value: None, before_commit=lambda db: None):
    item = db.get(Item, item_id)
    if item is None:
        raise ConnectorError("作品已被删除")
    assets = item.assets
    if not assets:
        raise ConnectorError("作品仅含元数据；请先保存原文件再分析")
    image_assets = [a for a in assets if a.mime_type.startswith("image/")]
    text_assets = [a for a in assets if a.mime_type.startswith("text/")]
    from app.ai_nodes import image_target, infer_remote, signature
    remote = image_target(db) if image_assets else None
    if image_assets and remote is None and not model_status()["available"]:
        raise ConnectorError(model_status()["models"][0]["message"])
    if not image_assets and not text_assets:
        raise ConnectorError("没有可分析的图片或文本文件")
    combined, evidence, text_evidence = {}, [], []
    if remote is not None:
        db.commit()  # No library transaction or row lock crosses remote inference.
    for index, asset in enumerate(image_assets):
        checkpoint(5 + int(80 * index / len(image_assets)))
        path = resolve_asset_path(asset.relative_path)
        prediction = infer_remote(remote, path) if remote is not None else infer_image(path)
        evidence.append({"asset_id": asset.id, "position": asset.position, "rating": prediction["rating"],
                         "rating_scores": prediction["rating_scores"], "model_version": prediction["model_version"],
                         "execution": prediction.get("execution")})
        for tag in prediction["tags"]:
            name = tag["name"][:191]
            if name not in combined or tag["score"] > combined[name]["score"]:
                combined[name] = {**tag, "name": name, "asset_id": asset.id, "model_version": prediction["model_version"]}
    for asset in text_assets:
        checkpoint(90)
        text = resolve_asset_path(asset.relative_path).read_text(encoding="utf-8", errors="replace").lower()
        for name, keywords in TEXT_RULES.items():
            matched = [word for word in keywords if word in text]
            if matched:
                combined[name] = {"name": name, "score": 1.0, "asset_id": asset.id, "model_version": "text-rules-v1"}
                text_evidence.append({"asset_id": asset.id, "tag": name, "matched_keywords": matched})
    checkpoint(95)
    # End the read snapshot before writing so concurrent manual changes are visible.
    db.rollback()
    if remote is not None:
        from app.queue_control import lock_control
        lock_control(db)
        if signature(image_target(db)) != signature(remote):
            raise ConnectorError("图片 AI 节点配置已更改，结果未保存，请重新提交")
    # Only mutate once every page succeeds. Keep manual/source annotations.
    db.execute(delete(ItemTag).where(ItemTag.item_id == item_id, ItemTag.origin == "ai"))
    from app.library import ensure_tag
    for name, prediction in sorted(combined.items()):
        tag = ensure_tag(db, name)
        db.add(ItemTag(item_id=item_id, tag_id=tag.id, origin="ai", score=prediction["score"], model_version=prediction["model_version"]))
    suggestion = None
    rating_applied = False
    if evidence:
        # Any uncertain page prevents a general result; a clear adult page takes priority.
        rank = {"general": 0, "sensitive": 1, "unknown": 2, "adult": 3}
        suggestion = max((p["rating"] for p in evidence), key=rank.get)
        retain_source_adult = or_(Item.rating_origin != "source", Item.rating != "adult") if suggestion != "adult" else True
        applied = db.execute(update(Item).where(Item.id == item_id, Item.rating_origin != "manual", retain_source_adult)
                             .values(rating=suggestion, rating_origin="ai"))
        rating_applied = bool(applied.rowcount)
    before_commit(db)
    db.commit()
    return {"item_id": item_id, "analyzed_pages": len(evidence), "analyzed_texts": len(text_assets),
            "complete": True, "suggested_rating": suggestion, "rating_applied": rating_applied,
            "tags": list(combined.values()), "page_evidence": evidence, "text_evidence": text_evidence,
            "message": "分析完成" if image_assets or text_evidence else "规则检查完成，未命中分类规则"}
