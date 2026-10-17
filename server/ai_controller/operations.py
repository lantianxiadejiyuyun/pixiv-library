"""Bounded persistent model-pull operations, separate from book processing."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import RLock
import time
import uuid

import httpx
from fastapi import HTTPException


class Operations:
    def __init__(self):
        self.lock = RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ollama-pull")

    @property
    def root(self):
        path = Path(os.getenv("QIYE_STATE_DIR", "/state")) / "operations"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def read(self, ident):
        try:
            if str(uuid.UUID(ident)) != ident:
                raise ValueError()
            return json.loads((self.root / (ident + ".json")).read_text())
        except (ValueError, OSError):
            raise HTTPException(404, "模型任务不存在") from None

    def write(self, value):
        with self.lock:
            value["updated_at"] = time.time()
            path = self.root / (value["id"] + ".json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value), encoding="utf-8")
            temporary.replace(path)

    def recover(self):
        for path in self.root.glob("*.json"):
            row = self.read(path.stem)
            if row["status"] in ("running", "queued"):
                row.update(status="failed", error="控制器已重启，请重新提交；Ollama 将复用已下载的数据")
                self.write(row)

    def enqueue(self, model, base_url):
        with self.lock:
            paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
            active = []
            for path in paths:
                row = self.read(path.stem)
                if row["status"] in ("queued", "running"):
                    if row["model"] == model:
                        return row
                    active.append(row)
            if len(active) >= 8:
                raise HTTPException(429, "模型下载队列已满")
            for path in paths[:-120]:
                if self.read(path.stem)["status"] not in ("queued", "running"):
                    path.unlink()
            row = {"id": str(uuid.uuid4()), "model": model, "status": "queued", "completed": 0,
                   "total": 0, "phase": "等待下载", "error": None}
            self.write(row)
            self.executor.submit(self.pull, dict(row), base_url)
            return row

    def pull(self, row, base_url):
        row.update(status="running", phase="连接 Ollama")
        self.write(row)
        try:
            deadline, last_write, pending = time.monotonic() + 1800, 0., bytearray()
            success = False
            with httpx.Client(timeout=httpx.Timeout(90, connect=5), trust_env=False, follow_redirects=False) as client:
                with client.stream("POST", base_url + "/api/pull", json={"model": row["model"], "stream": True}) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        if time.monotonic() > deadline:
                            raise ValueError()
                        pending.extend(chunk)
                        if len(pending) > 65536:
                            raise ValueError()
                        while b"\n" in pending:
                            raw, _, remaining = pending.partition(b"\n")
                            pending = bytearray(remaining)
                            if not raw.strip():
                                continue
                            data = json.loads(raw)
                            if data.get("error"):
                                raise ValueError()
                            phase = str(data.get("status", "下载中"))[:200]
                            row["phase"] = phase
                            for key in ("completed", "total"):
                                if type(data.get(key)) is int and data[key] >= 0:
                                    row[key] = data[key]
                            success = phase == "success"
                            if time.monotonic() - last_write >= .5:
                                self.write(row); last_write = time.monotonic()
                    if pending.strip():
                        data = json.loads(pending)
                        success = data.get("status") == "success" and not data.get("error")
            if not success:
                raise ValueError()
            row.update(status="completed", phase="下载完成")
        except Exception:
            row.update(status="failed", error="Ollama 模型下载失败或超时，请检查模型名称、网络和磁盘空间后重试")
        self.write(row)
