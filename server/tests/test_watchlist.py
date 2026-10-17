from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select

from test_library import client
from test_manual_download_queue import roles
from app import jobs, queue_control, watchlist
from app.database import SessionLocal
from app.integrations.base import ConnectorError, RemoteWork
from app.integrations.pixiv import PixivConnector
from app.models import Item, Job, PixivWatchlist, SourceAccount, utcnow

URL='/api/pixiv/watchlist'


def record(ident='42',kind='novel',latest='100'):
    return dict(remote_id=ident,kind=kind,title='合成系列',author='合成作者',author_id='77',content_count=1,
                latest_content_id=latest,remote_updated_at='2026-10-16T10:00:00+09:00')


def connector(monkeypatch, records, *, hook=lambda:None):
    def snapshot(**kw):
        hook();return [dict(row) for row in records]
    def fetch_series(ident,kind,**kw):
        return {'remote_id':ident,'kind':kind,'title':'合成系列','complete':True,'works':[
            RemoteWork(remote_id='novel:100' if kind=='novel' else '100',title='合成章节',item_type=kind,
                       source_url='https://www.pixiv.net/novel/show.php?id=100',metadata_only=True)]}
    monkeypatch.setattr(jobs,'get_connector',lambda *a,**kw:SimpleNamespace(watchlist=snapshot,fetch_series=fetch_series))


def sync(client):
    result=client.post(URL+'/sync');assert result.status_code==200,result.text
    for _ in range(5):
        assert jobs.run_once(('checks',))
        if client.get('/api/jobs/'+result.json()['id']).json()['status']=='completed': break
    assert client.get('/api/jobs/'+result.json()['id']).json()['status']=='completed'
    return result.json()['id']


def test_snapshot_dedup_both_types_catalog_queue_and_read_status(client,roles,monkeypatch):
    connector(monkeypatch,[record(),record(kind='manga')])
    first=client.post(URL+'/sync').json()['id']
    assert client.post(URL+'/sync').json()['id']==first
    assert jobs.run_once(('checks',))
    rows=client.get(URL).json()
    assert rows['total']==2 and rows['sync']['pending_tasks']==2
    assert {r['kind'] for r in rows['items']}=={'novel','manga'}
    assert all(r['has_update'] and not r['auto_download'] for r in rows['items'])
    assert rows['sync']['last_checked_at'].endswith('Z')
    assert client.get(URL,params={'kind':'novel','updates':True,'q':'合成'}).json()['total']==1
    assert jobs.run_once(('checks',))
    item=client.get(URL+'/'+rows['items'][0]['id']).json()
    # Either queued kind can execute first, so verify the directory through DB.
    with SessionLocal() as db:
        assert db.scalar(select(Item)) is not None
        task=db.scalar(select(Job).where(Job.kind=='series_sync',Job.status=='completed'))
        detail=client.get(URL+'/'+task.payload['watchlist_entry_id']).json()
        assert len(detail['catalog']['entries'])==1
        assert detail['last_checked_at']
    assert item['series_id']


def test_read_preserves_preferences_and_new_latest_becomes_unread(client,roles,monkeypatch):
    rows=[record()];connector(monkeypatch,rows);sync(client)
    entry=client.get(URL).json()['items'][0]
    ident=entry['id']
    assert client.put(URL+'/'+ident,json={'auto_download':True,'enabled':False}).status_code==200
    assert not client.post(URL+'/'+ident+'/read').json()['has_update']
    rows[0]['latest_content_id']='101'
    sync(client)
    current=client.get(URL).json()['items'][0]
    assert current['has_update'] and current['auto_download'] and not current['enabled']
    assert client.post(URL+'/'+ident+'/check').status_code==409


def test_empty_success_marks_remote_removed_but_retains_local_series(client,roles,monkeypatch):
    rows=[record()];connector(monkeypatch,rows);sync(client)
    ident=client.get(URL).json()['items'][0]['id']
    rows.clear();sync(client)
    assert client.get(URL).json()['total']==0
    with SessionLocal() as db:
        row=db.get(PixivWatchlist,ident)
        assert row and not row.present_remote and row.series_id


def test_failed_snapshot_does_not_erase_previous_list(client,roles,monkeypatch):
    connector(monkeypatch,[record()]);sync(client)
    before=client.get(URL).json()['items']
    def fail(**kw):raise ConnectorError('合成接口故障')
    monkeypatch.setattr(jobs,'get_connector',lambda *a,**kw:SimpleNamespace(watchlist=fail))
    job=client.post(URL+'/sync').json()['id']
    # An earlier series task may precede the newly queued snapshot.
    for _ in range(4):
        jobs.run_once(('checks',))
        if client.get('/api/jobs/'+job).json()['status']=='failed':break
    assert client.get('/api/jobs/'+job).json()['status']=='failed'
    after=client.get(URL).json()
    assert after['items'][0]['id']==before[0]['id']
    assert after['sync']['last_error']=='合成接口故障'


def test_main_account_change_during_snapshot_cannot_write(client,roles,monkeypatch):
    def change():
        result=client.put('/api/sources/pixiv/account-roles',json={'enabled':True,'main_account_id':roles['details'],
            'content_account_ids':[roles['auto']]})
        assert result.status_code==200
    connector(monkeypatch,[record()],hook=change)
    job=client.post(URL+'/sync').json()['id'];assert jobs.run_once(('checks',))
    assert client.get('/api/jobs/'+job).json()['status']=='failed'
    with SessionLocal() as db:assert not db.scalars(select(PixivWatchlist)).all()


def test_watchlist_auto_download_uses_automatic_body_lane(client,roles,monkeypatch):
    connector(monkeypatch,[record()]);sync(client)
    row=client.get(URL).json()['items'][0]
    client.put(URL+'/'+row['id'],json={'auto_download':True}).raise_for_status()
    assert jobs.run_once(('checks',))
    with SessionLocal() as db:
        task=db.scalar(select(Job).where(Job.kind=='import'))
        assert task.payload['download'] is True
        assert queue_control.job_lane(task)=='download'


def test_local_pause_during_catalog_network_preserves_library(client,roles,monkeypatch):
    connector(monkeypatch,[record()]);sync(client)
    row=client.get(URL).json()['items'][0]
    client.put(URL+'/'+row['id'],json={'enabled':False}).raise_for_status()
    assert jobs.run_once(('checks',))
    with SessionLocal() as db:assert not db.scalars(select(Item)).all()


def test_enabling_auto_download_checks_existing_catalog_immediately(client,roles,monkeypatch):
    connector(monkeypatch,[record()]);sync(client)
    assert jobs.run_once(('checks',))
    row=client.get(URL).json()['items'][0]
    client.put(URL+'/'+row['id'],json={'auto_download':True}).raise_for_status()
    assert client.get(URL).json()['sync']['pending_tasks']==1


def test_follow_sync_also_queues_watchlist_and_periodic_scheduler_deduplicates(client,roles,monkeypatch):
    monkeypatch.setattr(jobs,'get_connector',lambda *a,**kw:SimpleNamespace(follows=lambda **kw:[],supports_watchlist=True))
    with SessionLocal() as db:root=jobs.enqueue(db,'source_sync',{'platform':'pixiv','account_id':roles['main']}).id
    assert jobs.run_once(('checks',))
    assert client.get('/api/jobs/'+root).json()['result']['watchlist_job_id']
    with SessionLocal() as db:
        state=watchlist.read_state(db);state['next_check_at']=(utcnow()-timedelta(minutes=1)).isoformat()
        watchlist.save_state(db,state);db.commit()
        jobs.schedule_subscriptions(db)
        assert len([j for j in db.scalars(select(Job)) if j.payload.get('watchlist')])==1


def test_no_account_and_invalid_input_fail_without_remote_access(client):
    assert client.get(URL).json()['sync']['configured'] is False
    assert client.post(URL+'/sync').status_code==409
    assert client.get(URL,params={'kind':'illust'}).status_code==422
    assert client.put(URL+'/settings',json={'enabled':True,'interval_minutes':1}).status_code==422
    client.post('/api/auth/logout')
    assert client.get(URL).status_code==401


def adapter(documents):
    instance=PixivConnector({'refresh_token':'synthetic'})
    def api():
        instance._authenticated_user_id='77'
        return SimpleNamespace(no_auth_requests_call=lambda method,url,params,req_auth:documents[url.rsplit('/',1)[-1]](params),parse_result=lambda doc:doc)
    instance._api=api
    return instance


def wire(ident=42):
    return {'id':ident,'title':'合成系列','published_content_count':2,'latest_content_id':100,
            'last_published_content_datetime':'2026-10-16T10:00:00+09:00','user':{'id':77,'name':'合成作者'}}


def test_adapter_reads_complete_pages_and_keeps_kind_separate():
    seen=[]
    def novels(params):
        seen.append(params)
        return {'series':[wire(43 if params.get('offset') else 42)],'next_url':None if params.get('offset') else 'https://app-api.pixiv.net/v1/watchlist/novel?limit=30&offset=30'}
    result=adapter({'novel':novels,'manga':lambda p:{'series':[wire()],'next_url':None}}).watchlist()
    assert [(r['kind'],r['remote_id']) for r in result]==[('novel','42'),('novel','43'),('manga','42')]
    assert seen[1]=={'offset':30,'limit':30,'filter':'for_ios'}


@pytest.mark.parametrize('url',[
 'https://evil.invalid/v1/watchlist/novel?offset=30',
 'https://app-api.pixiv.net/v1/watchlist/manga?offset=30',
 'https://app-api.pixiv.net/v1/watchlist/novel?offset=30&user_id=99',
 'https://app-api.pixiv.net/v1/watchlist/novel?offset=30&offset=60',
 'https://app-api.pixiv.net/v1/watchlist/novel?offset=30&filter=for_android',
 'https://app-api.pixiv.net/v1/watchlist/novel?offset=0',
])
def test_adapter_rejects_untrusted_watchlist_cursors(url):
    with pytest.raises(ConnectorError):adapter({'novel':lambda p:{'series':[wire()],'next_url':url}}).watchlist()


@pytest.mark.parametrize('doc',[{}, {'series':[]}, {'series':'bad','next_url':None},
 {'series':[{'id':42}],'next_url':None}, {'series':[wire(),wire()],'next_url':None}])
def test_adapter_never_treats_malformed_response_as_empty_snapshot(doc):
    with pytest.raises(ConnectorError):adapter({'novel':lambda p:doc}).watchlist()


def test_device_can_browse_and_mark_read_but_cannot_change_download_policy(client,roles,monkeypatch):
    from test_devices import bind, bearer
    connector(monkeypatch,[record()]);sync(client)
    row=client.get(URL).json()['items'][0]
    headers=bearer(bind(client))
    assert client.get(URL,headers=headers).status_code==200
    assert client.get(URL+'/'+row['id'],headers=headers).status_code==200
    assert client.post(URL+'/'+row['id']+'/read',headers=headers).status_code==200
    assert client.put(URL+'/'+row['id'],json={'auto_download':True},headers=headers).status_code==403
