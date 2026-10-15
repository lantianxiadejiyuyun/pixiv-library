from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from test_library import client
from test_pixiv_accounts import URL, RoleConnector, accounts
from app import jobs, pixiv_accounts, queue_control
from app.database import SessionLocal
from app.integrations.credentials import encrypt_credentials
from app.integrations.base import ConnectorError
from app.models import Job, NetworkSetting, SourceAccount


def add_account(token='third-test', user='30'):
    with SessionLocal() as db:
        row = SourceAccount(platform='pixiv', label='另一个下载账号', credentials_encrypted=encrypt_credentials({'refresh_token': token, 'user_id': user}))
        db.add(row); db.commit()
        return row.id


def configure(client, accounts, pool):
    response = client.put(URL, json={'enabled': True, 'main_account_id': accounts['main'], 'content_account_ids': pool})
    assert response.status_code == 200, response.text
    return response.json()


def test_pool_has_no_account_count_quota_and_legacy_config_remains_readable(client, accounts):
    with SessionLocal() as db:
        db.add(NetworkSetting(key=pixiv_accounts.KEY, value_encrypted=encrypt_credentials({
            'enabled': True, 'main_account_id': accounts['main'], 'content_account_id': accounts['content']})))
        db.commit()
    assert client.get(URL).json()['content_account_ids'] == [accounts['content']]
    with SessionLocal() as db:
        rows = [SourceAccount(platform='pixiv', label=f'账号 {index}', credentials_encrypted=encrypt_credentials({'refresh_token': f'unique-{index}', 'user_id': str(1000+index)})) for index in range(70)]
        db.add_all(rows); db.commit()
        identifiers = [row.id for row in rows]
    result = configure(client, accounts, [accounts['content'], *identifiers])
    assert len(result['contents']) == 71
    assert result['content_account_id'] == accounts['content']
    assert client.get(URL).json()['content_account_ids'] == [accounts['content'], *identifiers]
    assert 'unique-69' not in client.get(URL).text


@pytest.mark.parametrize('problem', ['duplicate_id', 'main', 'duplicate_user', 'duplicate_token', 'wrong_platform', 'missing'])
def test_invalid_pool_is_rejected_without_changing_saved_config(client, accounts, problem):
    original = configure(client, accounts, [accounts['content']])
    if problem == 'duplicate_id': extra = accounts['content']
    elif problem == 'main': extra = accounts['main']
    elif problem == 'duplicate_user': extra = add_account(user='20')
    elif problem == 'duplicate_token': extra = add_account(token='content-test')
    elif problem == 'wrong_platform': extra = accounts['other']
    else: extra = 'missing'
    response = client.put(URL, json={'enabled': True, 'main_account_id': accounts['main'], 'content_account_ids': [accounts['content'], extra]})
    assert response.status_code == 400
    after = client.get(URL).json()
    assert after['content_account_ids'] == original['content_account_ids']


def test_parallel_downloads_use_distinct_secondary_accounts_and_same_main(client, accounts, monkeypatch):
    third = add_account()
    configure(client, accounts, [accounts['content'], third])
    queue_control.update_settings({'concurrency': 2, 'rate_limit_enabled': False})
    jobs._stop.clear()
    barrier, calls = Barrier(2), []
    class Connector(RoleConnector):
        def fetch(self, *args, **kwargs):
            if kwargs.get('download', True):
                barrier.wait(timeout=8)
            return super().fetch(*args, **kwargs)
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: Connector(credentials['refresh_token'], calls, False))
    with SessionLocal() as db:
        identifiers = [jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': ident}).id for ident in ('123', 'novel:124')]
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert all(executor.map(lambda _: jobs.run_once(), range(2)))
    with SessionLocal() as db:
        rows = [db.get(Job, ident) for ident in identifiers]
        assert all(row.status == 'completed' for row in rows), [row.error for row in rows]
        assert {row.payload['download_account_id'] for row in rows} == {accounts['content'], third}
    assert [token for token, download, _ in calls if not download] == ['main-test', 'main-test']
    assert sorted(token for token, download, _ in calls if download) == ['content-test', 'third-test']
    status = client.get(URL).json()
    assert all(row['running'] == 0 for row in status['download_accounts'])
    assert sorted(row['completed'] for row in status['download_accounts'] if row['in_pool']) == [1, 1]


def test_assignment_survives_pause_and_explicit_retry_can_reassign_after_pool_change(client, accounts, monkeypatch):
    third = add_account()
    configure(client, accounts, [accounts['content'], third])
    jobs._stop.clear(); queue_control.update_settings({'rate_limit_enabled': False})
    calls = []
    class Pause(RoleConnector):
        def fetch(self, *args, **kwargs):
            if kwargs.get('download', True):
                queue_control.update_settings({'paused': True})
                kwargs['checkpoint'](50)
            return super().fetch(*args, **kwargs)
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: Pause(credentials['refresh_token'], calls, False))
    with SessionLocal() as db:
        identifier = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123'}).id
    assert jobs.run_once()
    with SessionLocal() as db:
        row = db.get(Job, identifier)
        assert row.status == 'queued'
        assert row.payload['download_account_id'] == accounts['content']
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: RoleConnector(credentials['refresh_token'], calls, True))
    queue_control.update_settings({'paused': False})
    assert jobs.run_once()
    with SessionLocal() as db:
        row = db.get(Job, identifier)
        assert row.status == 'failed' and row.payload['download_account_id'] == accounts['content']
    assert not any(token == 'third-test' for token, _, _ in calls)
    configure(client, accounts, [third])
    assert client.post(f'/api/jobs/{identifier}/retry').status_code == 200
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: RoleConnector(credentials['refresh_token'], calls, False))
    assert jobs.run_once()
    with SessionLocal() as db:
        row = db.get(Job, identifier)
        assert row.status == 'completed' and row.payload['download_account_id'] == third


def test_removed_account_is_absent_from_management_and_new_assignments_use_remaining_pool(client, accounts, monkeypatch):
    third = add_account()
    configure(client, accounts, [accounts['content'], third])
    client.delete(f"/api/sources/pixiv/accounts/{accounts['content']}").raise_for_status()
    result = client.get(URL).json()
    assert accounts['content'] not in [row['id'] for row in result['download_accounts']]
    assert result['missing_account_ids'] == [accounts['content']]
    assert result['content']['status'] == 'missing'
    calls = []
    jobs._stop.clear(); queue_control.update_settings({'rate_limit_enabled': False})
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: RoleConnector(credentials['refresh_token'], calls, False))
    with SessionLocal() as db:
        identifier = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123'}).id
    assert jobs.run_once()
    with SessionLocal() as db:
        assert db.get(Job, identifier).payload['download_account_id'] == third
        assert db.get(Job, identifier).status == 'completed'


def test_deleted_main_and_historical_jobs_do_not_reappear_as_managed_accounts(client, accounts):
    configure(client, accounts, [accounts['content']])
    with SessionLocal() as db:
        db.add(Job(kind='import', status='failed', payload={'platform': 'pixiv',
            'download_account_id': accounts['content'], 'details_account_id': accounts['main']}))
        db.commit()
    for key in ('main', 'content'):
        client.delete(f"/api/sources/pixiv/accounts/{accounts[key]}").raise_for_status()
    result = client.get(URL).json()
    assert result['download_accounts'] == []
    assert set(result['missing_account_ids']) == {accounts['main'], accounts['content']}
    assert result['enabled'] is True  # Deletion cannot implicitly disable role protection.
    assert result['main']['status'] == result['content']['status'] == 'missing'
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.status == 'failed').count() == 1
        with pytest.raises(ConnectorError, match='未配置或已移除'):
            pixiv_accounts.resolve_account_id(db, purpose='content')


def test_corrupt_roles_fail_the_job_instead_of_stalling_all_claims(client, accounts):
    with SessionLocal() as db:
        db.add(NetworkSetting(key=pixiv_accounts.KEY, value_encrypted='damaged'))
        db.commit()
        identifier = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123'}).id
    jobs._stop.clear()
    assert jobs.run_once()
    with SessionLocal() as db:
        assert db.get(Job, identifier).status == 'failed'
        assert '配置无法读取' in db.get(Job, identifier).error
