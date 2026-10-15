"""Independent lane limits plus one shared budget per Pixiv identity."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from app import jobs, queue_control
from app.database import SessionLocal
from app.integrations.credentials import encrypt_credentials
from app.models import AuthorFeed, AuthorProfile, Job, SourceAccount, Subscription, utcnow
from app.integrations.base import RemoteWork
from test_library import client, png
from test_pixiv_accounts import accounts, enable, URL


def hold(monkeypatch):
    monkeypatch.setattr(jobs, "process_job", lambda _: None)


def add(kind, payload):
    with SessionLocal() as db:
        return jobs.enqueue(db, kind, payload).id


def test_lane_classification_and_api_filter_match(client):
    examples = [("import", {"platform": "pixiv"}, "download"),
                ("import", {"platform": "pixiv", "download": False}, "details"),
                ("import", {}, "details"), ("import_content", {}, "download"),
                ("source_sync", {}, "checks"), ("series_sync", {}, "checks"),
                ("author_profile", {}, "checks"), ("author_feed", {}, "checks"),
                ("ai", {}, "ai"), ("translate", {}, "translation"), ("unknown", {}, "other")]
    for kind, payload, lane in examples:
        ident = add(kind, payload)
        assert client.get('/api/jobs/'+ident).json()['queue'] == lane
        assert ident in [job['id'] for job in client.get('/api/jobs', params={'queue': lane}).json()['items']]
    lanes = {lane['id']: lane for lane in client.get('/api/queue/status').json()['lanes']}
    assert lanes['checks']['counts']['queued'] == 4
    assert lanes['details']['counts']['queued'] == 2
    assert client.get('/api/jobs?queue=missing').status_code == 422


def test_each_lane_runs_independently_and_honors_master_pause(client, monkeypatch):
    hold(monkeypatch)
    add('import_content', {'platform': 'jm'})
    add('import_content', {'platform': 'jm'})
    add('import', {'platform': 'jm'})
    add('source_sync', {'platform': 'jm'})
    add('ai', {})
    add('translate', {})
    assert all(jobs.run_once() for _ in range(5))
    assert not jobs.run_once()
    queue_control.update_settings({'paused': True})
    add('ai', {})
    queue_control.update_lane('ai', {'concurrency': 2})
    assert jobs.run_once()  # AI unaffected by the network master pause.
    lanes = {lane['id']: lane for lane in client.get('/api/queue/status').json()['lanes']}
    assert lanes['download']['counts']['running'] == 1 and lanes['download']['blocked_by_master']
    assert lanes['translation']['counts']['running'] == 1


@pytest.mark.parametrize('value', [0, 9, 1.5, True, '3'])
def test_lane_validation_does_not_modify_master_or_other_lanes(client, value):
    original = client.get('/api/queue/status').json()
    assert client.put('/api/queue/lanes/download', json={'concurrency': value}).status_code == 422
    assert client.get('/api/queue/status').json() == original


def test_live_limit_drains_and_pause_only_blocks_one_lane(client, monkeypatch):
    hold(monkeypatch)
    for _ in range(4): add('import_content', {'platform': 'jm'})
    assert client.put('/api/queue/lanes/download', json={'concurrency': 2}).status_code == 200
    assert jobs.run_once() and jobs.run_once()
    client.put('/api/queue/lanes/download', json={'concurrency': 1, 'paused': True})
    assert not jobs.run_once()
    add('ai', {})
    assert jobs.run_once()
    with SessionLocal() as db:
        for row in db.scalars(select(Job).where(Job.kind == 'import_content', Job.status == 'running')):
            row.status = 'completed'
        db.commit()
    assert not jobs.run_once()
    client.put('/api/queue/lanes/download', json={'paused': False})
    assert jobs.run_once() and not jobs.run_once()


def configure(client, accounts, **changes):
    response = client.put(URL, json={'enabled': True, 'main_account_id': accounts['main'],
        'content_account_ids': [accounts['content']], 'detail_account_ids': [accounts['content']],
        'check_account_ids': [accounts['content']], 'account_limits': {accounts['main']: 4, accounts['content']: 2}, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def test_one_account_budget_shared_across_roles_and_live_limit_drain(client, accounts, monkeypatch):
    hold(monkeypatch)
    configure(client, accounts)
    queue_control.update_settings({'concurrency': 4})
    first = add('import', {'platform': 'pixiv', 'remote_id': '10'})
    second = add('source_sync', {'platform': 'pixiv', 'subscription_id': 'synthetic'})
    third = add('import', {'platform': 'pixiv', 'remote_id': '11', 'download': False})
    assert jobs.run_once()
    with SessionLocal() as db:
        queue_control.lock_control(db).cover_streak = 4; db.commit()
    assert jobs.run_once() and not jobs.run_once()
    state = client.get(URL).json()
    account = next(row for row in state['download_accounts'] if row['id'] == accounts['content'])
    assert account['running'] == 2  # Combined job holds two roles, one slot.
    with SessionLocal() as db:
        payload = db.get(Job, first).payload
        assert payload['download_account_id'] == payload['details_account_id'] == accounts['content']
        assert db.get(Job, second).payload['check_account_id'] == accounts['content']
    configure(client, accounts, account_limits={accounts['main']: 4, accounts['content']: 1})
    assert not jobs.run_once()
    with SessionLocal() as db:
        db.get(Job, first).status = 'completed'; db.commit()
    assert not jobs.run_once()
    with SessionLocal() as db:
        db.get(Job, second).status = 'completed'; db.commit()
    assert jobs.run_once()
    assert client.get('/api/jobs/'+third).json()['status'] == 'running'


def test_parallel_claims_obey_account_and_queue_limits(client, accounts, monkeypatch):
    hold(monkeypatch)
    configure(client, accounts)
    queue_control.update_settings({'concurrency': 3})
    for _ in range(8): add('import', {'platform': 'pixiv', 'remote_id': '999'})
    barrier = Barrier(6)
    def run():
        barrier.wait(timeout=5)
        return jobs.run_once()
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(lambda _: run(), range(6))) == 2
    # Raise the identity budget, keep the queue budget at three.
    configure(client, accounts, account_limits={accounts['main']: 4, accounts['content']: 5})
    assert jobs.run_once() and not jobs.run_once()


def test_private_and_follows_use_main_public_checks_use_pool(client, accounts, monkeypatch):
    hold(monkeypatch)
    configure(client, accounts)
    queue_control.update_settings({'concurrency': 8})
    follows = add('source_sync', {'platform': 'pixiv', 'account_id': accounts['main']})
    public = add('author_profile', {'target': '12345', 'account_id': accounts['main']})
    own = add('author_profile', {'target': 'me', 'account_id': accounts['main']})
    with SessionLocal() as db:
        db.add(AuthorProfile(remote_id='10', name='主账号')); db.commit()
        feed = AuthorFeed(scope_key='synthetic-private', author_id='10', account_id=accounts['main'], section='bookmarks', category='novel', restrict='private')
        db.add(feed); db.commit(); feed_id = feed.id
    private = add('author_feed', {'feed_id': feed_id, 'account_id': accounts['main']})
    assert all(jobs.run_once() for _ in range(4))
    with SessionLocal() as db:
        assert db.get(Job, public).payload['check_account_id'] == accounts['content']
        for ident in (follows, own, private):
            assert db.get(Job, ident).payload['check_account_id'] == accounts['main']


def test_limits_validate_and_roles_can_be_multiselected_without_downloads(client, accounts):
    result = configure(client, accounts, content_account_ids=[])
    assert result['detail_account_ids'] == result['check_account_ids'] == [accounts['content']]
    assert client.put(URL, json={'enabled': False, 'account_limits': {accounts['main']: 0}}).status_code == 422
    assert client.get(URL).json() == result


@pytest.mark.parametrize('kind', ['import', 'source_sync'])
def test_full_pixiv_account_does_not_hide_other_platform_after_large_backlog(client, accounts, monkeypatch, kind):
    hold(monkeypatch)
    configure(client, accounts, account_limits={accounts['content']: 1})
    queue_control.update_settings({'concurrency': 3})
    payload = {'platform': 'pixiv', 'download': False, 'subscription_id': 'synthetic'}
    add(kind, payload)
    assert jobs.run_once()
    with SessionLocal() as db:
        db.add_all([Job(kind=kind, payload=payload) for _ in range(80)]); db.commit()
    later = add(kind, {'platform': 'jm', 'download': False})
    assert jobs.run_once()
    assert client.get('/api/jobs/'+later).json()['status'] == 'running'


def test_assigned_secondary_really_parses_details_downloads_and_checks_author(client, accounts, monkeypatch):
    jobs._stop.clear()
    configure(client, accounts)
    queue_control.update_settings({'rate_limit_enabled': False})
    seen = []
    class Connector:
        def __init__(self, credentials): self.token = credentials['refresh_token']
        def normalize_id(self, ident): return ident
        def fetch(self, ident, *, download=True, include_preview=True, checkpoint=lambda _: None):
            seen.append((self.token, 'download' if download else 'details'))
            checkpoint(50)
            return RemoteWork(remote_id=ident, title='副账号资料', source_url='https://example.invalid/work',
                metadata_only=not download, files=[('page.png', png())] if download else [], remote_page_count=1)
        def discover(self, *args, **kwargs):
            seen.append((self.token, 'checks'))
            return [], False
    monkeypatch.setattr(jobs, 'get_connector', lambda platform, credentials: Connector(credentials))
    body = add('import', {'platform': 'pixiv', 'remote_id': '88001234', 'account_id': accounts['main']})
    with SessionLocal() as db:
        subscription = Subscription(platform='pixiv', target_type='author', target='8800', label='测试作者', enabled=True, content_scope='all', next_check_at=utcnow()+timedelta(hours=1))
        db.add(subscription); db.commit(); sid = subscription.id
    check = add('source_sync', {'platform': 'pixiv', 'subscription_id': sid, 'account_id': accounts['main']})
    assert jobs.run_once() and jobs.run_once()
    for ident in (body, check):
        assert client.get('/api/jobs/'+ident).json()['status'] == 'completed'
    details = add('import', {'platform': 'pixiv', 'remote_id': '88001235', 'download': False})
    assert jobs.run_once()
    assert client.get('/api/jobs/'+details).json()['status'] == 'completed'
    assert seen == [('content-test', 'download'), ('content-test', 'checks'), ('content-test', 'details')]


def test_author_retry_clears_role_assignment_for_fresh_selection(client, accounts):
    configure(client, accounts)
    with SessionLocal() as db:
        row = Job(kind='author_profile', status='failed', payload={'account_id': accounts['main'], 'target': '123', 'check_account_id': accounts['content']})
        db.add(row); db.commit(); ident = row.id
    assert client.post('/api/jobs/'+ident+'/retry').status_code == 200
    with SessionLocal() as db:
        assert 'check_account_id' not in db.get(Job, ident).payload


def test_unused_duplicate_record_limit_does_not_join_active_identity_pool(client, accounts):
    with SessionLocal() as db:
        duplicate = SourceAccount(platform='pixiv', label='未分配功能的旧登录记录', credentials_encrypted=encrypt_credentials({'refresh_token': 'main-test', 'user_id': '10'}))
        db.add(duplicate); db.commit(); ident = duplicate.id
    result = configure(client, accounts, account_limits={ident: 3})
    assert result['account_limits'][ident] == 3
    assert ident not in result['content_account_ids']
