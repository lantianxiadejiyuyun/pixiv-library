"""Exercise claimed jobs through the real executors with distinct identities."""
from datetime import timedelta

import pytest

from app import author_api, jobs, queue_control
from app.database import SessionLocal
from app.integrations.base import RemoteWork
from app.integrations.credentials import encrypt_credentials
from app.models import AuthorFeed, AuthorProfile, Item, Job, SourceAccount, Subscription, utcnow
from test_library import client, png
from test_pixiv_accounts import accounts, URL


@pytest.fixture
def routed(client, accounts, monkeypatch):
    with SessionLocal() as db:
        for role, uid in [('details', '30'), ('authors', '40')]:
            row = SourceAccount(platform='pixiv', label=role,
                credentials_encrypted=encrypt_credentials({'refresh_token': role, 'user_id': uid}))
            db.add(row); db.flush(); accounts[role] = row.id
        db.commit()
    client.put(URL, json={'enabled': True, 'main_account_id': accounts['main'],
        'content_account_ids': [accounts['content']], 'detail_account_ids': [accounts['details']],
        'check_account_ids': [accounts['authors']]}).raise_for_status()
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    seen = []

    class Connector:
        def __init__(self, credentials): self.token = credentials['refresh_token']
        def normalize_id(self, value): return value
        def fetch(self, ident, *, download=True, **kwargs):
            seen.append((self.token, 'body' if download else 'details'))
            novel = ident.startswith('novel:')
            return RemoteWork(remote_id=ident, title='Synthetic work', author='Synthetic author',
                item_type='novel' if novel else 'manga', source_url='https://example.invalid/work',
                metadata_only=not download, remote_page_count=1,
                files=[('body.txt', b'Novel text')] if novel and download else [('page.png', png())] if download else [])
        def follows(self, **kwargs):
            seen.append((self.token, 'follows')); return []
        def discover(self, *args, **kwargs):
            seen.append((self.token, 'discover')); return [], False
        def fetch_author(self, target, **kwargs):
            seen.append((self.token, 'profile'))
            return {'remote_id': '10' if target == 'me' else target, 'name': 'Synthetic author',
                'description': '', 'statistics': {}, 'is_self': target == 'me'}
        def fetch_author_page(self, *args, **kwargs):
            seen.append((self.token, 'feed'))
            return {'works': [], 'raw_count': 0, 'unavailable': 0, 'next_cursor': None}
        def fetch_series(self, ident, *, kind, **kwargs):
            seen.append((self.token, 'series'))
            return {'remote_id': ident, 'kind': kind, 'title': 'Synthetic series', 'works': [], 'complete': True}

    factory = lambda platform, credentials: Connector(credentials)
    monkeypatch.setattr(jobs, 'get_connector', factory)
    monkeypatch.setattr(author_api, 'get_connector', factory)
    return accounts, seen


def run(kind, payload):
    with SessionLocal() as db:
        ident = jobs.enqueue(db, kind, payload).id
    assert jobs.run_once()
    with SessionLocal() as db:
        row = db.get(Job, ident)
        assert row.status == 'completed', row.error
        return dict(row.payload)


@pytest.mark.parametrize('novel', [False, True])
@pytest.mark.parametrize('legacy', [False, True])
def test_image_and_novel_bodies_use_their_own_role(routed, novel, legacy):
    accounts, seen = routed
    payload = {'platform': 'pixiv', 'remote_id': 'novel:123' if novel else '123', 'account_id': accounts['main']}
    if legacy:
        with SessionLocal() as db:
            item = Item(source='pixiv', source_id=payload['remote_id'], title='Existing', type='novel' if novel else 'manga')
            db.add(item); db.commit(); payload['item_id'] = item.id
    assigned = run('import_content' if legacy else 'import', payload)
    if novel:
        assert seen == [('details', 'body')]
        assert assigned['details_account_id'] == accounts['details']
        assert 'download_account_id' not in assigned
    else:
        assert seen == ([] if legacy else [('details', 'details')]) + [('content-test', 'body')]
        assert assigned['download_account_id'] == accounts['content']


@pytest.mark.parametrize('operation', ['follows', 'subscription', 'public_feed', 'private_feed', 'series'])
def test_all_discovery_requests_really_use_main(routed, operation):
    accounts, seen = routed
    payload = {'platform': 'pixiv', 'account_id': accounts['main']}
    kind = 'source_sync'
    expected = 'follows'
    if operation == 'subscription':
        with SessionLocal() as db:
            sub = Subscription(platform='pixiv', target_type='author', target='123', label='Synthetic',
                enabled=True, next_check_at=utcnow() + timedelta(hours=1))
            db.add(sub); db.commit(); payload['subscription_id'] = sub.id
        expected = 'discover'
    elif operation.endswith('_feed'):
        with SessionLocal() as db:
            db.add(AuthorProfile(remote_id='123', name='Synthetic')); db.flush()
            feed = AuthorFeed(scope_key='synthetic', author_id='123', account_id=accounts['main'],
                section='bookmarks', category='novel', restrict='private' if operation == 'private_feed' else 'public')
            db.add(feed); db.commit()
            payload.update(feed_id=feed.id, revision=feed.revision, more=False)
        kind, expected = 'author_feed', 'feed'
    elif operation == 'series':
        payload.update(remote_id='123', series_kind='novel')
        kind, expected = 'series_sync', 'series'
    assigned = run(kind, payload)
    assert seen == [('main-test', expected)]
    assert assigned['check_account_id'] == accounts['main']


@pytest.mark.parametrize('target,token', [('123', 'authors'), ('me', 'main-test')])
def test_profiles_use_author_pool_except_main_identity_lookup(routed, target, token):
    accounts, seen = routed
    run('author_profile', {'account_id': accounts['main'], 'target': target})
    assert seen == [(token, 'profile')]
    with SessionLocal() as db:
        profile = db.get(AuthorProfile, '10' if target == 'me' else '123')
        assert profile.statistics['_owned_accounts'] == ([accounts['main']] if target == 'me' else [])
