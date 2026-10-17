"""Retry eligibility is additional to the original role and shared concurrency."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import jobs, pixiv_accounts, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError, RemoteWork
from app.integrations.credentials import encrypt_credentials
from app.models import Job, SourceAccount, utcnow
from test_library import client, png

URL = '/api/sources/pixiv/account-roles'


@pytest.fixture
def roles(client):
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    with SessionLocal() as db:
        rows = [SourceAccount(platform='pixiv', label=name,
            credentials_encrypted=encrypt_credentials({'refresh_token': name + '-synthetic'}))
            for name in ('main', 'auto', 'retry', 'manual', 'details', 'checks')]
        db.add_all(rows); db.commit()
        ids = {row.label: row.id for row in rows}
    assert configure(client, ids).status_code == 200
    return ids


def configure(client, ids, **changes):
    return client.put(URL, json={'enabled': True, 'main_account_id': ids['main'],
        'content_account_ids': [ids['auto'], ids['retry']], 'manual_account_ids': [ids['manual']],
        'detail_account_ids': [ids['details']], 'check_account_ids': [ids['checks']], **changes})


def retry(client, kind='import', **payload):
    with SessionLocal() as db:
        job = Job(kind=kind, status='failed', error='Synthetic previous failure',
                  payload={'platform': 'pixiv', 'remote_id': '42', **payload})
        db.add(job); db.commit(); ident = job.id
    response = client.post('/api/jobs/' + ident + '/retry')
    assert response.status_code == 200, response.text
    assert response.json()['queue'] == 'retry'
    return ident


def status(ident):
    with SessionLocal() as db:
        row = db.get(Job, ident)
        return row.status, row.error, row.payload


def factory_with_log(seen):
    def factory(platform, credentials=None):
        name = credentials['refresh_token'].split('-')[0]
        def fetch(ident, *, download=True, **kwargs):
            seen.append((name, download))
            novel = ident.startswith('novel:')
            return RemoteWork(remote_id=ident, title='Synthetic retry',
                source_url='https://www.pixiv.net/artworks/42', item_type='novel' if novel else 'manga',
                metadata_only=not download, is_bookmarked=False, remote_page_count=None if novel else 1,
                files=[('body.txt', b'synthetic')] if novel and download else [('page.png', png())] if download else [])
        return SimpleNamespace(normalize_id=lambda value: value, fetch=fetch)
    return factory


def test_permission_defaults_empty_and_legacy_client_preserves_it(client, roles):
    assert client.get(URL).json()['retry_account_ids'] == []
    assert configure(client, roles, retry_account_ids=[roles['retry']]).status_code == 200
    assert configure(client, roles).json()['retry_account_ids'] == [roles['retry']]
    assert configure(client, roles, retry_account_ids=['missing']).status_code == 400
    assert configure(client, roles, retry_account_ids=[roles['retry']] * 2).status_code == 400


def test_retry_download_executes_original_roles_intersected_with_permission(client, roles, monkeypatch):
    configure(client, roles, retry_account_ids=[roles['retry'], roles['details']])
    seen = []
    monkeypatch.setattr(jobs, 'get_connector', factory_with_log(seen))
    ident = retry(client)
    assert not jobs.run_once(('download',))
    assert jobs.run_once(('retry',))
    state, error, payload = status(ident)
    assert state == 'completed', error
    assert seen == [('details', False), ('retry', True)]
    assert payload['download_account_id'] == roles['retry']
    assert payload['details_account_id'] == roles['details']


@pytest.mark.parametrize('grants', [[], ['retry'], ['details']])
def test_missing_retry_permission_fails_before_any_connector(client, roles, monkeypatch, grants):
    configure(client, roles, retry_account_ids=[roles[key] for key in grants])
    ident = retry(client)
    monkeypatch.setattr(jobs, 'get_connector', lambda *a, **kw: pytest.fail('Unpermitted network identity'))
    assert jobs.run_once(('retry',))
    state, error, _ = status(ident)
    assert state == 'failed' and '重试权限' in error


def test_manual_novel_retry_needs_manual_and_retry_permissions(client, roles, monkeypatch):
    configure(client, roles, retry_account_ids=[roles['manual']])
    seen = []
    monkeypatch.setattr(jobs, 'get_connector', factory_with_log(seen))
    ident = retry(client, remote_id='novel:42', download_origin='manual')
    assert jobs.run_once(('retry',))
    assert status(ident)[0] == 'completed', status(ident)[1]
    assert seen == [('manual', True)]


def test_discovery_retry_requires_main_identity_not_an_eligible_auxiliary(client, roles, monkeypatch):
    configure(client, roles, retry_account_ids=[roles['checks']])
    ident = retry(client, kind='source_sync')
    seen = []
    def factory(platform, credentials=None):
        seen.append(credentials['refresh_token'])
        return SimpleNamespace(follows=lambda **kw: [])
    monkeypatch.setattr(jobs, 'get_connector', factory)
    assert jobs.run_once(('retry',))
    assert status(ident)[0] == 'failed' and not seen
    configure(client, roles, retry_account_ids=[roles['main']])
    assert client.post('/api/jobs/' + ident + '/retry').status_code == 200
    assert jobs.run_once(('retry',))
    assert status(ident)[0] == 'completed', status(ident)[1]
    assert seen == ['main-synthetic']


def test_profile_retry_does_not_require_unused_directory_owner_permission(client, roles, monkeypatch):
    from app import author_api
    configure(client, roles, retry_account_ids=[roles['checks']])
    seen = []
    def factory(platform, credentials=None):
        seen.append(credentials['refresh_token'])
        return SimpleNamespace(fetch_author=lambda *a, **kw: {'remote_id': '55', 'name': 'Synthetic',
            'description': '', 'statistics': {}, 'is_self': False, 'avatar': None})
    monkeypatch.setattr(author_api, 'get_connector', factory)
    ident = retry(client, kind='author_profile', account_id=roles['main'], target='55', identity='profile55')
    assert jobs.run_once(('retry',))
    assert status(ident)[0] == 'completed', status(ident)[1]
    assert seen == ['checks-synthetic']


def test_disabling_roles_does_not_bypass_retry_or_change_requested_identity(client, roles):
    configure(client, roles, enabled=False, retry_account_ids=[roles['retry']])
    with SessionLocal() as db, queue_control.request_context(lambda: None, 'retry'):
        with pytest.raises(ConnectorError, match='重试权限'):
            jobs.account_credentials(db, 'pixiv', roles['main'])
        assert jobs.account_credentials(db, 'pixiv', roles['retry'])['refresh_token'] == 'retry-synthetic'
    ident = retry(client, account_id=roles['main'])
    assert jobs.run_once(('retry',))
    assert status(ident)[0] == 'failed' and '重试权限' in status(ident)[1]


def test_retry_uses_shared_account_limit_and_skips_saturated_backlog(client, roles):
    configure(client, roles, retry_account_ids=[roles['retry'], roles['details'], roles['checks']],
        account_limits={roles['retry']: 1, roles['details']: 2, roles['checks']: 1})
    with SessionLocal() as db:
        db.add(Job(kind='import', status='running', worker_id='busy', lease_until=utcnow(),
                   payload={'platform': 'pixiv', 'download_account_id': roles['retry']}))
        for i in range(70):
            db.add(Job(kind='import', status='queued', payload={'platform': 'pixiv', 'remote_id': str(i), 'retry_queue': True}))
        later = Job(kind='author_profile', status='queued', payload={'target': '55', 'account_id': roles['main'], 'retry_queue': True})
        db.add(later); db.commit(); later_id = later.id
        control = queue_control.lock_control(db)
        state = pixiv_accounts.assignment_state(db)
        candidate = db.scalar(select(Job).where(Job.status == 'queued', pixiv_accounts.assignment_filter(state)).order_by(Job.created_at).limit(1))
        assert candidate.id == later_id
        assert pixiv_accounts.assign_download_account(db, candidate, control, state=state)
        assert candidate.payload['check_account_id'] == roles['checks']
        blocked = db.scalar(select(Job).where(Job.kind == 'import', Job.status == 'queued'))
        assert not pixiv_accounts.assign_download_account(db, blocked, control, state=state)


def test_revoking_retry_after_claim_prevents_execution(client, roles, monkeypatch):
    configure(client, roles, retry_account_ids=[roles['retry'], roles['details']])
    ident = retry(client)
    claim = jobs.claim_once(('retry',))
    assert claim and claim[0] == ident
    configure(client, roles, retry_account_ids=[])
    monkeypatch.setattr(jobs, 'get_connector', lambda *a, **kw: pytest.fail('Revoked identity'))
    jobs.execute_claim(*claim)
    assert status(ident)[0] == 'failed' and '重试权限' in status(ident)[1]
