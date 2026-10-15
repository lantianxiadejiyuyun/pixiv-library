"""Classify observed invisible placeholders and preserve SDK HTTP 429 reasons."""
import httpx
import pytest
from sqlalchemy import select

from app import jobs, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError
from app.integrations.pixiv import PixivConnector
from app.models import Job
from test_connector_stages import transport, pixiv_metadata
from test_library import client
from test_pixiv_accounts import accounts, enable


@pytest.mark.parametrize('novel', [False, True])
@pytest.mark.parametrize('download', [False, True])
def test_invisible_work_is_permission_error_before_missing_title_id_or_body(transport, novel, download):
    connector = pixiv_metadata(transport, novel=novel)
    transport.routes['/v2/novel/detail' if novel else '/v1/illust/detail'] = {
        'novel' if novel else 'illust': {'id': 0, 'title': '', 'visible': False, 'page_count': 0,
            'image_urls': {'medium': 'https://i.pximg.net/private-placeholder.png'}}}
    with pytest.raises(ConnectorError, match='当前账号不可见') as error:
        connector.fetch('novel:123' if novel else '123', download=download)
    assert 'R18/R18G' in str(error.value)
    assert '数据不完整' not in str(error.value)
    assert len(transport.outgoing) == 2  # Auth + detail; never download a placeholder.


@pytest.mark.parametrize('phase', ['auth', 'detail'])
def test_rate_limit_preserves_explicit_http_reason_and_retry_after(transport, phase):
    connector = pixiv_metadata(transport)
    transport.routes['/auth/token' if phase == 'auth' else '/v1/illust/detail'] = httpx.Response(
        429, headers={'Retry-After': '75'}, text='private-refresh-token-and-response')
    with pytest.raises(ConnectorError, match='HTTP 429') as error:
        connector.fetch('123')
    assert '登录失败' not in str(error.value)
    assert 'private' not in str(error.value)
    assert transport.cooldowns == [75]
    assert len(transport.outgoing) == (1 if phase == 'auth' else 2)


@pytest.mark.parametrize('phase', ['metadata', 'content'])
def test_failed_download_identifies_actual_account_and_stage_without_fallback(client, accounts, monkeypatch, phase):
    enable(client, accounts)
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    calls = []
    class Connector:
        def __init__(self, token): self.token = token
        def normalize_id(self, value): return value
        def fetch(self, ident, *, download=True, **kwargs):
            calls.append((self.token, download))
            if (phase == 'metadata' and not download) or (phase == 'content' and download):
                PixivConnector._metadata_work({'id': 0, 'title': '', 'visible': False}, novel=False)
            from app.integrations.base import RemoteWork
            return RemoteWork(remote_id=ident, title='Synthetic', source_url='https://example.invalid', metadata_only=True)
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: Connector(credentials['refresh_token']))
    with SessionLocal() as db:
        ident = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123'}).id
    assert jobs.run_once()
    result = client.get('/api/jobs/' + ident).json()
    assert result['status'] == 'failed'
    assert ('详情账号「主账号」' if phase == 'metadata' else '下载账号「副账号」') in result['error']
    assert '当前账号不可见' in result['error']
    assert all(token != 'main-test' or not download for token, download in calls)
    assert 'main-test' not in result['error'] and 'content-test' not in result['error']
    assert client.get('/api/items').json()['total'] == 0
