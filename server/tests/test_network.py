import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import unquote

import httpx
import pytest

from test_library import client
from app import network, network_api
from app.database import SessionLocal
from app.integrations.base import ConnectorError, SafeHTTP
from app.models import NetworkSetting


def proxy_config(**values):
    return {"enabled": True, "proxy_url": "http://127.0.0.1:7890", **values}


def test_proxy_encryption_redaction_keep_clear_and_disable(client):
    result = client.put("/api/network/proxy", json=proxy_config(proxy_username="user:name",
        proxy_password="private/@password", controller_url="http://localhost:9090", controller_secret="controller-key"))
    assert result.status_code == 200
    assert result.json()["has_proxy_password"] and result.json()["has_controller_secret"]
    assert "private/" not in result.text and "controller-key" not in result.text
    with SessionLocal() as db:
        row = db.get(NetworkSetting, network.SETTING_KEY)
        assert "private/" not in row.value_encrypted and "controller-key" not in row.value_encrypted
    assert "user%3Aname:private%2F%40password@" in network.httpx_proxy()
    client.put("/api/network/proxy", json=proxy_config(proxy_username="user:name"))
    assert client.get("/api/network/proxy").json()["has_proxy_password"]
    result = client.put("/api/network/proxy", json=proxy_config(enabled=False, proxy_username="", proxy_password="", controller_secret=""))
    assert not result.json()["has_proxy_password"] and not result.json()["has_controller_secret"]
    assert network.httpx_proxy() is None
    assert network.requests_proxies() == {"http": "", "https": "", "all": ""}


@pytest.mark.parametrize("url", ["file:///x", "http://u:p@localhost:7890", "http://localhost:7890/x",
    "http://localhost:7890?secret=x", "http://localhost:99999", "http://localhost", "http://localhost:7890\\@evil"])
def test_reject_malformed_proxy_without_persisting(client, url):
    assert client.put("/api/network/proxy", json=proxy_config(proxy_url=url)).status_code == 400
    assert not client.get("/api/network/proxy").json()["enabled"]


def test_socks_remote_dns_and_platform_allowlist_preserved(client, monkeypatch):
    client.put("/api/network/proxy", json=proxy_config(proxy_url="socks5://localhost:7891"))
    assert network.requests_proxies()["https"] == "socks5h://localhost:7891"
    monkeypatch.setattr("app.integrations.base.socket.getaddrinfo", lambda *a, **k: pytest.fail("DNS must be delegated to configured proxy"))
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://localhost/private"})
    transport = SafeHTTP(("pixiv.net",))
    transport.client.close()
    transport.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ConnectorError):
            transport.fetch("https://www.pixiv.net/")
        assert requested == ["https://www.pixiv.net/"]
    finally:
        transport.close()


def test_clash_real_http_controller_flow(client):
    seen = []
    group_name = "我的代理 / 漫画"
    node_name = "日本节点 01"
    class Controller(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            body = json.dumps({"proxies": {
                group_name: {"type": "Selector", "now": "DIRECT", "all": [node_name, "DIRECT"]},
                "自动测速": {"type": "URLTest", "all": [node_name]},
                node_name: {"type": "Shadowsocks", "alive": True, "history": [{"delay": 125}]},
                "DIRECT": {"type": "Direct"}}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_PUT(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.command, unquote(self.path), body, self.headers.get("Authorization")))
            self.send_response(204)
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Controller)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = client.put("/api/network/proxy", json=proxy_config(
            controller_url=f"http://127.0.0.1:{server.server_port}", controller_secret="test-controller-secret"))
        assert response.status_code == 200
        groups = client.get("/api/network/clash/groups").json()["groups"]
        assert len(groups) == 1 and groups[0]["nodes"][0]["delay"] == 125
        assert "test-controller-secret" not in json.dumps(groups)
        rejected = client.post("/api/network/clash/select", json={"group": group_name, "node": "absent"})
        assert rejected.status_code == 400 and not any(v[0] == "PUT" for v in seen)
        selected = client.post("/api/network/clash/select", json={"group": group_name, "node": node_name})
        assert selected.status_code == 200
        assert seen[-1] == ("PUT", "/proxies/" + group_name, {"name": node_name}, "Bearer test-controller-secret")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_platform_probe_uses_saved_proxy_and_no_secrets(client, monkeypatch):
    client.put("/api/network/proxy", json=proxy_config(proxy_username="proxy-user", proxy_password="proxy-secret"))
    original = httpx.Client
    options, requests = [], []
    def handler(request):
        requests.append(request)
        return httpx.Response(403)
    def transport(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(network_api.httpx, "Client", transport)
    result = client.post("/api/network/proxy/test", json={"platform": "pixiv"})
    assert result.status_code == 200
    assert result.json()["reachable"] and not result.json()["ok"]
    assert options[0]["proxy"] == "http://proxy-user:proxy-secret@127.0.0.1:7890"
    assert "proxy-secret" not in result.text
    assert "authorization" not in requests[0].headers and "cookie" not in requests[0].headers
    assert client.post("/api/network/proxy/test", json={"platform": "http://localhost/"}).status_code == 422


def test_clash_errors_are_safe_and_do_not_end_library_session(client, monkeypatch):
    client.put("/api/network/proxy", json=proxy_config(controller_url="http://localhost:9090", controller_secret="sensitive-secret"))
    original = httpx.Client
    monkeypatch.setattr(network_api.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text="sensitive-secret should not escape"))))
    result = client.get("/api/network/clash/groups")
    assert result.status_code == 400 and "sensitive-secret" not in result.text
    assert client.get("/api/auth/status").json()["authenticated"]
    client.post("/api/auth/logout")
    assert client.get("/api/network/proxy").status_code == 401
    assert client.put("/api/network/proxy", json=proxy_config()).status_code == 401
