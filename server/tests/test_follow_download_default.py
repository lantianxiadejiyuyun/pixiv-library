from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import jobs, queue_control
from app.database import SessionLocal
from app.models import Item, Job, Subscription, utcnow
from app.subscription_policy import wants_body
from test_library import client
from test_manual_download_queue import roles
from test_devices import bind, bearer

URL = '/api/pixiv/follow-download-settings'


def enable(client, enabled=True):
    response = client.put(URL, json={'enabled': enabled})
    assert response.status_code == 200, response.text
    assert response.json() == {'enabled': enabled, 'scope': 'subscribed_author_works'}


def test_default_off_persists_separately_and_mobile_admin_exposes_boolean(client):
    assert client.get(URL).json()['enabled'] is False
    enable(client)
    assert client.get(URL).json()['enabled'] is True
    assert client.get('/api/pixiv/collection-settings').json()['enabled'] is False
    client.put('/api/pixiv/collection-settings', json={'enabled': True}).raise_for_status()
    enable(client, False)
    assert client.get('/api/pixiv/collection-settings').json()['enabled'] is True
    catalog = client.get('/api/mobile-admin/catalog').json()
    action = next(a for a in catalog['actions'] if a['id'] == 'PUT /pixiv/follow-download-settings')
    assert action['body']['properties']['enabled']['type'] == 'boolean'
    assert client.put(URL, json={'enabled': True, 'download_bookmarks': True}).status_code == 422
    headers = bearer(bind(client))
    assert client.get(URL, headers=headers).status_code == 403
    assert client.put(URL, headers=headers, json={'enabled': True}).status_code == 403
    client.cookies.clear()
    assert client.get(URL).status_code == 401


def test_enabling_only_reschedules_inherited_enabled_authors_and_is_idempotent(client):
    future = utcnow() + timedelta(days=1)
    with SessionLocal() as db:
        rows = [Subscription(platform='pixiv', target_type='author', target=str(index),
                             label='Synthetic', next_check_at=future) for index in range(6)]
        rows[1].download_works = False
        rows[2].download_works = True
        rows[3].enabled = False
        rows[4].target_type = 'tag'
        rows[5].platform = 'jm'
        db.add_all(rows); db.commit(); ids = [row.id for row in rows]
    before = client.get('/api/queue/settings').json()
    enable(client)
    with SessionLocal() as db:
        assert db.get(Subscription, ids[0]).next_check_at is None
        assert all(db.get(Subscription, ident).next_check_at == future for ident in ids[1:])
        assert db.get(Subscription, ids[0]).download_works is None
        db.get(Subscription, ids[0]).next_check_at = future; db.commit()
    enable(client)
    with SessionLocal() as db:
        assert db.get(Subscription, ids[0]).next_check_at == future
    assert client.get('/api/queue/settings').json() == before


@pytest.mark.parametrize('enabled,override,expected', [(False,None,False),(True,None,True),(True,False,False),(False,True,True)])
def test_real_checks_use_default_only_for_works_and_promote_metadata(client, roles, monkeypatch, enabled, override, expected):
    with SessionLocal() as db:
        subscription = Subscription(platform='pixiv', target_type='author', target='77', label='Synthetic',
            content_scope='all', account_id=roles['main'], download_works=override, download_bookmarks=False)
        db.add(subscription)
        db.add(Item(source='pixiv', source_id='10', title='Existing metadata', preview_cover='synthetic.webp'))
        waiting = jobs.enqueue_import(db, {'platform':'pixiv', 'remote_id':'11', 'download':False, 'account_id':roles['main']})
        waiting_id = waiting.id; db.commit()
    enable(client, enabled)
    fake = SimpleNamespace(supports_subscriptions=True, discover=lambda *a,**kw:(['10','11','novel:12','20','30'],False),
        discovery_origins={'10':{'works'},'11':{'works'},'novel:12':{'works'},'20':{'bookmarks'},'30':{'works','bookmarks'}})
    monkeypatch.setattr(jobs, 'get_connector', lambda *a,**kw:fake)
    assert jobs.run_once(('checks',))
    with SessionLocal() as db:
        rows = list(db.scalars(select(Job).where(Job.kind=='import')))
        payloads = {j.payload['remote_id']:j.payload for j in rows}
        assert ('10' in payloads) is expected
        assert {ident:p['download'] for ident,p in payloads.items() if ident!='10'} == {
            '11':expected, 'novel:12':expected, '20':False, '30':expected}
        assert next(j.id for j in rows if j.payload['remote_id']=='11') == waiting_id
        assert sum(queue_control.job_lane(j)=='download' for j in rows) == (4 if expected else 0)
    listed = client.get('/api/subscriptions').json()['items'][0]
    assert listed['download_works'] is expected and listed['download_bookmarks'] is False
    assert client.get('/api/pixiv/authors/77').json()['download_settings']['download_works'] is expected
    client.put('/api/queue/settings',json={'paused':True}).raise_for_status()
    assert not jobs.run_once(('download',))


def test_new_follow_inherits_and_toggle_does_not_overwrite_author_preferences(client, roles, monkeypatch):
    enable(client)
    fake = SimpleNamespace(follows=lambda **kw:[{'target_type':'author','target':'77','label':'Synthetic'}], supports_watchlist=False)
    monkeypatch.setattr(jobs, 'get_connector', lambda *a,**kw:fake)
    with SessionLocal() as db:
        result = jobs._sync(db, {'platform':'pixiv','account_id':roles['main']}, lambda _:None, lambda _:None)
        row = db.scalar(select(Subscription))
        assert result['added_subscriptions']==1 and row.content_scope=='all' and row.download_works is None
        ident = row.id
    assert client.get('/api/subscriptions').json()['items'][0]['download_works'] is True
    enable(client, False)
    assert client.get('/api/subscriptions').json()['items'][0]['download_works'] is False
    client.patch('/api/subscriptions/'+ident,json={'download_works':True}).raise_for_status()
    enable(client); enable(client, False)
    assert client.get('/api/subscriptions').json()['items'][0]['download_works'] is True


def test_legacy_explicit_off_and_reset_to_inherit(client, roles):
    enable(client)
    row = client.post('/api/subscriptions',json={'platform':'pixiv','target_type':'author','target':'77','account_id':roles['main']}).json()
    assert row['download_works'] is True and row['download_bookmarks'] is False
    url = '/api/subscriptions/'+row['id']
    assert client.patch(url,json={'auto_download':False}).json()['download_works'] is False
    assert client.patch(url,json={'download_works':None}).json()['download_works'] is True
    explicit = client.post('/api/subscriptions',json={'platform':'pixiv','target_type':'author','target':'78','auto_download':False}).json()
    assert explicit['download_works'] is False
    client.patch(url,json={'auto_download':True}).raise_for_status()
    with SessionLocal() as db:
        # Legacy adapters may lack origin labels; explicitly downloading both
        # categories must retain the previous behavior in that case.
        assert wants_body(db.get(Subscription,row['id']),'10',None)


def test_default_is_reread_after_network_work_and_missing_origin_stays_metadata(client, roles, monkeypatch):
    with SessionLocal() as db:
        row = Subscription(platform='pixiv', target_type='author', target='77', label='Synthetic',account_id=roles['main'])
        db.add(row);db.commit()
        assert not wants_body(row,'10',None,default_works=True)
    enable(client)
    def discover(*args,**kwargs):
        enable(client, False)
        return ['10'],False
    monkeypatch.setattr(jobs,'get_connector',lambda *a,**kw:SimpleNamespace(supports_subscriptions=True,discover=discover,discovery_origins={'10':{'works'}}))
    assert jobs.run_once(('checks',))
    with SessionLocal() as db:
        assert db.scalar(select(Job).where(Job.kind=='import')).payload['download'] is False


@pytest.mark.parametrize('section', ['works','bookmarks'])
def test_author_catalog_uses_the_same_default_without_downloading_bookmarks(client, roles, monkeypatch, section):
    from app import author_api
    from app.integrations.base import RemoteWork
    enable(client)
    with SessionLocal() as db:
        db.add(Subscription(platform='pixiv',target_type='author',target='77',label='Synthetic',
            content_scope='all',account_id=roles['main'],next_check_at=utcnow()+timedelta(days=1)))
        db.commit()
    client.post('/api/pixiv/authors/77/feed',json={'section':section,'category':'novel'}).raise_for_status()
    work = RemoteWork(remote_id='novel:42', title='Synthetic', item_type='novel',
                      source_url='https://www.pixiv.net/novel/show.php?id=42',metadata_only=True)
    monkeypatch.setattr(author_api,'get_connector',lambda *a,**kw:SimpleNamespace(
        fetch_author_page=lambda *a,**kw:{'works':[work],'next_cursor':None,'raw_count':1,'unavailable':0}))
    assert jobs.run_once(('checks',))
    with SessionLocal() as db:
        task=db.scalar(select(Job).where(Job.kind=='import'))
        assert task.payload['download'] is (section=='works')
