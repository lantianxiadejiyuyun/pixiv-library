"""Credential purposes match discovery, author profiles, work text and images."""
from collections import Counter

import pytest
from sqlalchemy import select

from app import pixiv_accounts, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError
from app.integrations.credentials import encrypt_credentials
from app.models import Job, SourceAccount
from test_library import client
from test_pixiv_accounts import accounts, URL


@pytest.fixture
def roles(client, accounts):
    with SessionLocal() as db:
        details = SourceAccount(platform='pixiv', label='作品内容', credentials_encrypted=encrypt_credentials({'refresh_token': 'details-test', 'user_id': '31'}))
        author = SourceAccount(platform='pixiv', label='作者资料', credentials_encrypted=encrypt_credentials({'refresh_token': 'author-test', 'user_id': '32'}))
        db.add_all([details, author]); db.commit()
        identities = {**accounts, 'details': details.id, 'author': author.id}
    response = client.put(URL, json={'enabled': True, 'main_account_id': identities['main'],
        'content_account_ids': [identities['content']], 'detail_account_ids': [identities['details']],
        'check_account_ids': [identities['author']], 'account_limits': {ident: 1 for ident in identities.values() if ident != identities['other']}})
    assert response.status_code == 200, response.text
    return identities


@pytest.mark.parametrize(('kind', 'payload', 'expected'), [
    ('source_sync', {'platform': 'pixiv'}, {'check_account_id': 'main'}),
    ('source_sync', {'platform': 'pixiv', 'subscription_id': 'author-subscription'}, {'check_account_id': 'main'}),
    ('author_feed', {'feed_id': 'public-or-private-catalogue'}, {'check_account_id': 'main'}),
    ('series_sync', {'remote_id': '123'}, {'check_account_id': 'main'}),
    ('author_profile', {'target': 'me'}, {'check_account_id': 'main'}),
    ('author_profile', {'target': '123'}, {'check_account_id': 'author'}),
    ('import', {'platform': 'pixiv', 'remote_id': '123'}, {'download_account_id': 'content', 'details_account_id': 'details'}),
    ('import', {'platform': 'pixiv', 'remote_id': '123', 'download': False}, {'details_account_id': 'details'}),
    ('import', {'platform': 'pixiv', 'remote_id': 'novel:123'}, {'details_account_id': 'details'}),
    ('import', {'platform': 'pixiv', 'remote_id': 'https://www.pixiv.net/novel/show.php?id=123'}, {'details_account_id': 'details'}),
    ('import_content', {'platform': 'pixiv', 'remote_id': 'novel:123'}, {'details_account_id': 'details'}),
    ('import_content', {'platform': 'pixiv', 'remote_id': '123'}, {'download_account_id': 'content'}),
])
def test_assignment_matrix(client, roles, kind, payload, expected):
    with SessionLocal() as db:
        control = queue_control.lock_control(db)
        row = Job(kind=kind, payload=payload)
        assert pixiv_accounts.assign_download_account(db, row, control)
        assigned = {key: value for key, value in row.payload.items() if key.endswith('_account_id')}
        assert assigned == {key: roles[value] for key, value in expected.items()}


@pytest.mark.parametrize('purpose', ['details', 'checks'])
def test_empty_required_pool_never_uses_main(client, roles, purpose):
    field = 'detail_account_ids' if purpose == 'details' else 'check_account_ids'
    saved = client.get(URL).json()
    response = client.put(URL, json={'enabled': True, 'main_account_id': roles['main'],
        'content_account_ids': [roles['content']], 'detail_account_ids': saved['detail_account_ids'],
        'check_account_ids': saved['check_account_ids'], field: []})
    assert response.status_code == 200
    with SessionLocal() as db:
        with pytest.raises(ConnectorError, match='未配置或已移除'):
            pixiv_accounts.resolve_account_id(db, purpose=purpose)
        with pytest.raises(ConnectorError, match='账号功能已更改'):
            pixiv_accounts.resolve_account_id(db, roles['main'], purpose=purpose, pinned=True)


def test_explicit_main_role_selection_is_required_for_non_discovery(client, roles):
    with SessionLocal() as db:
        for purpose in ('details', 'checks'):
            with pytest.raises(ConnectorError, match='账号功能已更改'):
                pixiv_accounts.resolve_account_id(db, roles['main'], purpose=purpose, pinned=True)
    response = client.put(URL, json={'enabled': True, 'main_account_id': roles['main'],
        'content_account_ids': [roles['content']], 'detail_account_ids': [roles['main']], 'check_account_ids': [roles['main']]})
    assert response.status_code == 200
    with SessionLocal() as db:
        for purpose in ('details', 'checks'):
            assert pixiv_accounts.resolve_account_id(db, purpose=purpose) == roles['main']


def test_old_discovery_pin_is_not_reassigned_to_main(client, roles):
    with SessionLocal() as db:
        control = queue_control.lock_control(db)
        row = Job(kind='source_sync', payload={'platform': 'pixiv', 'subscription_id': 'old-subscription', 'check_account_id': roles['author']})
        assert pixiv_accounts.assign_download_account(db, row, control)
        assert row.payload['check_account_id'] == roles['author']
        with pytest.raises(ConnectorError, match='主账号分工已更改'):
            pixiv_accounts.resolve_account_id(db, row.payload['check_account_id'], purpose='metadata', pinned=True)


def test_legacy_novel_assignment_releases_image_identity_without_changing_text_pin(client, roles):
    with SessionLocal() as db:
        control = queue_control.lock_control(db)
        row = Job(kind='import', payload={'platform': 'pixiv', 'remote_id': 'novel:123',
            'download_account_id': roles['content'], 'details_account_id': roles['details']})
        assert pixiv_accounts.assign_download_account(db, row, control)
        assert 'download_account_id' not in row.payload
        assert row.payload['details_account_id'] == roles['details']


@pytest.mark.parametrize('kind', ['import', 'import_content'])
@pytest.mark.parametrize('remote_id', ['novel:123', 'https://www.pixiv.net/novel/show.php?id=123'])
def test_old_novel_image_pin_does_not_wait_for_busy_image_account(client, roles, kind, remote_id):
    with SessionLocal() as db:
        control = queue_control.lock_control(db)
        old_novel = Job(kind=kind, payload={'platform': 'pixiv', 'remote_id': remote_id,
            'download_account_id': roles['content'], 'details_account_id': roles['details']})
        image = Job(kind='import', payload={'platform': 'pixiv', 'remote_id': '456',
            'download_account_id': roles['content'], 'details_account_id': roles['details']})
        db.add_all([old_novel, image]); db.flush()
        state = (pixiv_accounts.read_config(db), Counter({roles['content']: 1}), set(roles.values()))
        accepted = set(db.scalars(select(Job.id).where(pixiv_accounts.assignment_filter(state))))
        assert old_novel.id in accepted and image.id not in accepted
        assert pixiv_accounts.assign_download_account(db, old_novel, control, state=state)
        assert old_novel.payload['details_account_id'] == roles['details']
        assert 'download_account_id' not in old_novel.payload


@pytest.mark.parametrize(('full_role', 'allowed'), [
    ('content', {'novel', 'novel_link', 'novel_content', 'metadata', 'discovery', 'author'}),
    ('details', {'image_content', 'discovery', 'author'}),
    ('author', {'image', 'image_content', 'novel', 'novel_link', 'novel_content', 'metadata', 'discovery'}),
    ('main', {'image', 'image_content', 'novel', 'novel_link', 'novel_content', 'metadata', 'author'}),
])
def test_sql_saturation_filter_keeps_unrelated_roles_available(client, roles, full_role, allowed):
    examples = {
        'image': ('import', {'platform': 'pixiv', 'remote_id': '123'}),
        'image_content': ('import_content', {'platform': 'pixiv', 'remote_id': '123'}),
        'novel': ('import', {'platform': 'pixiv', 'remote_id': 'novel:123'}),
        'novel_link': ('import', {'platform': 'pixiv', 'remote_id': 'https://www.pixiv.net/novel/show.php?id=123'}),
        'novel_content': ('import_content', {'platform': 'pixiv', 'remote_id': 'novel:123'}),
        'metadata': ('import', {'platform': 'pixiv', 'remote_id': '123', 'download': False}),
        'discovery': ('source_sync', {'platform': 'pixiv', 'subscription_id': 'author-subscription'}),
        'author': ('author_profile', {'target': '123'}),
    }
    with SessionLocal() as db:
        rows = {name: Job(kind=kind, payload=payload) for name, (kind, payload) in examples.items()}
        db.add_all(rows.values()); db.flush()
        state = (pixiv_accounts.read_config(db), Counter({roles[full_role]: 1}), set(roles.values()))
        accepted = set(db.scalars(select(Job.id).where(pixiv_accounts.assignment_filter(state))))
        assert {name for name, row in rows.items() if row.id in accepted} == allowed


def test_unconfigured_pool_is_claimable_for_visible_failure(client, roles):
    response = client.put(URL, json={'enabled': True, 'main_account_id': roles['main'],
        'content_account_ids': [roles['content']], 'detail_account_ids': [], 'check_account_ids': []})
    assert response.status_code == 200
    with SessionLocal() as db:
        control = queue_control.lock_control(db)
        rows = [Job(kind='import', payload={'platform': 'pixiv', 'remote_id': 'novel:123'}),
                Job(kind='author_profile', payload={'target': '123'})]
        db.add_all(rows); db.flush()
        state = pixiv_accounts.assignment_state(db)
        assert len(db.scalars(select(Job.id).where(pixiv_accounts.assignment_filter(state))).all()) == 2
        for row in rows:
            assert pixiv_accounts.assign_download_account(db, row, control, state=state)
