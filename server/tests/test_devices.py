from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app import device_auth
from app.auth import COOKIE_NAME
from app.database import SessionLocal
from app.models import DevicePairing, DeviceSession, utcnow
from test_library import client, upload


@pytest.fixture(autouse=True)
def reset_rate_limits():
    with device_auth._rate_lock:
        device_auth._rates.clear()
    yield
    with device_auth._rate_lock:
        device_auth._rates.clear()


def pairing(client):
    response = client.post('/api/devices/pairing', json={'server_url': 'http://192.168.1.5:8000/'})
    assert response.status_code == 200, response.text
    return response.json(), json.loads(response.json()['qr_payload'])['pairing_token']


def exchange(client, token, name='我的手机'):
    return client.post('/api/devices/pairing/exchange', json={'pairing_token': token, 'device_name': name})


def bind(client):
    _, token = pairing(client)
    response = exchange(client, token)
    assert response.status_code == 200, response.text
    return response.json()


def bearer(device):
    return {'Authorization': 'Bearer ' + device['access_token']}


def test_pair_exchange_is_unauthenticated_single_use_and_hash_only(client, caplog):
    cookie = client.cookies.get(COOKIE_NAME)
    qr, token = pairing(client)
    payload = json.loads(qr['qr_payload'])
    assert payload == {'type': 'qiye-library', 'version': 1, 'server_url': 'http://192.168.1.5:8000', 'pairing_token': token}
    assert cookie not in qr['qr_payload'] and 'local-library' not in qr['qr_payload']
    client.cookies.clear()
    response = exchange(client, token, '  Android 手机  ')
    assert response.status_code == 200
    device = response.json()
    assert device['device_name'] == 'Android 手机' and device['server_name'] == '栖页'
    assert set(device) == {'device_id', 'device_name', 'access_token', 'expires_at', 'server_name'}
    with SessionLocal() as db:
        stored_qr = db.get(DevicePairing, qr['id'])
        stored_device = db.get(DeviceSession, device['device_id'])
        assert stored_qr.token_hash == device_auth.token_digest(token)
        assert stored_qr.consumed_at is not None
        assert stored_qr.expires_at - stored_qr.created_at == timedelta(minutes=5)
        assert stored_device.token_hash == device_auth.token_digest(device['access_token'])
        assert stored_device.expires_at - stored_device.created_at == timedelta(days=90)
        serialized = repr(stored_device.__dict__) + repr(stored_qr.__dict__)
        assert token not in serialized and device['access_token'] not in serialized
    replay = exchange(client, token)
    assert replay.status_code == 400
    assert token not in replay.text and token not in caplog.text and device['access_token'] not in caplog.text
    assert client.get('/api/devices').status_code == 401
    client.cookies.set(COOKIE_NAME, cookie)
    entry = client.get('/api/devices').json()['items'][0]
    assert entry['id'] == device['device_id'] and entry['status'] == 'active'
    assert entry['last_used_at'] is None and entry['revoked_at'] is None
    assert 'token' not in json.dumps(entry) and 'hash' not in json.dumps(entry)


def test_device_can_read_assets_series_and_update_progress_without_cookie(client):
    image = upload(client).json()
    novel = upload(client, [('files', ('story.txt', '第一章[newpage]第二章'.encode(), 'text/plain'))], type='novel').json()
    series = client.post('/api/series', json={'title': '系列', 'kind': 'novel', 'entries': [{'item_id': novel['id']}]}).json()
    device = bind(client)
    client.cookies.clear()
    paths = ['/api/health', '/api/dashboard', '/api/library/collections', '/api/items', '/api/tags',
             f"/api/items/{image['id']}", image['assets'][0]['url'], image['cover_url'],
             f"/api/items/{image['id']}/export", f"/api/series/{series['id']}",
             f"/api/assets/{novel['assets'][0]['id']}/text-sections"]
    for path in paths:
        response = client.get(path, headers=bearer(device))
        assert response.status_code == 200, (path, response.text)
    progress = client.put(f"/api/items/{novel['id']}/progress", headers=bearer(device), json={'page': 0, 'section': 1, 'offset': .45})
    assert progress.status_code == 200
    assert client.get(f"/api/items/{novel['id']}", headers=bearer(device)).json()['progress']['section'] == 1
    with SessionLocal() as db:
        assert db.get(DeviceSession, device['device_id']).last_used_at is not None
    # Whitelisted download reaches the actual endpoint, rather than granting
    # every integration endpoint. This local item has no remote content to fetch.
    assert client.post(f"/api/items/{image['id']}/download", headers=bearer(device)).status_code == 400


@pytest.mark.parametrize(('method', 'path'), [
    ('POST', '/api/database/export'), ('GET', '/api/sources'), ('GET', '/api/network/managed'),
    ('GET', '/api/devices'), ('POST', '/api/devices/pairing'), ('DELETE', '/api/devices/anything'),
    ('GET', '/api/queue/status'), ('GET', '/api/jobs'), ('POST', '/api/ai/characters/search'),
    ('POST', '/api/imports/upload'), ('POST', '/api/series'), ('PUT', '/api/series/anything'),
    ('PATCH', '/api/items/anything'), ('DELETE', '/api/items/anything'),
    ('GET', '/api/auth/status'), ('POST', '/api/auth/login'), ('POST', '/api/auth/logout'),
    ('GET', '/api/docs'), ('GET', '/openapi.json'),
])
def test_device_is_not_admin_even_when_admin_cookie_is_present(client, method, path):
    device = bind(client)
    response = client.request(method, path, headers=bearer(device))
    assert response.status_code == 403, (path, response.text)
    assert device['access_token'] not in response.text


def test_new_pairing_replaces_old_and_expiry_rejects_exact_boundary(client, monkeypatch):
    _, old_token = pairing(client)
    current, current_token = pairing(client)
    assert exchange(client, old_token).status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DevicePairing.id))) == 1
        expires = db.get(DevicePairing, current['id']).expires_at
    monkeypatch.setattr(device_auth, 'utcnow', lambda: expires)
    assert exchange(client, current_token).status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeviceSession.id))) == 0


def test_same_code_concurrent_exchange_has_exactly_one_winner(client):
    _, token = pairing(client)
    start = Barrier(2)
    def attempt(index):
        with SessionLocal() as db:
            start.wait(timeout=5)
            try:
                return device_auth.exchange_pairing(db, token, f'phone {index}')
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count(400) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count(DeviceSession.id))) == 1


def test_revocation_expiry_and_self_disconnect(client):
    first, second = bind(client), bind(client)
    assert client.delete('/api/devices/' + first['device_id']).json() == {'ok': True}
    assert client.delete('/api/devices/' + first['device_id']).status_code == 200
    assert client.get('/api/items', headers=bearer(first)).status_code == 401
    assert client.get('/api/items', headers=bearer(second)).status_code == 200
    assert client.post('/api/devices/disconnect', headers=bearer(second)).json() == {'ok': True}
    assert client.get('/api/items', headers=bearer(second)).status_code == 401
    assert all(row['status'] == 'revoked' for row in client.get('/api/devices').json()['items'])
    third = bind(client)
    with SessionLocal() as db:
        db.get(DeviceSession, third['device_id']).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert client.get('/api/items', headers=bearer(third)).status_code == 401
    assert client.get('/api/devices').json()['items'][0]['status'] == 'expired'
    assert client.post('/api/devices/disconnect').status_code == 401  # Cookie cannot choose an arbitrary device.


def test_device_quota_does_not_consume_pairing_when_full(client, monkeypatch):
    monkeypatch.setattr(device_auth, 'MAX_ACTIVE_DEVICES', 1)
    first = bind(client)
    _, token = pairing(client)
    assert exchange(client, token).status_code == 409
    client.delete('/api/devices/' + first['device_id'])
    assert exchange(client, token).status_code == 200


def test_tokens_are_header_only_and_cannot_bypass_origin(client):
    device = bind(client)
    assert client.post('/api/devices/pairing', headers={'Origin': 'https://evil.example'}, json={'server_url': 'http://localhost'}).status_code == 403
    assert client.post('/api/devices/disconnect', headers={**bearer(device), 'Origin': 'https://evil.example'}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/items', params={'access_token': device['access_token']}).status_code == 401
    assert client.get('/api/items', headers={'Authorization': device['access_token']}).status_code == 401
    assert client.get('/api/items', headers=[('Authorization', bearer(device)['Authorization']), ('Authorization', bearer(device)['Authorization'])]).status_code == 401
    assert client.post('/api/devices/pairing', json={'server_url': 'http://localhost'}).status_code == 401


def test_exchange_rate_limit_is_shared_despite_forwarded_headers_and_errors_are_safe(client):
    for number in range(20):
        response = client.post('/api/devices/pairing/exchange', headers={'X-Forwarded-For': f'192.168.0.{number}'}, json={'pairing_token': 'secret-invalid-token', 'device_name': 'phone'})
        assert response.status_code == 400
        assert 'secret-invalid-token' not in response.text
    response = exchange(client, 'secret-invalid-token')
    assert response.status_code == 429 and response.headers['Retry-After'] == '60'
    with device_auth._rate_lock:
        assert 'secret-invalid-token' not in repr(device_auth._rates)


def test_create_rate_limit_and_validation_never_reflect_inputs(client):
    for _ in range(10):
        pairing(client)
    assert client.post('/api/devices/pairing', json={'server_url': 'http://localhost'}).status_code == 429
    response = exchange(client, 'never-reflect-' + 'x' * 256, '\n')
    assert response.status_code == 422 and 'never-reflect-' not in response.text


@pytest.mark.parametrize(('value', 'expected'), [
    ('http://192.168.1.2:8000/', 'http://192.168.1.2:8000'), ('https://EXAMPLE.COM:443/', 'https://example.com'),
    ('http://localhost:80', 'http://localhost'), ('http://127.0.0.1:8801', 'http://127.0.0.1:8801'),
    ('http://nas.local', 'http://nas.local'), ('http://nas', 'http://nas'),
    ('http://[fd12::1]:8000', 'http://[fd12::1]:8000'), ('http://[::1]', 'http://[::1]'),
])
def test_server_url_normalization(value, expected):
    assert device_auth.normalize_server_url(value) == expected


@pytest.mark.parametrize('value', [
    'http://example.com', 'http://8.8.8.8', 'http://0.0.0.0', 'http://[::]', 'https://224.0.0.1',
    'https://user:secret@example.com', 'https://example.com/api', 'https://example.com/?secret=x',
    'https://example.com/#secret', 'https://example.com?', 'https://example.com#',
    'https://example.com\\@localhost', 'javascript:alert(1)', 'http://127.1', 'http://2130706433',
    'http://[fe80::1%25eth0]', 'https://example.com:0', 'https://example.com:65536', 'https://-bad.example',
])
def test_server_url_rejects_ambiguous_or_credential_addresses(value):
    with pytest.raises(ValueError) as result:
        device_auth.normalize_server_url(value)
    assert value not in str(result.value)
