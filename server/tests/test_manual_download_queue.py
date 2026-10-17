"""Manual downloads have a separate lane and an explicitly granted identity pool."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import cpu_affinity, jobs, queue_control
from app.database import SessionLocal
from app.integrations.base import RemoteWork
from app.integrations.credentials import encrypt_credentials
from app.models import Item, Job, SourceAccount
from test_library import client, png

URL = '/api/sources/pixiv/account-roles'


@pytest.fixture
def roles(client):
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    with SessionLocal() as db:
        rows = [SourceAccount(platform='pixiv', label=name,
            credentials_encrypted=encrypt_credentials({'refresh_token': name + '-synthetic'}))
            for name in ('main', 'auto', 'manual', 'details')]
        db.add_all(rows); db.commit()
        ids = {row.label: row.id for row in rows}
    response = client.put(URL, json={'enabled': True, 'main_account_id': ids['main'],
        'content_account_ids': [ids['auto']], 'manual_account_ids': [ids['manual']],
        'detail_account_ids': [ids['details']]})
    assert response.status_code == 200, response.text
    return ids


@pytest.mark.parametrize('remote_id', ['42', 'novel:42'])
def test_manual_real_worker_uses_only_manual_pool_for_body(client, roles, monkeypatch, remote_id):
    seen = []
    def factory(platform, credentials=None):
        name = credentials['refresh_token'].split('-')[0]
        def fetch(ident, *, download=True, **kwargs):
            seen.append((name, download))
            novel = ident.startswith('novel:')
            return RemoteWork(remote_id=ident, title='Synthetic', source_url='https://www.pixiv.net/artworks/42',
                item_type='novel' if novel else 'manga', metadata_only=not download, is_bookmarked=False,
                remote_page_count=None if novel else 1,
                files=[('body.txt', b'synthetic')] if novel and download else [('page.png', png())] if download else [])
        return SimpleNamespace(normalize_id=lambda value: value, fetch=fetch)
    monkeypatch.setattr(jobs, 'get_connector', factory)
    response = client.post('/api/sources/pixiv/import', json={'remote_id': remote_id})
    assert response.status_code == 200, response.text
    assert response.json()['queue'] == 'manual_download'
    assert not jobs.run_once(('download',))
    assert jobs.run_once(('manual_download',))
    with SessionLocal() as db:
        job = db.get(Job, response.json()['id'])
        assert job.status == 'completed', job.error
        assert job.payload['download_account_id'] == roles['manual']
        assert db.get(Item, job.result['item_id']).assets
    assert ('manual', True) in seen
    assert all(name != 'auto' for name, _ in seen)
    assert seen == ([('manual', True)] if remote_id.startswith('novel:') else [('details', False), ('manual', True)])


def test_no_manual_permission_fails_closed_without_borrowing_auto_or_main(client, roles, monkeypatch):
    assert client.put(URL, json={'enabled': True, 'main_account_id': roles['main'],
        'content_account_ids': [roles['auto']], 'manual_account_ids': []}).status_code == 200
    response = client.post('/api/sources/pixiv/import', json={'remote_id': '42'})
    assert response.status_code == 409 and '手动下载账号' in response.json()['detail']
    with SessionLocal() as db:
        assert not db.scalars(select(Job)).all()
        ident = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '42', 'download_origin': 'manual'}).id
    monkeypatch.setattr(jobs, 'get_connector', lambda *a, **kw: pytest.fail('Unpermitted connector created'))
    assert jobs.run_once(('manual_download',))
    with SessionLocal() as db:
        job = db.get(Job, ident)
        assert job.status == 'failed' and '手动下载账号' in job.error


def test_legacy_settings_client_preserves_manual_permission_and_validates_removed_accounts(client, roles):
    assert client.put(URL, json={'enabled': True, 'main_account_id': roles['main'],
        'content_account_ids': [roles['auto']]}).json()['manual_account_ids'] == [roles['manual']]
    assert client.put(URL, json={'enabled': True, 'main_account_id': roles['main'],
        'manual_account_ids': ['missing']}).status_code == 400


def test_manual_and_automatic_lanes_have_independent_pause_and_limits(client, roles, monkeypatch):
    monkeypatch.setattr(jobs, 'process_job', lambda _: None)
    queue_control.update_lane('manual_download', {'concurrency': 1})
    queue_control.update_lane('download', {'concurrency': 1, 'paused': True})
    with SessionLocal() as db:
        automatic = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '10'}).id
        manual = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '20', 'download_origin': 'manual'}).id
        jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '21', 'download_origin': 'manual'})
    assert jobs.run_once(('manual_download',))
    assert not jobs.run_once(('manual_download',))
    assert not jobs.run_once(('download',))
    queue_control.update_lane('download', {'paused': False})
    assert jobs.run_once(('download',))
    with SessionLocal() as db:
        assert db.get(Job, automatic).payload['download_account_id'] == roles['auto']
        assert db.get(Job, manual).payload['download_account_id'] == roles['manual']
    for lane, ident in [('download', automatic), ('manual_download', manual)]:
        result = client.get('/api/jobs', params={'queue': lane}).json()['items']
        assert ident in [job['id'] for job in result]
        assert all(job['queue'] == lane for job in result)
    queue_control.update_settings({'paused': True})
    with SessionLocal() as db:
        db.get(Job, manual).status = 'completed'; db.commit()
    assert jobs.run_once(('manual_download',))
    status = client.get('/api/queue/status').json()
    assert not next(l for l in status['lanes'] if l['id'] == 'manual_download')['blocked_by_master']
    with queue_control.request_context(lambda: None, 'manual_download'):
        queue_control.check_paused('manual_download')
        queue_control.wait_for_request()
    queue_control.update_lane('manual_download', {'paused': True})
    with queue_control.request_context(lambda: None, 'manual_download'):
        with pytest.raises(queue_control.QueuePaused): queue_control.wait_for_request()


def test_waiting_automatic_job_promotes_but_running_body_is_not_duplicated(client, roles, monkeypatch):
    monkeypatch.setattr(jobs, 'process_job', lambda _: None)
    with SessionLocal() as db:
        old = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '42', 'download': False,
            'details_account_id': roles['auto'], 'download_account_id': roles['auto']})
        ident = old.id
    response = client.post('/api/sources/pixiv/import', json={'remote_id': '42'})
    assert response.json()['id'] == ident and response.json()['queue'] == 'manual_download'
    with SessionLocal() as db:
        assert 'download_account_id' not in db.get(Job, ident).payload
    assert jobs.run_once(('manual_download',))
    duplicate = client.post('/api/sources/pixiv/import', json={'remote_id': '42'}).json()
    assert duplicate['id'] == ident
    with SessionLocal() as db:
        assert len(db.scalars(select(Job)).all()) == 1
        assert db.get(Job, ident).payload['download_account_id'] == roles['manual']


def test_non_pixiv_two_stage_manual_download_retains_origin(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    def fetch(ident, *, download=True, **kw):
        return RemoteWork(remote_id=ident, title='Synthetic', source_url='https://18comic.vip/album/42',
            metadata_only=not download, files=[('page.png', png())] if download else [])
    monkeypatch.setattr(jobs, 'get_connector', lambda *a, **kw: SimpleNamespace(normalize_id=lambda x: x, fetch=fetch))
    response = client.post('/api/sources/jm/import', json={'remote_id': '42'})
    assert response.status_code == 200 and response.json()['queue'] == 'manual_download'
    queue_control.update_settings({'paused': True})
    assert jobs.run_once(('manual_download',))
    with SessionLocal() as db:
        body = db.scalar(select(Job).where(Job.kind == 'import_content'))
        assert body.payload['download_origin'] == 'manual'
        ident = body.id
    assert not jobs.run_once(('download',))
    assert jobs.run_once(('manual_download',))
    assert client.get('/api/jobs/'+ident).json()['status'] == 'completed'


def test_old_cpu_map_adds_manual_process_without_changing_existing_placement():
    previous = {role: [index] for index, role in enumerate(cpu_affinity.ROLES) if role != 'manual_download'}
    current = cpu_affinity.plan(range(len(cpu_affinity.ROLES)), json.dumps(previous))
    assert all(current[key] == value for key, value in previous.items())
    assert current['manual_download'] == previous['download']


def test_collection_settings_never_overwrite_manual_lane(client):
    queue_control.update_settings({'concurrency': 8})
    status = client.get('/api/queue/status').json()
    assert next(l for l in status['lanes'] if l['id'] == 'manual_download')['concurrency'] == 1
    queue_control.update_lane('manual_download', {'concurrency': 3, 'paused': False})
    client.put('/api/queue/settings', json={'paused': True, 'concurrency': 1}).raise_for_status()
    lanes = {lane['id']: lane for lane in client.get('/api/queue/status').json()['lanes']}
    assert lanes['manual_download']['concurrency'] == 3
    assert not lanes['manual_download']['paused'] and not lanes['manual_download']['blocked_by_master']
    assert all(lanes[key]['concurrency'] == 1 and lanes[key]['blocked_by_master'] for key in queue_control.COLLECTION_LANES)


def test_manual_worker_keeps_fetching_when_collection_is_paused_mid_request(client, roles, monkeypatch):
    def fetch(ident, *, download=True, checkpoint=lambda _: None, **kwargs):
        queue_control.update_settings({'paused': True})
        checkpoint(50)
        queue_control.wait_for_request()
        return RemoteWork(remote_id=ident, title='Synthetic', source_url='https://www.pixiv.net/novel/show.php?id=42',
            item_type='novel', files=[('body.txt', b'synthetic')])
    monkeypatch.setattr(jobs, 'get_connector', lambda *a, **kw: SimpleNamespace(normalize_id=lambda x: x, fetch=fetch))
    response = client.post('/api/sources/pixiv/import', json={'remote_id': 'novel:42'})
    assert response.status_code == 200
    claim = jobs.claim_once(('manual_download',), preflight=True)
    assert claim and claim[0] == response.json()['id']
    ident = claim[0]
    jobs.execute_claim(*claim)
    with SessionLocal() as db:
        job = db.get(Job, ident)
        assert job.status == 'completed', job.error
        assert db.get(Item, job.result['item_id']).assets


def test_manual_request_during_automatic_metadata_promotes_followup_body(client, monkeypatch):
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    with SessionLocal() as db:
        ident = jobs.enqueue_import(db, {'platform': 'jm', 'remote_id': '42'}).id
    def fetch(remote_id, **kwargs):
        response = client.post('/api/sources/jm/import', json={'remote_id': '42'})
        assert response.status_code == 200 and response.json()['id'] == ident
        return RemoteWork(remote_id=remote_id, title='Synthetic', source_url='https://18comic.vip/album/42', metadata_only=True)
    monkeypatch.setattr(jobs, 'get_connector', lambda *a, **kw: SimpleNamespace(normalize_id=lambda x: x, fetch=fetch))
    assert jobs.run_once(('details',))
    with SessionLocal() as db:
        body = db.scalar(select(Job).where(Job.kind == 'import_content'))
        assert queue_control.job_lane(body) == 'manual_download'
