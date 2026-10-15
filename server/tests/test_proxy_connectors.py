"""Exercise the installed SDK request boundaries, rather than only constructor mocks."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs

import pytest
import requests

from test_library import client
from app import network
from app.integrations.base import ConnectorError
from app.integrations.pixiv import PixivConnector
from app.integrations.jm import JMConnector


def test_real_pixiv_sdk_uses_saved_proxy_for_auth_and_api_even_with_no_proxy(monkeypatch):
    configured = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890", "all": ""}
    monkeypatch.setattr(network, "requests_proxies", lambda: configured.copy())
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("HTTPS_PROXY", "http://unwanted-environment-proxy:9999")
    calls = []
    def send(adapter, request, **kwargs):
        calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        if request.url.endswith("/auth/token"):
            assert parse_qs(request.body)["refresh_token"] == ["synthetic-refresh"]
            response._content = json.dumps({"response": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "user": {"id": 101}}}).encode()
        else:
            response._content = b'{"illust":{"id":123,"title":"example"}}'
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    connector = PixivConnector({"refresh_token": "synthetic-refresh"})
    api = connector._api()
    try:
        assert api.illust_detail("123")["illust"]["id"] == 123
        assert api.requests.trust_env is False
    finally:
        api.requests.close()
    assert len(calls) == 2
    assert all(options["proxies"] == configured for request, options in calls)
    assert all(options["verify"] is True for request, options in calls)


def test_pixiv_proxy_failure_does_not_retry_directly_or_expose_secrets(monkeypatch):
    configured = {"http": "http://proxy:7890", "https": "http://proxy:7890", "all": ""}
    monkeypatch.setattr(network, "requests_proxies", lambda: configured.copy())
    calls = []
    def fail(adapter, request, **kwargs):
        calls.append(kwargs["proxies"])
        raise requests.exceptions.ProxyError("proxy-password=private-value")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fail)
    with pytest.raises(ConnectorError) as error:
        PixivConnector({"refresh_token": "private-token"})._api()
    assert calls == [configured]
    assert "private" not in str(error.value)


def test_disabled_proxy_does_not_inherit_environment_in_pixiv_sdk(monkeypatch):
    disabled = {"http": "", "https": "", "all": ""}
    monkeypatch.setattr(network, "requests_proxies", lambda: disabled.copy())
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-be-used:9999")
    captured = []
    def send(adapter, request, **kwargs):
        captured.append(kwargs["proxies"])
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response._content = b'{"response":{"access_token":"test","refresh_token":"test","user":{"id":123}}}'
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    api = PixivConnector({"refresh_token": "test"})._api()
    api.requests.close()
    assert captured == [disabled]


def test_jm_sdk_curl_request_ignores_no_proxy_and_uses_configured_proxy(client, monkeypatch):
    from jmcomic import JmHtmlClient
    from curl_cffi import CurlOpt
    seen = []
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"proxy")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    configured = {"http": f"http://127.0.0.1:{server.server_port}", "https": f"http://127.0.0.1:{server.server_port}", "all": ""}
    monkeypatch.setattr(network, "requests_proxies", lambda: configured.copy())
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("HTTP_PROXY", "http://unwanted-proxy:9999")
    def get_album(sdk, ident):
        assert sdk.postman.meta_data["proxies"] == configured
        assert sdk.postman.meta_data["curl_options"][CurlOpt.NOPROXY] == ""
        # This hostname does not resolve; success therefore requires the local
        # proxy, and would fail if libcurl honored the ambient NO_PROXY=*.
        response = sdk.postman.get("http://unresolvable-proxy-test.invalid/test")
        assert response.text == "proxy"
        from types import SimpleNamespace
        return SimpleNamespace(name="proxy test", authors=[], tags=[], description="")
    monkeypatch.setattr(JmHtmlClient, "get_album_detail", get_album)
    # The local proxy fixture tests metadata transport, not a remote CDN cover.
    monkeypatch.setattr("app.integrations.jm.fetch_preview_cover", lambda *args, **kwargs: None)
    try:
        work = JMConnector({}).fetch("123", download=False)
        assert work.title == "proxy test"
        assert seen == ["http://unresolvable-proxy-test.invalid/test"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
