import pytest
from app import jobs, pixiv_accounts, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError, RemoteWork
from app.integrations.credentials import encrypt_credentials
from app.models import AuthorProfile, Item, Job, NetworkSetting, SourceAccount, utcnow
from test_devices import bind, bearer
from test_library import client, png

URL = '/api/sources/pixiv/account-roles'


@pytest.fixture
def accounts(client):
    with SessionLocal() as db:
        main = SourceAccount(platform='pixiv', label='主账号', verified_at=utcnow(), credentials_encrypted=encrypt_credentials({'refresh_token': 'main-test', 'user_id': '10'}))
        content = SourceAccount(platform='pixiv', label='副账号', credentials_encrypted=encrypt_credentials({'refresh_token': 'content-test', 'user_id': '20'}))
        other = SourceAccount(platform='jm', label='其他平台', credentials_encrypted=encrypt_credentials({'AVS': 'other-test'}))
        db.add_all([main, content, other]); db.commit()
        return dict(main=main.id, content=content.id, other=other.id)


def enable(client, accounts):
    result = client.put(URL, json={'enabled': True, 'main_account_id': accounts['main'], 'content_account_id': accounts['content'],
        'detail_account_ids': [accounts['main']]})
    assert result.status_code == 200, result.text
    return result.json()


def test_settings_persist_encrypted_and_display_independent_statuses(client, accounts):
    assert client.get(URL).json()['enabled'] is False
    result = enable(client, accounts)
    assert result['main']['status'] == 'verified' and result['content']['status'] == 'unverified'
    assert result['main']['label'] == '主账号' and result['content']['label'] == '副账号'
    with SessionLocal() as db:
        row = db.get(NetworkSetting, pixiv_accounts.KEY)
        assert accounts['main'] not in row.value_encrypted and 'main-test' not in row.value_encrypted
    assert 'main-test' not in client.get(URL).text and 'content-test' not in client.get('/api/sources').text
    assert client.get(URL).json() == result


@pytest.mark.parametrize('kind', ['none', 'same', 'wrong_platform', 'deleted', 'same_user', 'same_token'])
def test_invalid_account_pairs_rejected(client, accounts, kind):
    main, content = accounts['main'], accounts['content']
    if kind == 'none': content = None
    if kind == 'same': content = main
    if kind == 'wrong_platform': content = accounts['other']
    if kind == 'deleted': content = 'deleted-account'
    if kind in ('same_user', 'same_token'):
        with SessionLocal() as db:
            db.get(SourceAccount, content).credentials_encrypted = encrypt_credentials({'refresh_token': 'content-test' if kind == 'same_user' else 'main-test', 'user_id': '10' if kind == 'same_user' else '20'})
            db.commit()
    assert client.put(URL, json={'enabled': True, 'main_account_id': main, 'content_account_id': content}).status_code == 400
    assert client.get(URL).json()['enabled'] is False


def test_disabled_preserves_legacy_selection_and_other_platforms(client, accounts):
    with SessionLocal() as db:
        assert jobs.account_credentials(db, 'pixiv', accounts['content'])['refresh_token'] == 'content-test'
    enable(client, accounts)
    with SessionLocal() as db:
        assert jobs.account_credentials(db, 'pixiv', accounts['content'])['refresh_token'] == 'main-test'
        assert jobs.account_credentials(db, 'pixiv', accounts['main'], purpose='content')['refresh_token'] == 'content-test'
        assert jobs.account_credentials(db, 'jm', accounts['other'], purpose='content')['AVS'] == 'other-test'
    assert client.put(URL, json={'enabled': False, 'main_account_id': accounts['main'], 'content_account_id': accounts['content']}).status_code == 200
    with SessionLocal() as db:
        assert jobs.account_credentials(db, 'pixiv', accounts['content'])['refresh_token'] == 'content-test'


def test_roles_require_admin_even_with_cookie_and_resist_csrf(client, accounts):
    device = bearer(bind(client))
    assert client.get(URL, headers=device).status_code == 403
    assert client.put(URL, headers=device, json={'enabled': False}).status_code == 403
    assert client.put(URL, headers={'Origin': 'https://evil.test'}, json={'enabled': False}).status_code == 403
    client.cookies.clear()
    assert client.get(URL).status_code == 401


class RoleConnector:
    def __init__(self, token, calls, fail): self.token, self.calls, self.fail = token, calls, fail
    def normalize_id(self, value): return value
    def fetch(self, ident, *, download=True, checkpoint=lambda value: None, include_preview=True):
        self.calls.append((self.token, download, ident))
        if download and self.fail:
            raise ConnectorError('副账号正文不可访问')
        novel = ident.startswith('novel:')
        return RemoteWork(remote_id=ident, title='主账号资料' if not download else '副账号返回的标题',
            item_type='novel' if novel else 'manga', source_url='https://www.pixiv.net/artworks/123',
            tags=['副账号标签'] if download else ['主账号标签'], rating='adult' if download else 'general',
            files=[('novel.txt', '小说正文'.encode())] if download and novel else [('page.png', png())] if download else [],
            metadata_only=not download)


@pytest.mark.parametrize('ident', ['123', 'novel:123'])
def test_real_worker_routes_metadata_to_main_and_body_to_secondary_preserving_tags(client, accounts, monkeypatch, ident):
    enable(client, accounts); jobs._stop.clear(); queue_control.update_settings({'request_interval_seconds': 0})
    calls = []
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: RoleConnector(credentials['refresh_token'], calls, False))
    with SessionLocal() as db:
        job = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': ident, 'account_id': accounts['content']})
        job_id = job.id
    assert jobs.run_once()
    metadata = client.get('/api/jobs/' + job_id).json()
    assert metadata['status'] == 'completed', metadata
    item_id = metadata['item_id']
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.kind == 'import_content').count() == 0
    assert calls == ([('main-test', True, ident)] if ident.startswith('novel:') else [('main-test', False, ident), ('content-test', True, ident)])
    item = client.get('/api/items/' + item_id).json()
    assert item['content_state'] == 'ready'
    # The synthetic connector returns alternate metadata when download=True.
    assert item['title'] == ('副账号返回的标题' if ident.startswith('novel:') else '主账号资料')
    assert item['rating'] == ('adult' if ident.startswith('novel:') else 'general')
    assert [tag['name'] for tag in item['tags']] == (['副账号标签'] if ident.startswith('novel:') else ['主账号标签'])


@pytest.mark.parametrize('failure', ['deleted', 'remote_error'])
def test_secondary_failure_never_uses_main_for_body(client, accounts, monkeypatch, failure):
    enable(client, accounts); jobs._stop.clear(); queue_control.update_settings({'request_interval_seconds': 0})
    calls = []
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: RoleConnector(credentials['refresh_token'], calls, True))
    with SessionLocal() as db:
        job_id = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123', 'account_id': accounts['main']}).id
    if failure == 'deleted':
        assert client.delete('/api/sources/pixiv/accounts/' + accounts['content']).status_code == 200
        assert client.get(URL).json()['content']['status'] == 'missing'
    assert jobs.run_once()
    job = client.get('/api/jobs/' + job_id).json()
    assert job['status'] == 'failed'
    assert client.get('/api/items').json()['total'] == 0
    assert not any(token == 'main-test' and download for token, download, _ in calls)
    if failure == 'deleted':
        disabled = client.put(URL, json={'enabled': False, 'main_account_id': accounts['main'], 'content_account_id': accounts['content']})
        assert disabled.status_code == 200 and disabled.json()['content_account_id'] is None


def test_main_role_controls_author_identity_without_relabeling_old_private_scopes(client, accounts):
    enable(client, accounts)
    from app.author_api import account, sync_profile
    with SessionLocal() as db:
        assert account(db, accounts['content']).id == accounts['main']
        db.add(AuthorProfile(remote_id='20', name='副账号主页', statistics={'_owned_accounts': [accounts['content']]})); db.commit()
        with pytest.raises(ConnectorError, match='主账号分工已更改'):
            sync_profile(db, {'account_id': accounts['content'], 'target': 'me'}, lambda value: None, lambda db: None)
    assert client.get('/api/pixiv/authors/20', params={'account_id': accounts['content']}).json()['can_read_private'] is False


def test_corrupt_config_fails_closed_and_can_be_replaced(client, accounts):
    with SessionLocal() as db:
        db.add(NetworkSetting(key=pixiv_accounts.KEY, value_encrypted='damaged')); db.commit()
        with pytest.raises(ConnectorError, match='分工配置无法读取'):
            jobs.account_credentials(db, 'pixiv')
    assert client.get(URL).status_code == 503
    assert enable(client, accounts)['enabled'] is True


def test_follow_sync_uses_main_and_binds_new_subscriptions_to_main(client, accounts, monkeypatch):
    enable(client, accounts)
    seen = []
    class Follows:
        def follows(self, checkpoint):
            return [{'target_type': 'author', 'target': '12345', 'label': '关注作者'}]
    def connector(platform, credentials):
        seen.append(credentials['refresh_token'])
        return Follows()
    monkeypatch.setattr(jobs, 'get_connector', connector)
    from app.models import Subscription
    with SessionLocal() as db:
        result = jobs._sync(db, {'platform': 'pixiv', 'account_id': accounts['content']}, lambda value: None, lambda db: None)
        assert result['added_subscriptions'] == 1
        assert db.query(Subscription).one().account_id == accounts['main']
    assert seen == ['main-test']
