from datetime import timedelta

import pytest

from test_library import client
from test_pixiv_accounts import accounts
from test_independent_queues import configure
from app import queue_control, worker_runtime
from app.database import SessionLocal
from app.models import Job, QueueControl, utcnow


@pytest.fixture(autouse=True)
def healthy_workers(monkeypatch):
    monkeypatch.setattr(worker_runtime, 'status', lambda: {'mode': 'thread', 'workers': []})


def lanes(client):
    response = client.get('/api/queue/status')
    assert response.status_code == 200
    return {row['id']: row for row in response.json()['lanes']}


def test_legacy_pauses_are_honest_and_reading_status_does_not_modify_them(client):
    client.get('/api/queue/settings')
    with SessionLocal() as db:
        q = db.get(QueueControl, 1)
        q.lanes = {'manual_download': {'paused': True, 'concurrency': 4}}
        db.commit()
    for _ in range(2):
        state = lanes(client)['manual_download']['diagnostic']
        reason = state['reasons'][0]
        assert state['state'] == 'paused' and reason['source'] == 'legacy'
        assert reason['changed_at'] is None and '未保存操作来源和时间' in reason['message']
    with SessionLocal() as db:
        assert 'pause_event' not in db.get(QueueControl, 1).lanes['manual_download']


def test_pause_provenance_survives_concurrency_other_lane_and_noop_updates(client):
    assert client.put('/api/queue/lanes/manual_download', json={'paused': True}).status_code == 200
    first = lanes(client)['manual_download']['diagnostic']['reasons'][0]
    assert first['source'] == 'manual' and first['changed_at'].endswith('Z')
    assert client.put('/api/queue/lanes/manual_download', json={'concurrency': 3}).status_code == 200
    assert client.put('/api/queue/lanes/ai', json={'paused': True}).status_code == 200
    assert client.put('/api/queue/settings', json={'concurrency': 4}).status_code == 200
    assert client.put('/api/queue/lanes/manual_download', json={'paused': True, 'pause_reason': 'maintenance'}).status_code == 200
    assert lanes(client)['manual_download']['diagnostic']['reasons'][0] == first
    client.put('/api/queue/lanes/manual_download', json={'paused': False})
    assert lanes(client)['manual_download']['diagnostic']['state'] == 'idle'


def test_master_and_local_pause_show_both_recovery_steps_and_exempt_independent_lanes(client):
    client.put('/api/queue/settings', json={'paused': True})
    client.put('/api/queue/lanes/download', json={'paused': True})
    state = lanes(client)
    assert [r['code'] for r in state['download']['diagnostic']['reasons']] == ['lane_paused', 'master_paused']
    assert state['details']['diagnostic']['code'] == 'master_paused'
    assert all(state[key]['diagnostic']['state'] == 'idle' for key in ('manual_download', 'retry', 'ai'))
    client.put('/api/queue/lanes/download', json={'paused': False})
    assert lanes(client)['download']['diagnostic']['code'] == 'master_paused'


def test_maintenance_reason_and_invalid_reason_updates_are_atomic(client):
    catalog = client.get('/api/mobile-admin/catalog').json()
    assert catalog['labels']['platform'] == '平台' and catalog['labels']['pause_reason'] == '暂停原因'
    client.put('/api/queue/lanes/checks', json={'paused': True, 'pause_reason': 'maintenance'})
    reason = lanes(client)['checks']['diagnostic']['reasons'][0]
    assert reason['source'] == 'maintenance' and '维护' in reason['message']
    for payload in ({'pause_reason': 'maintenance', 'concurrency': 8}, {'paused': False, 'pause_reason': 'manual'}, {'paused': True, 'pause_reason': 'secret-token'}):
        assert client.put('/api/queue/lanes/checks', json=payload).status_code in (400, 422)
    assert lanes(client)['checks']['concurrency'] == 1
    assert lanes(client)['checks']['diagnostic']['reasons'][0] == reason
    before = client.get('/api/queue/settings').json()
    assert client.put('/api/queue/settings', json={'pause_reason': 'manual', 'concurrency': 8}).status_code == 400
    assert client.get('/api/queue/settings').json() == before


def test_platform_backoff_is_waiting_not_paused_and_expires_without_mutation(client):
    client.get('/api/queue/settings')
    with SessionLocal() as db:
        q = db.get(QueueControl, 1)
        q.rate_limit_enabled = False
        q.backoff_until = utcnow() + timedelta(minutes=2)
        db.add(Job(kind='import_content', status='queued', payload={'platform': 'jm'}))
        db.commit()
    state = lanes(client)
    assert not state['download']['paused']
    diagnostic = state['download']['diagnostic']
    assert diagnostic['state'] == 'waiting' and diagnostic['code'] == 'platform_backoff'
    assert diagnostic['reasons'][0]['resume_at'].endswith('Z')
    assert state['ai']['diagnostic']['state'] == 'idle'
    with SessionLocal() as db:
        q = db.get(QueueControl, 1)
        q.backoff_until = utcnow() - timedelta(seconds=1)
        db.commit()
    assert lanes(client)['download']['diagnostic']['state'] == 'ready'


def test_worker_failures_are_not_pause_switches(client, monkeypatch):
    monkeypatch.setattr(worker_runtime, 'status', lambda: {'mode': 'process', 'workers': [{'lane': 'download', 'state': 'restarting', 'last_exit_code': -9}]})
    state = lanes(client)['download']
    assert state['diagnostic']['code'] == 'worker_restarting' and not state['paused']
    assert '内存' not in state['diagnostic']['reasons'][0]['message']  # SIGKILL alone is not proof of OOM.
    monkeypatch.setattr(worker_runtime, 'status', lambda: {'mode': 'disabled'})
    assert lanes(client)['download']['diagnostic']['code'] == 'worker_disabled'


def test_queue_concurrency_is_not_an_automatic_pause(client):
    with SessionLocal() as db:
        db.add_all([Job(kind='import_content', status=status, payload={'platform': 'jm'}) for status in ('queued', 'running')])
        db.commit()
    state = lanes(client)['download']
    assert state['diagnostic']['code'] == 'lane_concurrency'
    assert state['diagnostic']['state'] == 'running' and not state['paused']


def test_shared_account_capacity_is_explained_without_claiming_or_reassigning(client, accounts):
    configure(client, accounts, account_limits={accounts['main']: 1, accounts['content']: 1})
    client.put('/api/queue/lanes/download', json={'concurrency': 2})
    with SessionLocal() as db:
        db.add(Job(kind='import_content', status='running', payload={'platform':'pixiv','download_account_id':accounts['content']}))
        job = Job(kind='import_content', status='queued', payload={'platform':'pixiv','remote_id':'123'})
        db.add(job);db.commit();ident=job.id
    assert lanes(client)['download']['diagnostic']['code'] == 'account_capacity'
    with SessionLocal() as db:
        job=db.get(Job, ident)
        assert job.status == 'queued' and 'download_account_id' not in job.payload
    response=client.get('/api/queue/status')
    assert all(value not in response.text for value in accounts.values())
