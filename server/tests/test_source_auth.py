import json
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from test_library import client
from app import source_auth, source_auth_api
from app.database import SessionLocal
from app.integrations.credentials import decrypt_credentials
from app.models import SourceAccount
from app.source_auth import SourceLoginError


@pytest.fixture(autouse=True)
def endpoint_settings(client):
    # Login now reads the same persisted endpoint configuration as downloads.
    pass


def transport(monkeypatch, handler):
    monkeypatch.setattr(source_auth, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))


@pytest.mark.parametrize('platform,url', [('picacg', 'https://mirror.example.org/pica'), ('jm', 'https://mirror.example.org')])
def test_login_uses_selected_api_without_forwarding_credentials_on_redirect(client, monkeypatch, platform, url):
    from app import endpoint_config
    monkeypatch.setattr(endpoint_config, 'public_dns', lambda host: None)
    client.put('/api/platform-endpoints/' + platform, json={'enabled': True, 'custom_url': url}).raise_for_status()
    seen = []
    def handler(request):
        seen.append(request)
        assert str(request.url).startswith(url + '/')
        if platform == 'jm':
            assert request.headers['origin'] == url
        return httpx.Response(302, headers={'location': 'https://unselected.example.org/collect'})
    transport(monkeypatch, handler)
    with pytest.raises(SourceLoginError, match='跳转'):
        source_auth.login_platform(platform, 'synthetic-user', 'synthetic-password')
    assert len(seen) == 1


def test_picacg_custom_login_profile_uses_same_endpoint_and_protocol_signature(client, monkeypatch):
    from app import endpoint_config
    monkeypatch.setattr(endpoint_config, 'public_dns', lambda host: None)
    client.put('/api/platform-endpoints/picacg', json={'enabled': True, 'custom_url': 'https://mirror.example.org/pica'}).raise_for_status()
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.host == 'mirror.example.org'
        assert request.headers['signature']
        if request.url.path == '/pica/auth/sign-in':
            return httpx.Response(200, json={'code': 200, 'data': {'token': 'issued-test'}})
        assert request.url.path == '/pica/users/profile'
        assert request.headers['authorization'] == 'issued-test'
        return httpx.Response(200, json={'code': 200, 'data': {'user': {'_id': 'test-user'}}})
    transport(monkeypatch, handler)
    assert source_auth.login_picacg('synthetic-user', 'synthetic-password') == {'token': 'issued-test'}
    assert len(seen) == 2


def test_picacg_login_validates_profile_and_only_persists_returned_token(client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/auth/sign-in":
            assert json.loads(request.content) == {"email": "user@example.invalid", "password": "never-save-this-password"}
            assert "signature" in request.headers
            return httpx.Response(200, json={"code": 200, "data": {"token": "issued-private-token"}})
        assert request.url.path == "/users/profile"
        assert request.headers["authorization"] == "issued-private-token"
        return httpx.Response(200, json={"code": 200, "data": {"user": {"_id": "user-id"}}})

    transport(monkeypatch, handler)
    response = client.post("/api/sources/picacg/login", json={"label": " 我的账号 ", "username": "user@example.invalid", "password": "never-save-this-password"})
    assert response.status_code == 200, response.text
    assert response.json()["auth_status"] == "verified"
    assert response.json()["label"] == "我的账号"
    assert len(calls) == 2
    with SessionLocal() as db:
        account = db.get(SourceAccount, response.json()["id"])
        assert decrypt_credentials(account.credentials_encrypted) == {"token": "issued-private-token"}
        assert "never-save-this-password" not in account.credentials_encrypted
        assert account.verified_at is not None
    listing = client.get("/api/sources")
    assert "issued-private-token" not in listing.text
    assert next(s for s in listing.json() if s["platform"] == "picacg")["accounts"][0]["auth_status"] == "verified"


def test_platform_unauthorized_does_not_log_out_local_user_or_store_account(client, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(401, json={"error": "secret upstream response"}))
    response = client.post("/api/sources/picacg/login", json={"label": "失败", "username": "u", "password": "private"})
    assert response.status_code == 400
    assert "secret upstream" not in response.text
    assert client.get("/api/auth/status").json()["authenticated"] is True
    with SessionLocal() as db:
        assert db.scalar(select(SourceAccount)) is None


def test_picacg_token_without_valid_profile_is_not_success(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"code": 200, "data": {"token": "token"}}))
    with pytest.raises(SourceLoginError, match="验证失败"):
        source_auth.login_picacg("u", "p")


def test_jm_requires_success_status_and_fresh_avs_cookie(monkeypatch):
    def handler(request):
        assert request.url.host == "18comic.vip"
        assert parse_qs(request.content.decode())["password"] == ["private"]
        return httpx.Response(200, json={"status": 1}, headers={"set-cookie": "AVS=issued-session; Path=/; Secure; HttpOnly"})
    transport(monkeypatch, handler)
    assert source_auth.login_jm("user", "private") == {"AVS": "issued-session"}
    transport(monkeypatch, lambda request: httpx.Response(200, json={"status": 0, "errors": ["private value"]}, headers={"set-cookie": "AVS=guest-session; Path=/"}))
    with pytest.raises(SourceLoginError, match="登录失败"):
        source_auth.login_jm("user", "private")
    transport(monkeypatch, lambda request: httpx.Response(200, json={"status": 1}))
    with pytest.raises(SourceLoginError, match="AVS"):
        source_auth.login_jm("user", "private")


def test_ehentai_verifies_login_marker_and_returned_cookies(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, text='<form><input type="hidden" name="auth_key" value="server-csrf"></form>')
        form = parse_qs(request.content.decode())
        assert form["UserName"] == ["user"]
        assert form["PassWord"] == ["private"]
        assert form["auth_key"] == ["server-csrf"]
        return httpx.Response(200, text="<p>You are now logged in as: user</p>", headers=[
            ("set-cookie", "ipb_member_id=123; Domain=.e-hentai.org; Path=/; Secure"),
            ("set-cookie", "ipb_pass_hash=" + "a" * 32 + "; Domain=.e-hentai.org; Path=/; Secure")])
    transport(monkeypatch, handler)
    assert source_auth.login_ehentai("user", "private") == {"ipb_member_id": "123", "ipb_pass_hash": "a" * 32}


def test_ehentai_challenge_or_cookie_without_success_is_not_saved(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, text='<div class="g-recaptcha"></div>'))
    with pytest.raises(SourceLoginError, match="验证码"):
        source_auth.login_ehentai("user", "private")
    def handler(request):
        return httpx.Response(200, text="Login failed", headers=[
            ("set-cookie", "ipb_member_id=123; Domain=.e-hentai.org; Path=/"),
            ("set-cookie", "ipb_pass_hash=" + "a" * 32 + "; Domain=.e-hentai.org; Path=/")])
    transport(monkeypatch, handler)
    with pytest.raises(SourceLoginError, match="未确认"):
        source_auth.login_ehentai("user", "private")


def test_login_never_replays_password_on_redirect(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, text="Login form")
        return httpx.Response(302, headers={"location": "https://evil.invalid/steal"})
    transport(monkeypatch, handler)
    with pytest.raises(ValueError):
        source_auth.login_ehentai("user", "private")
    assert len(requests) == 2
    assert all(request.url.host == "forums.e-hentai.org" for request in requests)


def test_proxy_is_applied_to_new_login_client(monkeypatch):
    from app import network
    called = {}
    monkeypatch.setattr(network, "httpx_proxy", lambda: "http://127.0.0.1:7890")
    monkeypatch.setattr(source_auth.httpx, "Client", lambda **kwargs: called.update(kwargs))
    source_auth._client()
    assert called["proxy"] == "http://127.0.0.1:7890"
    assert called["trust_env"] is False
    assert called["follow_redirects"] is False


def test_invalid_proxy_failure_does_not_expose_credentials(monkeypatch):
    from app import network
    def invalid_proxy():
        raise ValueError("private-proxy-password")
    monkeypatch.setattr(network, "httpx_proxy", invalid_proxy)
    with pytest.raises(SourceLoginError, match="代理地址") as failure:
        source_auth._client()
    assert "private" not in str(failure.value)


def test_blank_account_labels_rejected_and_advanced_credentials_unverified(client):
    assert client.post("/api/sources/jm/login", json={"label": "  ", "username": "user", "password": "private"}).status_code == 422
    assert client.post("/api/sources/jm/accounts", json={"label": "  ", "credentials": {"AVS": "secret"}}).status_code == 422
    response = client.post("/api/sources/jm/accounts", json={"label": "高级会话", "credentials": {"AVS": "secret"}})
    assert response.status_code == 200
    assert response.json()["auth_status"] == "unverified"
    with SessionLocal() as db:
        assert db.get(SourceAccount, response.json()["id"]).verified_at is None


def test_local_auth_required_for_platform_login(client):
    client.post("/api/auth/logout")
    assert client.post("/api/sources/jm/login", json={"label": "账号", "username": "user", "password": "private"}).status_code == 401


def test_validation_errors_never_echo_invalid_secrets(client):
    private = "private-password-value-" * 120
    response = client.post("/api/sources/jm/login", json={"label": "账号", "username": "user", "password": private})
    assert response.status_code == 422
    assert "private-password-value" not in response.text
    assert all("input" not in error for error in response.json()["detail"])
