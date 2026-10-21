from test_library import client
from app import jobs
from app.database import SessionLocal
from app.models import Job, Subscription
from sqlalchemy import select


def test_tag_tracking_defaults_to_download_and_deduplicates_checks(client):
    value = {'tags': [' synthetic ', 'synthetic', 'novel']}
    response = client.post('/api/tag-tracking', json=value)
    assert response.status_code == 200, response.text
    rows = response.json()['items']
    assert len(rows) == 2
    assert all(r['auto_download'] and r['content_scope'] == 'all' for r in rows)
    again = client.post('/api/tag-tracking', json=value).json()
    assert again['job_ids'] == response.json()['job_ids']
    job = client.post(f"/api/tag-tracking/{rows[0]['id']}/check").json()
    assert job['id'] == response.json()['job_ids'][0] and job['queue'] == 'checks'
    assert len(client.get('/api/tag-tracking').json()['items']) == 2
    with SessionLocal() as db:
        assert db.query(Subscription).count() == 2 and db.query(Job).count() == 2
        assert all(row.payload.get('download_origin') != 'manual' for row in db.scalars(select(Job)))


def test_tag_tracking_pause_and_download_are_independent(client):
    row = client.post('/api/tag-tracking', json={'tags': ['synthetic'], 'auto_download': False}).json()['items'][0]
    assert row['auto_download'] is False
    client.patch('/api/subscriptions/'+row['id'], json={'enabled': False})
    assert client.post('/api/tag-tracking/'+row['id']+'/check').status_code == 409
    client.delete('/api/subscriptions/'+row['id'])
    assert client.get('/api/tag-tracking').json()['items'] == []


def test_invalid_tag_tracking_is_bounded(client):
    for value in ({'tags': ['  ']}, {'tags': ['a'*192]}, {'tags': ['a']*21}, {'tags': ['a'], 'platform': 'jm'}):
        assert client.post('/api/tag-tracking', json=value).status_code in (400,422)


def test_tracking_queues_images_and_novels_for_download_without_duplicates(client, monkeypatch):
    calls = []
    class Connector:
        def discover(self, target_type, target, content_scope, **kwargs):
            calls.append((target_type, target, content_scope, kwargs))
            return ['123', 'novel:456', '123'], False
    monkeypatch.setattr(jobs, 'get_connector', lambda *a: Connector())
    row = client.post('/api/tag-tracking', json={'tags': ['synthetic']}).json()['items'][0]
    with SessionLocal() as db:
        payload = {'platform': 'pixiv', 'subscription_id': row['id']}
        jobs._sync(db, payload, lambda _: None, lambda _: None)
        jobs._sync(db, payload, lambda _: None, lambda _: None)
        imports = list(db.scalars(select(Job).where(Job.kind == 'import')))
        assert len(imports) == 2
        assert {r.payload['remote_id'] for r in imports} == {'123', 'novel:456'}
        assert all(r.payload['download'] is True for r in imports)
        assert all(r.payload.get('download_origin') != 'manual' for r in imports)
    assert calls[0][:3] == ('tag', 'synthetic', 'all')
    assert calls[0][3]['include_author_bookmarks'] is False


def test_device_tag_permissions_do_not_grant_general_subscription_management(client):
    from test_devices import bind, bearer
    headers = bearer(bind(client))
    client.cookies.clear()
    row = client.post('/api/tag-tracking', headers=headers, json={'tags': ['synthetic']}).json()['items'][0]
    assert client.patch('/api/tag-tracking/'+row['id'], headers=headers, json={'enabled': False}).status_code == 200
    assert client.get('/api/tag-tracking', headers=headers).status_code == 200
    assert client.post('/api/subscriptions', headers=headers, json={}).status_code == 403
    assert client.patch('/api/tag-tracking/'+row['id'], headers=headers, json={'account_id': 'unexpected'}).status_code == 422
