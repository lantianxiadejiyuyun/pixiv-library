import pytest

from app.config import Settings, settings
from test_library import client


@pytest.mark.parametrize('origin', ['https://frontend.example', 'http://192.168.100.100:3000', 'null'])
def test_wildcard_allows_cross_site_login_and_authenticated_write(client, monkeypatch, origin):
    monkeypatch.setattr(settings, 'allowed_origins', ['*'])
    headers = {'Origin': origin, 'Sec-Fetch-Site': 'cross-site'}
    client.cookies.clear()
    login = client.post('/api/auth/login', headers=headers, json={'password': 'local-library'})
    assert login.status_code == 200
    assert login.headers['access-control-allow-origin'] == origin
    assert login.headers['access-control-allow-credentials'] == 'true'
    assert 'origin' in login.headers['vary'].lower()
    assert 'HttpOnly' in login.headers['set-cookie']
    response = client.post('/api/favorite-groups', headers=headers, json={'name': 'Synthetic cross-origin group'})
    assert response.status_code == 200, response.text
    assert response.headers['access-control-allow-origin'] == origin


@pytest.mark.parametrize('method', ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
def test_wildcard_preflight_accepts_methods_headers_and_private_network(client, monkeypatch, method):
    monkeypatch.setattr(settings, 'allowed_origins', ['*'])
    client.cookies.clear()
    requested = 'authorization, content-type, range, x-custom-client'
    response = client.options('/api/queue/settings', headers={
        'Origin': 'https://another.example', 'Sec-Fetch-Site': 'cross-site',
        'Access-Control-Request-Method': method, 'Access-Control-Request-Headers': requested,
        'Access-Control-Request-Private-Network': 'true'})
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'https://another.example'
    assert response.headers['access-control-allow-credentials'] == 'true'
    assert response.headers['access-control-allow-private-network'] == 'true'
    assert method in response.headers['access-control-allow-methods']
    assert response.headers['access-control-allow-headers'] == requested


def test_wildcard_does_not_bypass_password_cookie_or_device_authentication(client, monkeypatch):
    monkeypatch.setattr(settings, 'allowed_origins', ['*'])
    headers = {'Origin': 'https://anywhere.example', 'Sec-Fetch-Site': 'cross-site'}
    client.cookies.clear()
    response = client.post('/api/auth/login', json={'password': 'wrong'}, headers=headers)
    assert response.status_code == 401
    assert response.headers['access-control-allow-origin'] == headers['Origin']
    for path in ('/api/items', '/api/queue/status'):
        response = client.get(path, headers=headers)
        assert response.status_code == 401
        assert response.headers['access-control-allow-origin'] == headers['Origin']
    response = client.put('/api/queue/settings', json={'paused': True},
        headers={**headers, 'Authorization': 'Bearer invalid'})
    assert response.status_code == 401
    assert response.headers['access-control-allow-origin'] == headers['Origin']


def test_specific_origins_and_empty_default_keep_their_write_boundary(client, monkeypatch):
    monkeypatch.setattr(settings, 'allowed_origins', ['https://listed.example'])
    assert client.post('/api/auth/login', json={'password': 'local-library'},
        headers={'Origin': 'https://listed.example'}).status_code == 200
    denied = client.post('/api/auth/logout', headers={'Origin': 'https://other.example'})
    assert denied.status_code == 403 and 'access-control-allow-origin' not in denied.headers
    preflight = client.options('/api/queue/settings', headers={'Origin': 'https://other.example',
        'Access-Control-Request-Method': 'PUT'})
    assert preflight.status_code == 400
    monkeypatch.setattr(settings, 'allowed_origins', [])
    assert client.post('/api/auth/logout', headers={'Origin': 'https://listed.example'}).status_code == 403
    assert client.post('/api/auth/logout', headers={'Origin': 'http://testserver'}).status_code == 200


def test_cors_exposes_download_and_video_range_headers(client, monkeypatch):
    monkeypatch.setattr(settings, 'allowed_origins', ['*'])
    response = client.get('/api/health', headers={'Origin': 'https://viewer.example'})
    exposed = response.headers['access-control-expose-headers'].lower()
    assert all(name in exposed for name in ('content-disposition', 'content-range', 'accept-ranges'))
    assert client.get('/api/health').status_code == 200


def test_environment_parses_wildcard_among_other_origins(monkeypatch):
    monkeypatch.setenv('LIBRARY_ALLOWED_ORIGINS', ' https://listed.example/ , * ')
    assert Settings().allowed_origins == ['https://listed.example', '*']
