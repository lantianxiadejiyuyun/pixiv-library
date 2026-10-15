import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import app_updates as updates
from app.database import SessionLocal
from app.models import AndroidRelease
from test_devices import bind, bearer
from test_library import client

HEADERS = {"Content-Type": "application/vnd.android.package-archive"}


@pytest.fixture
def verified(monkeypatch):
    metadata = dict(version_code=4, version_name="0.4.0", min_sdk=24, certificate_sha256=updates.CERTIFICATE)
    monkeypatch.setattr(updates, "inspect_apk", lambda path: dict(metadata))
    return metadata


def test_release_publish_download_and_device_privilege(client, verified):
    assert client.get('/api/app-updates/android').json() == {'release': None}
    device = bearer(bind(client))
    assert client.post('/api/app-updates/android', headers={**HEADERS, **device}, content=b'apk').status_code == 403
    payload = b'signed-synthetic-apk'
    result = client.post('/api/app-updates/android', params={'notes': '在线更新'}, headers=HEADERS, content=payload)
    assert result.status_code == 200, result.text
    release = result.json()['release']
    assert release['version_code'] == 4 and release['sha256'] == hashlib.sha256(payload).hexdigest()
    assert release['notes'] == '在线更新' and 'relative_path' not in release
    client.cookies.clear()
    assert client.get('/api/app-updates/android').status_code == 401
    assert client.get(release['download_path']).status_code == 401
    assert client.get('/api/app-updates/android', headers=device).json()['release'] == release
    download = client.get(release['download_path'], headers=device)
    assert download.content == payload
    assert download.headers['content-type'] == 'application/vnd.android.package-archive'


def test_version_monotonic_idempotent_and_immutable(client, verified):
    url = '/api/app-updates/android'
    first = client.post(url, headers=HEADERS, content=b'apk').json()
    assert client.post(url, headers=HEADERS, content=b'apk').json() == first
    assert client.post(url, headers=HEADERS, content=b'different').status_code == 409
    verified['version_code'] = 3
    assert client.post(url, headers=HEADERS, content=b'older').status_code == 409
    verified['version_code'] = 5
    assert client.post(url, headers=HEADERS, content=b'new').status_code == 200
    assert client.get(url).json()['release']['version_code'] == 5
    assert client.get(first['release']['download_path']).content == b'apk'


@pytest.mark.parametrize('headers,body,code', [
    ({}, b'apk', 415), (HEADERS, b'', 400),
    ({**HEADERS, 'content-length': '999999999'}, b'apk', 413),
    ({**HEADERS, 'content-length': 'invalid'}, b'apk', 400),
    ({**HEADERS, 'content-length': '10'}, b'apk', 400),
    ({**HEADERS, 'Origin': 'https://evil.test'}, b'apk', 403),
])
def test_upload_validation(client, verified, headers, body, code):
    assert client.post('/api/app-updates/android', headers=headers, content=body).status_code == code


def test_chunked_limit_cleanup_and_no_publication(client, verified, monkeypatch, tmp_path):
    monkeypatch.setattr(updates, 'MAX_APK_BYTES', 3)
    assert client.post('/api/app-updates/android', headers=HEADERS, content=iter([b'ab', b'cd'])).status_code == 413
    assert client.get('/api/app-updates/android').json()['release'] is None
    assert not updates._upload_lock.locked()


def test_corrupt_published_blob_rejected(client, verified):
    response = client.post('/api/app-updates/android', headers=HEADERS, content=b'apk').json()['release']
    with SessionLocal() as db:
        release = db.get(AndroidRelease, 4)
        path = updates.resolve_asset_path(release.relative_path)
    path.write_bytes(b'bad')
    assert client.get(response['download_path']).status_code == 503


def test_missing_tools_fail_closed(client, monkeypatch):
    monkeypatch.setenv('LIBRARY_APKSIGNER', 'missing-qiye-tool-123')
    assert client.post('/api/app-updates/android', headers=HEADERS, content=b'fake').status_code == 503
    assert client.get('/api/app-updates/android').json()['release'] is None


@pytest.mark.parametrize('mutation', ['cert', 'unsigned', 'package', 'debug', 'code'])
def test_signature_and_metadata_fail_closed(monkeypatch, mutation):
    signature = f'Verified using v2 scheme (APK Signature Scheme v2): true\nSigner #1 certificate SHA-256 digest: {updates.CERTIFICATE}\n'
    metadata = "package: name='app.qiye.qiye_library' versionCode='4' versionName='0.4.0'\nsdkVersion:'24'\n"
    if mutation == 'cert': signature = signature.replace(updates.CERTIFICATE, '0' * 64)
    if mutation == 'unsigned': signature = signature.replace('true', 'false')
    if mutation == 'package': metadata = metadata.replace('app.qiye.qiye_library', 'evil.app')
    if mutation == 'debug': metadata += 'application-debuggable\n'
    if mutation == 'code': metadata = metadata.replace("versionCode='4'", "versionCode='0'")
    monkeypatch.setattr(updates, 'tool', lambda command: signature if 'verify' in command else metadata)
    with pytest.raises(HTTPException) as failure:
        updates.inspect_apk(Path('synthetic.apk'))
    assert failure.value.status_code == 400


def test_signed_metadata_extracted_not_client_claimed(monkeypatch):
    signature = f'Verified using v2 scheme (APK Signature Scheme v2): true\nSigner #1 certificate SHA-256 digest: {updates.CERTIFICATE}\n'
    metadata = "package: name='app.qiye.qiye_library' versionCode='4' versionName='0.4.0'\nsdkVersion:'24'\n"
    monkeypatch.setattr(updates, 'tool', lambda command: signature if 'verify' in command else metadata)
    assert updates.inspect_apk(Path('synthetic.apk')) == dict(version_code=4, version_name='0.4.0', min_sdk=24, certificate_sha256=updates.CERTIFICATE)
