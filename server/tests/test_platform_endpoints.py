"""Configuration and synthetic HTTP/SDK boundaries for platform API switching."""
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete

from app import endpoint_config
from app.database import SessionLocal
from app.integrations import ehentai, jm, picacg, pixiv
from app.integrations.base import ConnectorError
from app.models import NetworkSetting
from test_library import client
from test_devices import bind, bearer
from test_connector_stages import transport


@pytest.fixture(autouse=True)
def isolate_endpoints(client, monkeypatch):
    monkeypatch.setattr(endpoint_config.socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    yield
    with SessionLocal() as db:
        db.execute(delete(NetworkSetting).where(NetworkSetting.key.startswith(endpoint_config.PREFIX)))
        db.commit()


def configure(client, platform, url):
    response = client.put('/api/platform-endpoints/' + platform, json={'enabled': True, 'custom_url': url})
    assert response.status_code == 200, response.text
    return response.json()


def test_defaults_and_encrypted_independent_settings(client):
    result = client.get('/api/platform-endpoints')
    assert result.status_code == 200
    entries = {entry['platform']: entry for entry in result.json()['items']}
    assert set(entries) == {'pixiv', 'picacg', 'ehentai', 'jm', 'kemono'}
    assert all(not entry['enabled'] and entry['custom_url'] is None and entry['default_url'] == entry['effective_url'] for entry in entries.values())
    configured = configure(client, 'pixiv', 'https://mirror.example.org/pixiv/')
    assert configured['effective_url'] == 'https://mirror.example.org/pixiv'
    with SessionLocal() as db:
        stored = db.get(NetworkSetting, endpoint_config.PREFIX + 'pixiv').value_encrypted
        assert 'mirror.example.org' not in stored and 'local-library' not in stored
    assert endpoint_config.get_endpoint('pixiv') == configured['effective_url']
    assert endpoint_config.get_endpoint('picacg') == entries['picacg']['default_url']
    disabled = client.put('/api/platform-endpoints/pixiv', json={'enabled': False, 'custom_url': configured['custom_url']}).json()
    assert disabled['custom_url'] == configured['custom_url']
    assert disabled['effective_url'] == disabled['default_url']
    assert client.put('/api/platform-endpoints/pixiv', json={'enabled': False, 'custom_url': None}).json()['custom_url'] is None


@pytest.mark.parametrize('url', [
    'http://example.org', 'https://localhost', 'https://127.0.0.1', 'https://10.0.0.1/api',
    'https://[::1]', 'https://api.local', 'https://user:password@example.org',
    'https://example.org:8443', 'https://example.org?token=private', 'https://example.org#fragment',
    'https://example.org/../api', 'https://example.org/%2e%2e/api', 'https://example.org/a//b',
    'https://example.org\\@localhost', 'https://example.org/path\nother',
])
def test_bad_endpoint_rejected_without_mutation(client, url):
    assert client.put('/api/platform-endpoints/pixiv', json={'enabled': True, 'custom_url': url}).status_code == 400
    assert endpoint_config.get_endpoint('pixiv') == endpoint_config.DEFAULTS['pixiv'][1]


def test_public_dns_required_but_disabling_dead_endpoint_remains_possible(client, monkeypatch):
    custom = 'https://mirror.example.org/api'
    configure(client, 'pixiv', custom)
    monkeypatch.setattr(endpoint_config.socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.9', 443))])
    assert client.put('/api/platform-endpoints/pixiv', json={'enabled': True, 'custom_url': custom}).status_code == 400
    assert client.put('/api/platform-endpoints/pixiv', json={'enabled': False, 'custom_url': custom}).status_code == 200
    assert client.put('/api/platform-endpoints/jm', json={'enabled': True, 'custom_url': 'https://mirror.example.org/path'}).status_code == 400
    assert client.put('/api/platform-endpoints/pixiv', json={'enabled': True, 'custom_url': None}).status_code == 400


def test_device_reads_and_requires_fresh_admin_password_without_losing_session(client):
    device = bearer(bind(client))
    client.cookies.clear()
    assert client.get('/api/platform-endpoints').status_code == 401
    assert client.get('/api/platform-endpoints', headers=device).status_code == 200
    data = {'enabled': True, 'custom_url': 'https://mirror.example.org/api'}
    assert client.put('/api/platform-endpoints/pixiv', headers=device, json=data).status_code == 403
    assert client.put('/api/platform-endpoints/pixiv', headers=device, json={**data, 'admin_password': 'wrong'}).status_code == 403
    assert client.get('/api/platform-endpoints', headers=device).status_code == 200
    response = client.put('/api/platform-endpoints/pixiv', headers=device, json={**data, 'admin_password': 'local-library'})
    assert response.status_code == 200 and 'password' not in response.text
    assert client.put('/api/platform-endpoints/not-a-platform', headers=device, json=data).status_code == 403


def test_cookie_change_is_protected_against_cross_origin_requests(client):
    response = client.put('/api/platform-endpoints/pixiv', headers={'Origin': 'https://unrelated.example.org'},
        json={'enabled': False, 'custom_url': None})
    assert response.status_code == 403


def test_picacg_custom_prefix_keeps_original_signing_path_and_token(client, transport):
    configure(client, 'picacg', 'https://mirror.example.org/pica')
    ident = 'a' * 24
    transport.routes['/pica/comics/' + ident] = {'code': 200, 'data': {'comic': {'title': 'Synthetic comic'}}}
    result = picacg.PicACGConnector({'token': 'synthetic-token'}).fetch(ident, download=False)
    assert result.title == 'Synthetic comic'
    method, url, headers = transport.outgoing[0]
    assert url == 'https://mirror.example.org/pica/comics/' + ident
    expected = picacg.signed_headers('comics/' + ident, method, token='synthetic-token', timestamp=headers['time'], nonce=headers['nonce'])
    assert headers['signature'] == expected['signature'] and headers['authorization'] == 'synthetic-token'


def test_picacg_connector_snapshot_survives_settings_change(client, transport):
    configure(client, 'picacg', 'https://first.example.org/api')
    transport.routes['/api/users/profile'] = {'code': 200, 'data': {}}
    connector = picacg.PicACGConnector({'token': 'synthetic-token'})
    connector._request('users/profile')
    configure(client, 'picacg', 'https://second.example.org/api')
    connector._request('users/profile')
    picacg.PicACGConnector({'token': 'synthetic-token'})._request('users/profile')
    assert [url for _, url, _ in transport.outgoing] == ['https://first.example.org/api/users/profile'] * 2 + ['https://second.example.org/api/users/profile']


@pytest.mark.parametrize('location', ['https://sub.mirror.example.org/pica/users/profile', '/outside/users/profile', 'https://127.0.0.1/private'])
def test_picacg_redirect_cannot_escape_custom_origin_or_prefix(client, transport, location):
    configure(client, 'picacg', 'https://mirror.example.org/pica')
    transport.routes['/pica/users/profile'] = httpx.Response(302, headers={'Location': location})
    with pytest.raises(ConnectorError):
        picacg.PicACGConnector({'token': 'synthetic-token'})._request('users/profile')
    assert len(transport.outgoing) == 1


def test_custom_endpoint_is_rechecked_before_transport(client, transport, monkeypatch):
    configure(client, 'picacg', 'https://mirror.example.org/pica')
    monkeypatch.setattr(endpoint_config.socket, 'getaddrinfo', lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ConnectorError, match='公网'):
        picacg.PicACGConnector({'token': 'synthetic-token'})._request('users/profile')
    assert transport.outgoing == []


def test_ehentai_switches_only_metadata_endpoint_and_sends_no_cookies(client, transport):
    configure(client, 'ehentai', 'https://mirror.example.org/metadata/api.php')
    transport.routes['/metadata/api.php'] = {'gmetadata': [{'title': 'Synthetic gallery', 'filecount': '1', 'tags': []}]}
    result = ehentai.EHentaiConnector({'ipb_member_id': 'synthetic', 'ipb_pass_hash': 'synthetic-pass'}).fetch('123:abcdef0123', download=False)
    assert result.title == 'Synthetic gallery' and result.source_url.startswith('https://e-hentai.org/g/')
    assert transport.outgoing[0][1] == 'https://mirror.example.org/metadata/api.php'
    assert 'cookie' not in transport.outgoing[0][2]


def pixiv_routes(transport):
    transport.routes['/auth/token'] = {'response': {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh', 'user': {'id': 101}}}
    transport.routes['/px/v1/illust/detail'] = {'illust': {'id': 123, 'title': 'Synthetic work', 'type': 'illust',
        'page_count': 1, 'user': {'id': 201, 'name': 'Example'}, 'tags': []}}


def test_pixiv_sdk_rewrites_app_api_but_never_oauth(client, transport):
    configure(client, 'pixiv', 'https://mirror.example.org/px')
    pixiv_routes(transport)
    work = pixiv.PixivConnector({'refresh_token': 'synthetic-refresh'}).fetch('123', download=False)
    assert work.title == 'Synthetic work'
    assert [url for _, url, _ in transport.outgoing] == ['https://oauth.secure.pixiv.net/auth/token', 'https://mirror.example.org/px/v1/illust/detail?illust_id=123']
    assert transport.outgoing[1][2]['Authorization'] == 'Bearer synthetic-access'


@pytest.mark.parametrize('stage', ['oauth', 'app'])
def test_pixiv_redirects_never_forward_refresh_or_access_tokens(client, transport, stage):
    configure(client, 'pixiv', 'https://mirror.example.org/px')
    pixiv_routes(transport)
    transport.routes['/auth/token' if stage == 'oauth' else '/px/v1/illust/detail'] = httpx.Response(307, headers={'Location': 'https://unrelated.example.org/receive'})
    with pytest.raises(ConnectorError, match='重定向') as failure:
        pixiv.PixivConnector({'refresh_token': 'synthetic-refresh'}).fetch('123', download=False)
    assert len(transport.outgoing) == (1 if stage == 'oauth' else 2)
    assert all('unrelated' not in url for _, url, _ in transport.outgoing)
    assert 'synthetic-refresh' not in str(failure.value)


def test_pixiv_sdk_rejects_unrelated_direct_requests(client, transport):
    configure(client, 'pixiv', 'https://mirror.example.org/px')
    pixiv_routes(transport)
    api = pixiv.PixivConnector({'refresh_token': 'synthetic-refresh'})._api()
    with pytest.raises(ConnectorError, match='范围'):
        api.no_auth_requests_call('GET', 'https://unrelated.example.org/receive')
    assert len(transport.outgoing) == 1


@pytest.mark.parametrize('redirect', [False, True])
def test_jm_sdk_uses_only_selected_html_domain(client, monkeypatch, redirect):
    import jmcomic
    from curl_cffi import requests as curl_requests
    configure(client, 'jm', 'https://mirror.example.org')
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs['allow_redirects'] is False
        return SimpleNamespace(status_code=302 if redirect else 200, headers={'Location': 'https://unrelated.example.org'})
    def album(sdk, ident):
        sdk.get('/album/' + ident)
        return SimpleNamespace(name='Synthetic album', authors=[], tags=[], description='', page_count=1)
    monkeypatch.setattr(curl_requests, 'get', get)
    monkeypatch.setattr(jmcomic.JmHtmlClient, 'get_album_detail', album)
    monkeypatch.setattr(jm, 'fetch_preview_cover', lambda *args, **kwargs: None)
    if redirect:
        with pytest.raises(ConnectorError, match='重定向'):
            jm.JMConnector({'AVS': 'synthetic-cookie'}).fetch('123', download=False)
    else:
        assert jm.JMConnector({'AVS': 'synthetic-cookie'}).fetch('123', download=False).title == 'Synthetic album'
    assert len(calls) == 1 and calls[0][0] == 'https://mirror.example.org/album/123'
