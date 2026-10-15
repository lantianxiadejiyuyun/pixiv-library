from __future__ import annotations

import json
import time
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import require_auth
from .database import get_db
from .integrations.base import ConnectorError, validate_url
from .network import httpx_proxy, public_config, read_config, save_config, validate_endpoint

router = APIRouter(dependencies=[Depends(require_auth)])


class ProxyInput(BaseModel):
    enabled: bool = False
    proxy_url: str = Field(default="", max_length=2048)
    proxy_username: str = Field(default="", max_length=512)
    proxy_password: str | None = Field(default=None, max_length=2048)
    controller_url: str = Field(default="", max_length=2048)
    controller_secret: str | None = Field(default=None, max_length=2048)


class SelectionInput(BaseModel):
    group: str = Field(min_length=1, max_length=512)
    node: str = Field(min_length=1, max_length=512)


class TestInput(BaseModel):
    platform: Literal["pixiv", "ehentai", "picacg", "jm"]


@router.get("/network/proxy")
def get_proxy(db=Depends(get_db)):
    return public_config(read_config(db))


@router.put("/network/proxy")
def put_proxy(value: ProxyInput, db=Depends(get_db)):
    return public_config(save_config(db, value.model_dump()))


def controller_request(config, method, path, body=None):
    root = validate_endpoint(config["controller_url"], controller=True)
    if not root:
        raise HTTPException(400, "请先保存 Clash 控制器地址")
    headers = {"Authorization": "Bearer " + config["controller_secret"]} if config["controller_secret"] else {}
    try:
        # Never send controller secrets through the content proxy or redirects.
        with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
            with client.stream(method, root + path, headers=headers, json=body) as response:
                if response.status_code in (401, 403):
                    raise HTTPException(400, "Clash 控制器拒绝访问，请检查密钥")
                if not 200 <= response.status_code < 300:
                    raise HTTPException(502, f"Clash 控制器请求失败（HTTP {response.status_code}）")
                if response.status_code == 204:
                    return {}
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 4 * 1024 * 1024:
                        raise HTTPException(502, "Clash 节点列表超过大小限制")
                return json.loads(content) if content else {}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "无法连接 Clash 控制器或响应格式无效，请检查地址、端口和网络") from None


def load_groups(config):
    result = controller_request(config, "GET", "/proxies")
    proxies = result.get("proxies") if isinstance(result, dict) else None
    if not isinstance(proxies, dict):
        raise HTTPException(502, "Clash 控制器未返回有效节点列表")
    groups = []
    for name, proxy in proxies.items():
        if not isinstance(proxy, dict) or proxy.get("type") != "Selector" or not isinstance(proxy.get("all"), list):
            continue
        nodes = []
        for node_name in proxy["all"]:
            if not isinstance(node_name, str):
                continue
            node = proxies.get(node_name, {})
            if not isinstance(node, dict):
                node = {}
            history = node.get("history", [])
            latest = history[-1] if isinstance(history, list) and history and isinstance(history[-1], dict) else {}
            delay = latest.get("delay")
            nodes.append({"name": node_name, "type": str(node.get("type", "Unknown")),
                          "alive": node.get("alive") if isinstance(node.get("alive"), bool) else None,
                          "delay": delay if isinstance(delay, (int, float)) else None})
        groups.append({"name": name, "type": "Selector", "now": proxy.get("now", ""), "nodes": nodes})
    return groups


@router.get("/network/clash/groups")
def clash_groups(db=Depends(get_db)):
    return {"groups": load_groups(read_config(db))}


@router.post("/network/clash/select")
def select_node(value: SelectionInput, db=Depends(get_db)):
    config = read_config(db)
    groups = load_groups(config)
    group = next((g for g in groups if g["name"] == value.group), None)
    if not group or value.node not in [n["name"] for n in group["nodes"]]:
        raise HTTPException(400, "节点不在此可手动选择的策略组中，请刷新节点列表")
    if value.group in (".", ".."):
        raise HTTPException(400, "不支持此策略组名称")
    controller_request(config, "PUT", "/proxies/" + quote(value.group, safe=""), {"name": value.node})
    return {"ok": True, "group": value.group, "node": value.node}


# Public roots only: no account, authorization code, cookie or library data sent.
TEST_TARGETS = {"pixiv": "https://www.pixiv.net/", "ehentai": "https://e-hentai.org/",
                "picacg": "https://picaapi.picacomic.com/", "jm": "https://18comic.vip/"}


@router.post("/network/proxy/test")
def test_proxy(value: TestInput, db=Depends(get_db)):
    proxy = httpx_proxy(read_config(db))
    url = TEST_TARGETS[value.platform]
    start = time.monotonic()
    status = None
    reachable = False
    message = "连接失败，请检查代理地址、端口、Clash 的局域网访问设置和节点状态"
    try:
        from urllib.parse import urlsplit
        validate_url(url, (urlsplit(url).hostname,), resolve=not bool(proxy))
        with httpx.Client(proxy=proxy, timeout=12, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", url) as response:
                status = response.status_code
                reachable = status != 407
        message = (f"已收到平台响应（HTTP {status}）；此测试不验证账号权限"
                   if reachable else "代理认证失败（HTTP 407），请检查代理用户名和密码")
    except (httpx.HTTPError, ConnectorError):
        pass
    return {"ok": reachable and status is not None and status < 400, "reachable": reachable,
            "status_code": status, "elapsed_ms": round((time.monotonic() - start) * 1000),
            "message": message, "via_proxy": bool(proxy)}
