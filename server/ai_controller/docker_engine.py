"""Only control containers carrying this node's service labels."""
import json
import os
import re

import httpx
from fastapi import HTTPException

from .common import json_request

SERVICES = ("ollama", "vision")
NODE_LABEL = "io.qiye.ai.node"
SERVICE_LABEL = "io.qiye.ai.service"


class DockerEngine:
    def __init__(self, *, transport=None):
        self.transport = transport

    def request(self, method, path, body=None, timeout=15):
        # No arbitrary Docker HTTP path is accepted from an API caller.
        transport = self.transport or httpx.HTTPTransport(uds=os.getenv("QIYE_DOCKER_SOCKET", "/var/run/docker.sock"))
        return json_request(method, "http://docker" + path, body=body, timeout=timeout, transport=transport)

    def version(self):
        version = self.request("GET", "/version").get("ApiVersion", "")
        if not re.fullmatch(r"1\.\d{2,3}", version):
            raise HTTPException(502, "Docker 未返回有效 API 版本")
        return "/v" + version

    def containers(self, service):
        if service not in SERVICES:
            raise HTTPException(404, "AI 服务不存在")
        node = os.getenv("QIYE_NODE_ID", "qiye-ai-node")
        filters = json.dumps({"label": [f"{NODE_LABEL}={node}", f"{SERVICE_LABEL}={service}"]})
        from urllib.parse import urlencode
        rows = self.request("GET", self.version() + "/containers/json?" + urlencode({"all": "true", "filters": filters}))
        if not isinstance(rows, list) or len(rows) > 1:
            raise HTTPException(409, "AI 服务容器标签重复，请检查节点部署")
        for row in rows:
            labels = row.get("Labels") or {}
            if (labels.get(NODE_LABEL) != node or labels.get(SERVICE_LABEL) != service
                    or not re.fullmatch(r"[a-f0-9]{64}", row.get("Id", ""))):
                raise HTTPException(502, "Docker 返回了不属于该节点的容器")
        return rows

    def status(self, service):
        rows = self.containers(service)
        if not rows:
            return {"service": service, "state": "not_created", "status": "请先在 AI 电脑执行节点 Compose"}
        row = rows[0]
        return {"service": service, "container_id": row["Id"][:12], "state": str(row.get("State", "unknown"))[:40],
                "status": str(row.get("Status", ""))[:256], "image": str(row.get("Image", ""))[:256]}

    def action(self, service, action):
        if action not in ("start", "stop", "restart"):
            raise HTTPException(422, "不支持的容器操作")
        rows = self.containers(service)
        if not rows:
            raise HTTPException(409, "容器尚未创建，请先在 AI 电脑执行节点 Compose")
        suffix = "?t=15" if action in ("stop", "restart") else ""
        self.request("POST", self.version() + f"/containers/{rows[0]['Id']}/{action}" + suffix, timeout=25)
        return self.status(service)
