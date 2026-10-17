"""Retries have their own dispatch capacity while preserving original work."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import cpu_affinity, jobs, pixiv_accounts, queue_control
from app.database import SessionLocal
from app.integrations.base import ConnectorError, RemoteWork
from app.models import Job, NetworkSetting, Subscription, utcnow
from test_library import client, png
from test_retry_permissions import roles as retry_roles, configure as configure_retry, retry as enqueue_pixiv_retry, status as retry_status


def failed(kind='import', **payload):
    with SessionLocal() as db:
        row = Job(kind=kind, status='failed', error='Synthetic previous failure',
                  attempts=2, payload={'platform': 'jm', 'remote_id': '42', **payload})
        db.add(row)
        db.commit()
        return row.id


def test_python_and_sql_lane_classification_agree_without_double_counting(client):
    cases = [('import', {'platform': 'jm'}), ('import', {'platform': 'pixiv'}),
             ('import', {'platform': 'pixiv', 'download': False}),
             ('import', {'platform': 'pixiv', 'download_origin': 'manual'}),
             ('import_content', {}), ('import_content', {'download_origin': 'manual'}),
             *[(kind, {}) for kind in ('source_sync', 'series_sync', 'author_profile',
                                      'author_feed', 'pixiv_bookmark', 'ai', 'translate', 'unknown')]]
    with SessionLocal() as db:
        for kind, payload in cases:
            for flags in ({}, {'retry_queue': None}, {'retry_queue': False}, {'retry_queue': True}):
                db.add(Job(kind=kind, payload={**payload, **flags}))
        db.commit()
        rows = db.scalars(select(Job)).all()
        expression = dict(db.execute(select(Job.id, queue_control.lane_expression())).all())
        memberships = {lane: set(db.scalars(select(Job.id).where(queue_control.lane_clause(lane))))
                       for lane in queue_control.LANES}
        for row in rows:
            expected = queue_control.job_lane(row)
            assert expression[row.id] == expected
            assert [lane for lane, ids in memberships.items() if row.id in ids] == [expected]


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_retry_moves_same_job_preserving_origin_and_previous_error(client, status):
    ident = failed(download_origin='manual', download=False)
    with SessionLocal() as db:
        db.get(Job, ident).status = status
        db.commit()
    response = client.post(f'/api/jobs/{ident}/retry')
    assert response.status_code == 200, response.text
    row = response.json()
    assert row['id'] == ident and row['queue'] == 'retry'
    assert row['original_queue'] == 'manual_download'
    assert row['status'] == 'queued' and row['error'] is None
    assert row['retry_last_error'] == 'Synthetic previous failure'
    assert 'payload' not in row
    assert client.post(f'/api/jobs/{ident}/retry').status_code == 409
    with SessionLocal() as db:
        stored = db.get(Job, ident)
        assert stored.payload['download'] is False
        assert stored.payload['download_origin'] == 'manual'
        assert stored.attempts == 2
        assert len(db.scalars(select(Job)).all()) == 1
    assert [job['id'] for job in client.get('/api/jobs?queue=retry').json()['items']] == [ident]
    assert not client.get('/api/jobs?queue=manual_download').json()['items']


def test_retry_deduplicates_work_against_active_original_or_retry_lane(client):
    ident = failed()
    with SessionLocal() as db:
        active = jobs.enqueue_import(db, {'platform': 'jm', 'remote_id': '42'}).id
    assert client.post(f'/api/jobs/{ident}/retry').json()['id'] == active
    with SessionLocal() as db:
        assert db.get(Job, ident).status == 'failed'
        db.get(Job, active).status = 'failed'
        db.commit()
    assert client.post(f'/api/jobs/{active}/retry').json()['queue'] == 'retry'
    assert client.post(f'/api/jobs/{ident}/retry').json()['id'] == active
    with SessionLocal() as db:
        assert db.get(Job, ident).status == 'failed'
        assert len(db.scalars(select(Job)).all()) == 2


def test_collection_settings_do_not_change_retry_pause_or_limit(client):
    queue_control.update_settings({'concurrency': 8, 'paused': True})
    lanes = {row['id']: row for row in client.get('/api/queue/status').json()['lanes']}
    assert lanes['retry']['concurrency'] == 1
    assert not lanes['retry']['blocked_by_master'] and not lanes['retry']['paused']
    assert client.put('/api/queue/lanes/retry', json={'concurrency': 3, 'paused': True}).status_code == 200
    queue_control.update_settings({'concurrency': 2, 'paused': False})
    lanes = {row['id']: row for row in client.get('/api/queue/status').json()['lanes']}
    assert lanes['retry']['concurrency'] == 3 and lanes['retry']['paused']


def test_retry_dispatch_is_independent_and_obeys_its_own_concurrency(client):
    jobs._stop.clear()
    queue_control.update_settings({'paused': True, 'concurrency': 1})
    queue_control.update_lane('retry', {'concurrency': 2})
    with SessionLocal() as db:
        for index in range(5):
            db.add(Job(kind='import', payload={'platform': 'jm', 'remote_id': str(index), 'retry_queue': True}))
        normal = Job(kind='import_content', status='running', payload={'platform': 'jm'},
                     lease_until=utcnow() + timedelta(minutes=5), worker_id='synthetic-worker')
        db.add(normal)
        db.commit()
    with ThreadPoolExecutor(max_workers=5) as executor:
        claims = list(executor.map(lambda _: jobs.claim_once(('retry',), preflight=True), range(5)))
    assert sum(claim is not None for claim in claims) == 2
    assert len({claim[0] for claim in claims if claim}) == 2
    assert not jobs.claim_once(('retry',), preflight=True)
    queue_control.update_lane('retry', {'concurrency': 3, 'paused': True})
    assert not jobs.claim_once(('retry',), preflight=True)
    queue_control.update_lane('retry', {'paused': False})
    assert jobs.claim_once(('retry',), preflight=True)
    with SessionLocal() as db:
        assert all(queue_control.job_lane(db.get(Job, claim[0])) == 'retry' for claim in claims if claim)


def test_retry_pacing_obeys_own_pause_but_not_collection_pause(client):
    queue_control.update_settings({'paused': True, 'rate_limit_enabled': False})
    with queue_control.request_context(lambda: None, 'retry'):
        assert queue_control.current_lane() == 'retry'
        queue_control.check_paused()
        queue_control.wait_for_request()
        queue_control.update_lane('retry', {'paused': True})
        with pytest.raises(queue_control.QueuePaused):
            queue_control.check_paused()
        with pytest.raises(queue_control.QueuePaused):
            queue_control.wait_for_request()
    assert queue_control.current_lane() is None


def test_two_stage_retry_keeps_body_in_retry_queue(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({'paused': True, 'rate_limit_enabled': False})
    def fetch(ident, *, download=True, checkpoint=lambda _: None, **kwargs):
        checkpoint(50)
        queue_control.wait_for_request()
        return RemoteWork(remote_id=ident, title='Synthetic', source_url='https://example.invalid/work',
                          metadata_only=not download, files=[('page.png', png())] if download else [])
    monkeypatch.setattr(jobs, 'get_connector', lambda *args, **kwargs: SimpleNamespace(normalize_id=lambda value: value, fetch=fetch))
    ident = failed(download_origin='manual')
    assert client.post(f'/api/jobs/{ident}/retry').json()['queue'] == 'retry'
    assert not jobs.run_once(('manual_download', 'download', 'details'))
    assert jobs.run_once(('retry',))
    with SessionLocal() as db:
        first = db.get(Job, ident)
        assert first.status == 'completed', first.error
        body = db.scalar(select(Job).where(Job.kind == 'import_content'))
        assert queue_control.job_lane(body) == 'retry'
        assert body.payload['download_origin'] == 'manual'
        assert body.payload['retry_last_error'] == 'Synthetic previous failure'
        body_id = body.id
    assert not jobs.run_once(('manual_download', 'download'))
    assert jobs.run_once(('retry',))
    row = client.get(f'/api/jobs/{body_id}').json()
    assert row['status'] == 'completed', row['error']
    assert row['queue'] == 'retry' and row['original_queue'] == 'manual_download'


def test_retried_discovery_sends_newly_discovered_work_to_normal_queue(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({'paused': True, 'rate_limit_enabled': False})
    monkeypatch.setattr(jobs, 'get_connector', lambda *args, **kwargs:
                        SimpleNamespace(discover=lambda *args: (['new-work'], False)))
    with SessionLocal() as db:
        subscription = Subscription(platform='jm', target_type='tag', target='synthetic', label='Synthetic', auto_download=True)
        db.add(subscription)
        db.commit()
        subscription_id = subscription.id
    ident = failed(kind='source_sync', subscription_id=subscription_id)
    assert client.post(f'/api/jobs/{ident}/retry').json()['queue'] == 'retry'
    assert jobs.run_once(('retry',))
    with SessionLocal() as db:
        assert db.get(Job, ident).status == 'completed', db.get(Job, ident).error
        discovered = db.scalar(select(Job).where(Job.kind == 'import'))
        assert discovered.payload['remote_id'] == 'new-work'
        assert queue_control.job_lane(discovered) == 'details'
    assert not jobs.run_once(('retry',))


def test_retry_failure_keeps_latest_error_for_next_attempt(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    def fetch(*args, **kwargs):
        raise ConnectorError('Synthetic new failure')
    monkeypatch.setattr(jobs, 'get_connector', lambda *args, **kwargs:
                        SimpleNamespace(normalize_id=lambda value: value, fetch=fetch))
    ident = failed()
    client.post(f'/api/jobs/{ident}/retry').raise_for_status()
    assert jobs.run_once(('retry',))
    row = client.get(f'/api/jobs/{ident}').json()
    assert row['status'] == 'failed' and row['error'] == 'Synthetic new failure'
    assert row['retry_last_error'] == 'Synthetic previous failure'
    retried = client.post(f'/api/jobs/{ident}/retry').json()
    assert retried['status'] == 'queued' and retried['retry_last_error'] == 'Synthetic new failure'
    with SessionLocal() as db:
        assert db.get(Job, ident).attempts == 3


def test_retry_pause_during_fetch_returns_job_to_retry_without_saving_body(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    def fetch(ident, *, checkpoint=lambda _: None, **kwargs):
        queue_control.update_lane('retry', {'paused': True})
        checkpoint(50)
        pytest.fail('Paused retry continued beyond checkpoint')
    monkeypatch.setattr(jobs, 'get_connector', lambda *args, **kwargs:
                        SimpleNamespace(normalize_id=lambda value: value, fetch=fetch))
    ident = failed()
    client.post(f'/api/jobs/{ident}/retry').raise_for_status()
    assert jobs.run_once(('retry',))
    row = client.get(f'/api/jobs/{ident}').json()
    assert row['status'] == 'queued' and row['queue'] == 'retry'
    assert row['error'] is None and row['retry_last_error'] == 'Synthetic previous failure'
    assert not jobs.claim_once(('retry',), preflight=True)
    assert client.get('/api/items').json()['total'] == 0


@pytest.mark.parametrize('missing', [{'retry'}, {'retry', 'manual_download'}])
def test_old_affinity_maps_get_retry_process_without_moving_existing_workers(missing):
    previous = {role: [index] for index, role in enumerate(cpu_affinity.ROLES) if role not in missing}
    updated = cpu_affinity.plan(range(len(cpu_affinity.ROLES)), json.dumps(previous))
    assert set(updated) == set(cpu_affinity.ROLES)
    assert all(updated[key] == value for key, value in previous.items())
    assert updated['retry'] == previous['download']


def test_failed_work_is_not_automatically_replayed(client):
    ident = failed()
    assert not jobs.claim_once(('retry',), preflight=True)
    with SessionLocal() as db:
        assert db.get(Job, ident).status == 'failed'


def test_retry_permission_revoked_during_request_stops_at_next_checkpoint(client, retry_roles, monkeypatch):
    configure_retry(client, retry_roles, retry_account_ids=[retry_roles['retry'], retry_roles['details']])
    def fetch(ident, *, checkpoint=lambda _: None, **kwargs):
        configure_retry(client, retry_roles, retry_account_ids=[]).raise_for_status()
        checkpoint(30)
        pytest.fail('Revoked retry reached the next stage')
    monkeypatch.setattr(jobs, 'get_connector', lambda *args, **kwargs:
                        SimpleNamespace(normalize_id=lambda value: value, fetch=fetch))
    ident = enqueue_pixiv_retry(client)
    assert jobs.run_once(('retry',))
    state, error, _ = retry_status(ident)
    assert state == 'failed' and '重试权限' in error
    assert client.get('/api/items').json()['total'] == 0


def test_retry_roles_disabled_can_claim_later_identity_behind_saturated_backlog(client, retry_roles):
    configure_retry(client, retry_roles, enabled=False,
                    retry_account_ids=[retry_roles['main'], retry_roles['auto']],
                    account_limits={retry_roles['main']: 1, retry_roles['auto']: 1}).raise_for_status()
    with SessionLocal() as db:
        db.add(Job(kind='import', status='running', worker_id='busy', lease_until=utcnow() + timedelta(minutes=5),
                   payload={'platform': 'pixiv', 'execution_account_id': retry_roles['main']}))
        for index in range(70):
            db.add(Job(kind='import', payload={'platform': 'pixiv', 'remote_id': str(index),
                                              'account_id': retry_roles['main'], 'retry_queue': True}))
        eligible = Job(kind='import', payload={'platform': 'pixiv', 'remote_id': 'later',
                                               'account_id': retry_roles['auto'], 'retry_queue': True})
        db.add(eligible)
        db.commit()
        ident = eligible.id
    claim = jobs.claim_once(('retry',), preflight=True)
    assert claim and claim[0] == ident


def test_retry_commit_guard_does_not_trust_cached_permissions(client, retry_roles):
    configure_retry(client, retry_roles, retry_account_ids=[retry_roles['retry'], retry_roles['details']])
    ident = enqueue_pixiv_retry(client)
    claim = jobs.claim_once(('retry',))
    assert claim and claim[0] == ident
    with SessionLocal() as db:
        cached = db.get(NetworkSetting, pixiv_accounts.KEY)
        assert cached is not None
        configure_retry(client, retry_roles, retry_account_ids=[]).raise_for_status()
        owner = jobs._lease_owner.set(claim[1])
        try:
            with pytest.raises(ConnectorError, match='重试权限'):
                jobs.commit_guard(db, ident)
        finally:
            jobs._lease_owner.reset(owner)
