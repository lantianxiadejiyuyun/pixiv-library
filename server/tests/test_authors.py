from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import author_api, jobs
from app.database import SessionLocal
from app.integrations.base import ConnectorError
from app.integrations.credentials import encrypt_credentials
from app.integrations.pixiv import PixivConnector
from app.models import AuthorFeed, AuthorFeedEntry, AuthorProfile, Item, ItemTag, Job, SourceAccount, Subscription, utcnow
from test_devices import bind, bearer
from test_library import client


def work(ident, kind='illust', author='10'):
    return {'id': ident, 'title': 'Work ' + str(ident), 'type': kind, 'page_count': 1,
            'user': {'id': author, 'name': 'Author ' + author}, 'tags': [{'name': 'sample'}]}


class FakePixiv:
    def __init__(self):
        self.calls = []
        self.response = None
    def user_detail(self, ident):
        self.calls.append(('profile', ident))
        return {'user': {'id': ident, 'name': 'Author ' + ident, 'comment': '<b>Profile</b>'},
                'profile': {'total_novels': 2, 'total_manga': 1}}
    def user_illusts(self, ident, type, **kwargs):
        self.calls.append(('works', type, kwargs))
        return self.response or {'illusts': [work(11, type, ident)], 'next_url': None}
    def user_novels(self, ident, **kwargs):
        self.calls.append(('novels', kwargs))
        return self.response or {'novels': [work(21, author=ident)], 'next_url': None}
    def user_bookmarks_illust(self, ident, restrict, **kwargs):
        self.calls.append(('bookmarks', restrict, kwargs))
        return self.response or {'illusts': [work(31, 'illust', '20'), work(32, 'manga', '30')], 'next_url': None}
    def user_bookmarks_novel(self, ident, restrict, **kwargs):
        self.calls.append(('novel_bookmarks', restrict, kwargs))
        return self.response or {'novels': [work(41, author='40')], 'next_url': None}


@pytest.fixture
def pixiv(client, monkeypatch):
    api = FakePixiv()
    def connected(self):
        self._user_id = '10'
        self._authenticated_user_id = '10'
        return api
    monkeypatch.setattr(PixivConnector, '_api', connected)
    with SessionLocal() as db:
        account = SourceAccount(platform='pixiv', label='Synthetic Pixiv', credentials_encrypted=encrypt_credentials({'refresh_token': 'synthetic'}))
        db.add(account); db.commit()
        api.account_id = account.id
    return api


def run_job(ident):
    jobs._stop.clear()
    with SessionLocal() as db:
        job = db.get(Job, ident)
        job.status, job.worker_id, job.lease_until = 'running', jobs._worker_id, utcnow() + timedelta(seconds=300)
        db.commit()
    jobs.process_job(ident)
    with SessionLocal() as db:
        job = db.get(Job, ident)
        assert job.status == 'completed', job.error
        return dict(job.result)


def profile(client, target='10', headers=None):
    result = client.post('/api/pixiv/authors/open', json={'url_or_id': target}, headers=headers)
    assert result.status_code == 200, result.text
    run_job(result.json()['job']['id'])
    return result.json()


@pytest.mark.parametrize('suffix,section,category,restrict', [
    ('', 'works', 'illustration', 'public'), ('/novels', 'works', 'novel', 'public'),
    ('/manga', 'works', 'manga', 'public'), ('/bookmarks/artworks?rest=show&mode=all', 'bookmarks', 'illustration', 'public'),
    ('/bookmarks/artworks?rest=hide&mode=manga', 'bookmarks', 'manga', 'private'),
    ('/bookmarks/novels', 'bookmarks', 'novel', 'public'),
])
def test_author_url_parsing(suffix, section, category, restrict):
    assert PixivConnector.normalize_author('https://www.pixiv.net/users/10' + suffix) == {
        'remote_id': '10', 'section': section, 'category': category, 'restrict': restrict}


@pytest.mark.parametrize('url', ['https://pixiv.net.evil.test/users/10', 'https://user:pass@www.pixiv.net/users/10',
    'https://www.pixiv.net/users/10?redirect=evil', 'https://www.pixiv.net/users/0', 'file:///users/10'])
def test_author_url_rejection(url):
    with pytest.raises(ConnectorError):
        PixivConnector.normalize_author(url)


def test_profile_me_and_directory_include_existing_followed_authors(client, pixiv):
    with SessionLocal() as db:
        db.add(Subscription(platform='pixiv', target_type='author', target='20', label='Existing follow', enabled=False))
        db.commit()
    assert client.get('/api/pixiv/authors').json()['total'] == 1
    opened = profile(client, 'me')
    state = client.get('/api/pixiv/tasks/' + opened['job']['id']).json()
    assert state['author_id'] == '10'
    author = client.get('/api/pixiv/authors/10').json()
    assert author['name'] == 'Author 10' and author['description'] == 'Profile'
    assert author['can_read_private'] and '_owned_accounts' not in author['statistics']
    assert client.get('/api/pixiv/authors', params={'q': 'Author 10'}).json()['total'] == 1


@pytest.mark.parametrize('section,category,expected', [
    ('works', 'illustration', '11'), ('works', 'manga', '11'), ('works', 'novel', 'novel:21'),
    ('bookmarks', 'illustration', '31'), ('bookmarks', 'manga', '32'), ('bookmarks', 'novel', 'novel:41'),
])
def test_six_catalogues_save_metadata_and_only_queue_covers(client, pixiv, section, category, expected):
    profile(client)
    options = {'section': section, 'category': category}
    response = client.post('/api/pixiv/authors/10/feed', json=options)
    duplicate = client.post('/api/pixiv/authors/10/feed', json=options)
    assert response.json()['job']['id'] == duplicate.json()['job']['id']
    run_job(response.json()['job']['id'])
    result = client.get('/api/pixiv/authors/10/feed', params=options).json()
    assert result['complete'] and not result['has_more'] and result['total'] == 1
    with SessionLocal() as db:
        item = db.get(Item, result['items'][0]['id'])
        assert item.source_id == expected and item.author_source_id
        assert not item.assets
        queued = list(db.scalars(select(Job).where(Job.kind == 'import')))
        assert len(queued) == 1 and queued[0].payload['download'] is False
        assert not list(db.scalars(select(Job).where(Job.kind == 'import_content')))


def test_private_bookmarks_are_enforced_against_authenticated_owner(client, pixiv):
    profile(client)
    profile(client, '20')
    denied = client.post('/api/pixiv/authors/20/feed', json={'section': 'bookmarks', 'restrict': 'private'})
    assert denied.status_code == 403
    own = client.post('/api/pixiv/authors/10/feed', json={'section': 'bookmarks', 'restrict': 'private'})
    assert own.status_code == 200
    run_job(own.json()['job']['id'])
    connector = PixivConnector({'user_id': '20'})
    with pytest.raises(ConnectorError, match='自己的私密'):
        connector.fetch_author_page('20', 'bookmarks', 'novel', 'private')


@pytest.mark.parametrize('url', [
    'https://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=20&max_bookmark_id=50',
    'https://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=10&max_bookmark_id=50&restrict=private',
    'https://app-api.pixiv.net/v1/user/bookmarks/novel?user_id=10&max_bookmark_id=50',
    'https://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=10&max_bookmark_id=50&url=evil',
])
def test_pagination_never_switches_author_endpoint_or_privacy(url):
    with pytest.raises(ConnectorError):
        PixivConnector.author_cursor(url, '10', 'bookmarks', 'illustration', 'public')


def test_mixed_bookmark_pages_can_be_empty_without_finishing_or_losing_cursor(client, pixiv):
    profile(client)
    def only_manga(ident, restrict, **kwargs):
        number = int(kwargs.get('max_bookmark_id', '100')) - 1
        return {'illusts': [work(1000 + number, 'manga')],
                'next_url': f'https://app-api.pixiv.net/v1/user/bookmarks/illust?user_id=10&restrict=public&max_bookmark_id={number}'}
    pixiv.user_bookmarks_illust = only_manga
    response = client.post('/api/pixiv/authors/10/feed', json={'section': 'bookmarks'})
    run_job(response.json()['job']['id'])
    feed = client.get('/api/pixiv/authors/10/feed', params={'section': 'bookmarks'}).json()
    assert feed['total'] == 0 and feed['has_more'] and not feed['complete']
    with SessionLocal() as db:
        cached = db.scalar(select(AuthorFeed))
        assert cached.next_cursor == {'max_bookmark_id': '97'} and cached.raw_count == 3


def test_partial_refresh_retains_old_tail_then_complete_scan_removes_only_membership(client, pixiv):
    profile(client)
    options = {'section': 'works', 'category': 'illustration'}
    first = client.post('/api/pixiv/authors/10/feed', json=options).json()
    run_job(first['job']['id'])
    pixiv.response = {'illusts': [work(12)], 'next_url': 'https://app-api.pixiv.net/v1/user/illusts?user_id=10&type=illust&offset=30'}
    refresh = client.post('/api/pixiv/authors/10/feed', json=options).json()
    run_job(refresh['job']['id'])
    result = client.get('/api/pixiv/authors/10/feed', params=options).json()
    assert result['total'] == 2 and not result['complete']
    pixiv.response = {'illusts': [work(13)], 'next_url': None}
    more = client.post('/api/pixiv/authors/10/feed', json={**options, 'more': True}).json()
    run_job(more['job']['id'])
    result = client.get('/api/pixiv/authors/10/feed', params=options).json()
    assert result['total'] == 2 and result['complete']
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Item)))) == 3


def test_device_can_open_author_catalogues_but_no_unrelated_job_payloads(client, pixiv):
    device = bind(client)
    headers = bearer(device)
    opened = profile(client, headers=headers)
    assert client.get('/api/pixiv/authors', headers=headers).status_code == 200
    assert client.get('/api/pixiv/authors/10', headers=headers).status_code == 200
    assert client.post('/api/pixiv/authors/10/feed', json={}, headers=headers).status_code == 200
    with SessionLocal() as db:
        job = Job(kind='import', status='queued', payload={'secret': 'never-visible'})
        db.add(job); db.commit(); ident = job.id
    assert client.get('/api/pixiv/tasks/' + ident, headers=headers).status_code == 404
    assert 'synthetic' not in client.get('/api/pixiv/tasks/' + opened['job']['id'], headers=headers).text
    assert client.get('/api/sources', headers=headers).status_code == 403


def test_duplicate_remote_members_do_not_duplicate_cover_jobs_or_count(client, pixiv):
    profile(client)
    pixiv.response = {'illusts': [work(11), work(11)], 'next_url': None}
    response = client.post('/api/pixiv/authors/10/feed', json={}).json()
    result = run_job(response['job']['id'])
    assert result['queued_covers'] == 1
    assert client.get('/api/pixiv/authors/10/feed').json()['total'] == 1


def test_retry_reuses_active_catalogue_job_and_feed_view_prefers_it(client, pixiv):
    profile(client)
    first = client.post('/api/pixiv/authors/10/feed', json={}).json()['job']['id']
    with SessionLocal() as db:
        db.get(Job, first).status = 'failed'
        db.commit()
    second = client.post('/api/pixiv/authors/10/feed', json={}).json()['job']['id']
    retry = client.post('/api/jobs/' + first + '/retry')
    assert retry.json()['id'] == second
    assert client.get('/api/pixiv/authors/10/feed').json()['job']['id'] == second
