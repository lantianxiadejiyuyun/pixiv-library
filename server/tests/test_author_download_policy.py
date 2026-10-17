from types import SimpleNamespace
import pytest
from sqlalchemy import select
from test_library import client
from test_manual_download_queue import roles
from app import jobs, author_api
from app.database import SessionLocal
from app.integrations.base import RemoteWork
from app.integrations.pixiv import PixivConnector
from app.models import Job, Subscription


@pytest.mark.parametrize('works,bookmarks',[(False,False),(True,False),(False,True),(True,True)])
def test_subscription_worker_splits_own_and_bookmarked_works(client,roles,monkeypatch,works,bookmarks):
    discovered=SimpleNamespace(supports_subscriptions=True,discover=lambda *a,**kw:(['10','20','30'],False),
        discovery_origins={'10':{'works'},'20':{'bookmarks'},'30':{'works','bookmarks'}})
    monkeypatch.setattr(jobs,'get_connector',lambda *a,**kw:discovered)
    response=client.post('/api/subscriptions',json={'platform':'pixiv','target_type':'author','target':'77',
        'account_id':roles['main'],'download_works':works,'download_bookmarks':bookmarks})
    assert response.status_code==200,response.text
    assert response.json()['auto_download']==(works or bookmarks)
    assert jobs.run_once(('checks',))
    with SessionLocal() as db:
        payloads={j.payload['remote_id']:j.payload for j in db.scalars(select(Job).where(Job.kind=='import'))}
        assert {key:p['download'] for key,p in payloads.items()}=={'10':works,'20':bookmarks,'30':works or bookmarks}


def test_author_page_settings_create_all_types_and_keep_legacy_subscription_behavior(client,roles):
    url='/api/pixiv/authors/77/download-settings'
    response=client.put(url,json={'download_works':True,'download_bookmarks':False})
    assert response.status_code==200,response.text
    with SessionLocal() as db:
        row=db.scalar(select(Subscription));assert row.content_scope=='all' and row.account_id==roles['main']
        ident=row.id
    assert client.get('/api/pixiv/authors/77').json()['download_settings']['download_works']
    result=client.patch('/api/subscriptions/'+ident,json={'auto_download':False})
    assert not result.json()['download_works'] and not result.json()['download_bookmarks']
    client.patch('/api/subscriptions/'+ident,json={'auto_download':True}).raise_for_status()
    options=client.get('/api/pixiv/authors/77').json()['download_settings']
    assert options['download_works'] and options['download_bookmarks']
    assert client.put(url,json={'download_works':False,'download_bookmarks':True,'account_id':'wrong'}).status_code==422


def test_reader_device_can_only_set_author_scoped_download_preferences(client,roles):
    from test_devices import bind,bearer
    headers=bearer(bind(client))
    response=client.put('/api/pixiv/authors/77/download-settings',headers=headers,
        json={'download_works':False,'download_bookmarks':True})
    assert response.status_code==200,response.text
    assert response.json()['download_bookmarks']
    assert client.get('/api/subscriptions',headers=headers).status_code==403


def test_discovery_records_both_origins_for_the_same_work():
    adapter=PixivConnector({'refresh_token':'synthetic'})
    adapter._api=lambda:SimpleNamespace(user_illusts=lambda target,type:{'illusts':[{'id':10}] if type=='illust' else [],'next_url':None},
        user_bookmarks_illust=lambda *a,**kw:{'illusts':[{'id':10},{'id':20}],'next_url':None},
        user_novels=lambda target:{'novels':[{'id':31}],'next_url':None},
        user_bookmarks_novel=lambda *a,**kw:{'novels':[{'id':32}],'next_url':None})
    identities,_=adapter.discover('author','77','all',include_author_bookmarks=True)
    assert identities==['10','20','novel:31','novel:32']
    assert adapter.discovery_origins=={'10':{'works','bookmarks'},'20':{'bookmarks'},'novel:31':{'works'},'novel:32':{'bookmarks'}}


@pytest.mark.parametrize('section,expected',[('works',True),('bookmarks',False)])
def test_manual_author_catalog_fetch_respects_saved_download_choices(client,roles,monkeypatch,section,expected):
    client.put('/api/pixiv/authors/77/download-settings',json={'download_works':True,'download_bookmarks':False}).raise_for_status()
    # The subscription's immediate check is not the directory operation under test.
    with SessionLocal() as db:
        db.scalar(select(Subscription)).enabled=False;db.commit()
    request=client.post('/api/pixiv/authors/77/feed',json={'section':section,'category':'novel'})
    assert request.status_code==200,request.text
    with SessionLocal() as db:
        db.scalar(select(Subscription)).enabled=True;db.commit()
        # Avoid scheduling a separate author subscription during this explicit feed.
        from datetime import timedelta
        from app.models import utcnow
        db.scalar(select(Subscription)).next_check_at=utcnow()+timedelta(days=1);db.commit()
    work=RemoteWork(remote_id='novel:42',title='合成章节',item_type='novel',source_url='https://www.pixiv.net/novel/show.php?id=42',metadata_only=True)
    monkeypatch.setattr(author_api,'get_connector',lambda *a,**kw:SimpleNamespace(fetch_author_page=lambda *a,**kw:{'works':[work],'next_cursor':None,'raw_count':1,'unavailable':0}))
    assert jobs.run_once(('checks',))
    with SessionLocal() as db:
        task=db.scalar(select(Job).where(Job.kind=='import'))
        assert task is not None
        assert task.payload['download'] is expected
