import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.integrations import pixiv_auth as oauth
from app.integrations.base import ConnectorError
from test_library import client


@pytest.fixture(autouse=True)
def isolate_sessions():
    with oauth._lock:
        oauth._sessions.clear()
        oauth._attempts.clear()
        for queue in oauth._global_attempts.values():
            queue.clear()
    yield
    with oauth._lock:
        oauth._sessions.clear()
        oauth._attempts.clear()
        for queue in oauth._global_attempts.values():
            queue.clear()


def mock_endpoint(monkeypatch, handler):
    monkeypatch.setattr(oauth, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))


def test_start_keeps_verifier_server_side_and_pkce_matches_exchange(monkeypatch):
    start = oauth.start_oauth("signed-library-cookie")
    assert set(start) == {"session_id", "authorization_url", "expires_in"}
    url = urlsplit(start["authorization_url"])
    assert url.scheme == "https" and url.netloc == "app-api.pixiv.net" and url.path == "/web/v1/login"
    params = parse_qs(url.query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["client"] == ["pixiv-android"]
    sent = []
    def exchange(request):
        assert str(request.url) == oauth.TOKEN_URL
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        form = parse_qs(request.content.decode())
        sent.append(form)
        assert form["redirect_uri"] == [oauth.CALLBACK_URI]
        assert form["code"] == ["valid-code"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).decode().rstrip("=")
        assert challenge == params["code_challenge"][0]
        assert form["code_verifier"][0] not in json.dumps(start)
        return httpx.Response(200, json={"access_token": "discard-access-token", "refresh_token": "saved-refresh-token", "user": {"id": 123}})
    mock_endpoint(monkeypatch, exchange)
    result = oauth.finish_oauth("signed-library-cookie", start["session_id"], "pixiv://account/login?code=valid-code&via=login")
    assert result == {"refresh_token": "saved-refresh-token", "user_id": "123"}
    with pytest.raises(ConnectorError, match="已使用"):
        oauth.finish_oauth("signed-library-cookie", start["session_id"], "valid-code")
    assert len(sent) == 1


@pytest.mark.parametrize("value", ["raw-code_1", "code=raw-code_1&state=state", "pixiv://account/login?code=raw-code_1", oauth.CALLBACK_URI + "?state=state&code=raw-code_1&via=login"])
def test_supported_manual_code_formats(value):
    assert oauth.extract_code(value) == "raw-code_1"


@pytest.mark.parametrize("value", ["", "https://evil.example/?code=token", "https://app-api.pixiv.net.evil.example/web/v1/users/auth/pixiv/callback?code=token", "pixiv://account/login?code=one&code=two", "pixiv://account/login?code=", "pixiv://evil/login?code=token", oauth.CALLBACK_URI + "?code=a%0Ab", "code=contains+space", "raw code", "code=" + "a" * 8192])
def test_invalid_or_ambiguous_callback_rejected_without_echo(value):
    with pytest.raises(ConnectorError) as error:
        oauth.extract_code(value)
    assert "evil.example" not in str(error.value)
    assert "contains" not in str(error.value)


def test_library_session_binding_prevents_cross_session_consumption(monkeypatch):
    start = oauth.start_oauth("owner-session")
    mock_endpoint(monkeypatch, lambda request: httpx.Response(200, json={"refresh_token": "ok", "user": {"id": "7"}}))
    with pytest.raises(ConnectorError, match="同一书库"):
        oauth.finish_oauth("different-session", start["session_id"], "code")
    assert oauth.finish_oauth("owner-session", start["session_id"], "code")["user_id"] == "7"


def test_expiry_clears_pending_verifier_without_network(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: clock[0])
    start = oauth.start_oauth("session")
    clock[0] += oauth.SESSION_TTL + 1
    mock_endpoint(monkeypatch, lambda request: pytest.fail("Expired authorization must not reach token endpoint"))
    with pytest.raises(ConnectorError, match="过期"):
        oauth.finish_oauth("session", start["session_id"], "code")
    assert not oauth._sessions


def test_inflight_exchange_cannot_be_replayed(monkeypatch):
    start = oauth.start_oauth("session")
    def exchange(request):
        with pytest.raises(ConnectorError, match="已使用"):
            oauth.finish_oauth("session", start["session_id"], "same-code")
        return httpx.Response(200, json={"refresh_token": "accepted", "user": {"id": 9}})
    mock_endpoint(monkeypatch, exchange)
    assert oauth.finish_oauth("session", start["session_id"], "same-code")["refresh_token"] == "accepted"


@pytest.mark.parametrize("response", [httpx.Response(400, json={"error": "secret-code-secret-verifier"}),
    httpx.Response(200, json={"access_token": "secret-access-token"}),
    httpx.Response(200, json={"refresh_token": "secret-refresh-token", "user": {"id": None}}),
    httpx.Response(302, headers={"Location": "https://evil.example/?secret=token"}),
    httpx.Response(200, content=b"not JSON secret-refresh-token")])
def test_failed_exchange_is_consumed_and_errors_do_not_disclose_secrets(monkeypatch, response):
    start = oauth.start_oauth("session")
    mock_endpoint(monkeypatch, lambda request: response)
    with pytest.raises(ConnectorError) as error:
        oauth.finish_oauth("session", start["session_id"], "secret-code")
    assert "secret" not in str(error.value)
    assert "evil" not in str(error.value)
    assert start["session_id"] not in oauth._sessions


def test_exchange_bounds_response_and_wraps_network_error(monkeypatch):
    start = oauth.start_oauth("session")
    mock_endpoint(monkeypatch, lambda request: httpx.Response(200, content=b"x" * (oauth.MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(ConnectorError, match="响应异常"):
        oauth.finish_oauth("session", start["session_id"], "code")
    start = oauth.start_oauth("session")
    def fail(request):
        raise httpx.ConnectError("proxy-password=secret", request=request)
    mock_endpoint(monkeypatch, fail)
    with pytest.raises(ConnectorError, match="代理设置") as error:
        oauth.finish_oauth("session", start["session_id"], "code")
    assert "secret" not in str(error.value)


def test_rate_limits_are_bounded_and_expire(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        oauth.start_oauth("session")
    with pytest.raises(oauth.OAuthRateLimitError):
        oauth.start_oauth("session")
    for _ in range(12):
        with pytest.raises(ConnectorError):
            oauth.finish_oauth("attacker-session", "missing", "code")
    with pytest.raises(oauth.OAuthRateLimitError):
        oauth.finish_oauth("attacker-session", "missing", "code")
    clock[0] += oauth.SESSION_TTL + 1
    assert oauth.start_oauth("session")["session_id"]
    assert len(oauth._sessions) == 1


def test_legacy_nested_token_response_is_supported(monkeypatch):
    start = oauth.start_oauth("session")
    mock_endpoint(monkeypatch, lambda request: httpx.Response(200, json={"response": {"refresh_token": "legacy", "user": {"id": 15}}}))
    assert oauth.finish_oauth("session", start["session_id"], "code") == {"refresh_token": "legacy", "user_id": "15"}


def test_http_client_uses_saved_proxy_once_without_ambient_proxy_or_redirects(monkeypatch):
    from app import network
    calls = []
    def configured_proxy():
        calls.append(True)
        return "http://user:private-password@127.0.0.1:7890"
    monkeypatch.setattr(network, "httpx_proxy", configured_proxy)
    monkeypatch.setattr(oauth.httpx, "Client", lambda **kwargs: kwargs)
    options = oauth._http_client()
    assert options["proxy"] == "http://user:private-password@127.0.0.1:7890"
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    assert options["timeout"] == 25
    assert len(calls) == 1


def test_invalid_proxy_configuration_is_safe_actionable_error(monkeypatch):
    from app import network
    def invalid_proxy():
        raise ValueError("private-proxy-password")
    monkeypatch.setattr(network, "httpx_proxy", invalid_proxy)
    with pytest.raises(ConnectorError, match="代理地址") as error:
        oauth._http_client()
    assert "private" not in str(error.value)


def test_http_oauth_routes_encrypt_verified_account_and_never_return_tokens(client, monkeypatch):
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import SourceAccount
    from app.integrations.credentials import decrypt_credentials
    start = client.post("/api/sources/pixiv/oauth/start")
    assert start.status_code == 200
    mock_endpoint(monkeypatch, lambda request: httpx.Response(200,
        json={"access_token": "not-stored-access", "refresh_token": "encrypted-refresh", "user": {"id": 555}}))
    body = {"session_id": start.json()["session_id"], "code": "pixiv://account/login?code=callback-code", "label": "浏览器授权"}
    finish = client.post("/api/sources/pixiv/oauth/finish", json=body)
    assert finish.status_code == 200, finish.text
    assert finish.json()["auth_status"] == "verified"
    assert "encrypted-refresh" not in finish.text and "not-stored-access" not in finish.text
    with SessionLocal() as db:
        account = db.scalar(select(SourceAccount).where(SourceAccount.id == finish.json()["id"]))
        assert "encrypted-refresh" not in account.credentials_encrypted
        assert decrypt_credentials(account.credentials_encrypted) == {"refresh_token": "encrypted-refresh", "user_id": "555"}
        assert account.verified_at is not None
    assert client.post("/api/sources/pixiv/oauth/finish", json=body).status_code == 400
    client.post("/api/auth/logout")
    assert client.post("/api/sources/pixiv/oauth/start").status_code == 401
