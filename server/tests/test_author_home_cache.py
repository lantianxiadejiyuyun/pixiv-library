from datetime import datetime

from sqlalchemy import func, select

from app import queue_control
from app.database import SessionLocal
from app.integrations.credentials import encrypt_credentials
from app.models import AuthorProfile, Item, Job, SourceAccount, Subscription
from test_devices import bearer, bind
from test_library import client


def known_author(db, ident='10', *, kind='manga', count=3):
    db.add(Subscription(platform='pixiv', target_type='author', target=ident, label='Saved author', content_scope='all'))
    for index in range(count):
        db.add(Item(id=f'{ident}-{index:03}', source='pixiv', source_id=f'{ident}-{index}',
                    author_source_id=ident, author='Saved author', title=f'Synthetic {index}', type=kind,
                    created_at=datetime(2026, 1, 1)))


def test_cached_home_needs_no_platform_account_or_queue_lock(client, monkeypatch):
    with SessionLocal() as db:
        known_author(db)
        db.add(AuthorProfile(remote_id='10'))
        db.add_all([Job(kind='author_profile', payload={}, status='queued') for _ in range(101)])
        db.commit()
    def reject_lock(_db):
        raise AssertionError('A cached author read must not lock the acquisition queue')
    monkeypatch.setattr(queue_control, 'lock_control', reject_lock)
    response = client.post('/api/pixiv/authors/open', json={'url_or_id': '10', 'prefer_cached': True})
    assert response.status_code == 200
    data = response.json()
    assert data['cached'] and data['job'] is None
    assert data['profile']['name'] == 'Saved author' and data['category'] == 'manga'
    assert data['profile']['local_counts'] == {'manga': 3}
    assert data['feed']['total'] == 3 and data['feed']['local_only']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 101


def test_local_fallback_is_author_platform_type_scoped_and_paginated(client):
    with SessionLocal() as db:
        known_author(db, count=27)
        known_author(db, '20')
        db.add_all([Item(source='pixiv', author_source_id='10', type='novel', title='Novel'),
                    Item(source='local', author_source_id='10', type='manga', title='Other platform')])
        db.commit()
    pages = [client.get('/api/pixiv/authors/10/feed', params={'category': 'manga', 'page': page}).json()
             for page in (1, 2)]
    assert [item['id'] for page in pages for item in page['items']] == [f'10-{index:03}' for index in range(27)]
    assert all(page['total'] == 27 and page['local_only'] and not page['complete'] for page in pages)
    for section, privacy in [('bookmarks', 'public'), ('bookmarks', 'private'), ('works', 'private')]:
        data = client.get('/api/pixiv/authors/10/feed', params={'category': 'manga', 'section': section, 'restrict': privacy}).json()
        assert data['total'] == 0 and data['items'] == []


def test_paused_home_displays_local_novels_and_creates_no_tasks(client):
    with SessionLocal() as db:
        known_author(db, kind='novel')
        control = queue_control.lock_control(db)
        control.lanes = {'checks': {'paused': True, 'concurrency': 2}}
        db.commit()
    data = client.post('/api/pixiv/authors/open', json={'url_or_id': '10', 'prefer_cached': True}).json()
    assert data['refresh_paused'] and data['feed']['refresh_paused'] and data['category'] == 'novel'
    assert data['feed']['total'] == 3
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_explicit_category_and_bookmarks_never_switch_to_local_author_works(client):
    with SessionLocal() as db:
        known_author(db, kind='novel')
        db.commit()
    explicit = client.post('/api/pixiv/authors/open', json={'url_or_id': '10', 'prefer_cached': True, 'category': 'illustration'}).json()
    assert explicit['category'] == 'illustration' and explicit['feed']['items'] == []
    bookmarks = client.post('/api/pixiv/authors/open', json={'url_or_id': '10', 'prefer_cached': True, 'section': 'bookmarks'}).json()
    assert bookmarks['section'] == 'bookmarks' and bookmarks['feed']['items'] == []


def test_cached_me_only_uses_profiles_verified_for_selected_account(client):
    with SessionLocal() as db:
        accounts = [SourceAccount(id=ident, platform='pixiv', label=ident,
                                  credentials_encrypted=encrypt_credentials({'refresh_token': 'synthetic', 'user_id': '99'}))
                    for ident in ('a', 'b')]
        db.add_all(accounts)
        db.add_all([AuthorProfile(remote_id='10', name='Own author', statistics={'_owned_accounts': ['a']}, synced_at=datetime(2026, 1, 1)),
                    AuthorProfile(remote_id='20', name='Other author', statistics={'_owned_accounts': ['b']}, synced_at=datetime(2026, 1, 1))])
        db.commit()
    own = client.post('/api/pixiv/authors/open', json={'url_or_id': 'me', 'prefer_cached': True, 'account_id': 'a'}).json()
    assert own['remote_id'] == '10' and own['profile']['name'] == 'Own author'
    assert not any(key.startswith('_') for key in own['profile']['statistics'])
    with SessionLocal() as db:
        db.get(AuthorProfile, '10').statistics = {}
        db.commit()
    unknown = client.post('/api/pixiv/authors/open', json={'url_or_id': 'me', 'prefer_cached': True, 'account_id': 'a'}).json()
    assert unknown['remote_id'] == 'me' and unknown['profile'] is None and unknown['job'] is None


def test_cached_home_is_available_to_paired_readers_without_admin_access(client):
    with SessionLocal() as db:
        known_author(db)
        db.commit()
    token = bearer(bind(client))
    assert client.post('/api/pixiv/authors/open', json={'url_or_id': '10', 'prefer_cached': True}, headers=token).json()['feed']['total'] == 3
    assert client.get('/api/sources', headers=token).status_code == 403


def test_unknown_home_is_placeholder_not_fake_successful_remote_profile(client):
    data = client.post('/api/pixiv/authors/open', json={'url_or_id': '10', 'prefer_cached': True}).json()
    assert not data['cached'] and data['profile']['synced_at'] is None
    assert data['profile']['name'] == 'Pixiv 作者 10'
    assert data['feed']['items'] == [] and not data['feed']['complete']


def test_author_directory_uses_follow_name_for_pending_profiles_and_keeps_real_name(client):
    with SessionLocal() as db:
        known_author(db)
        db.add(AuthorProfile(remote_id='10'))
        db.commit()
    pending = client.get('/api/pixiv/authors', params={'q': 'Saved author'}).json()
    assert pending['items'] == [{'remote_id': '10', 'name': 'Saved author'}] and pending['total'] == 1
    with SessionLocal() as db:
        db.get(AuthorProfile, '10').name = 'Verified profile name'
        db.commit()
    assert client.get('/api/pixiv/authors', params={'q': 'Verified'}).json()['items'][0]['name'] == 'Verified profile name'
    assert client.get('/api/pixiv/authors', params={'q': 'Saved author'}).json()['total'] == 0
