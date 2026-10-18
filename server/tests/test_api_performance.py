"""Bounded reads and database-maintained queue indexes retain API semantics."""
from sqlalchemy import event,select,update,inspect
from app.database import SessionLocal,engine
from app.models import Item,ItemTag,Job,ReadingProgress,ItemFavorite
from app import jobs,queue_control
from app.library import import_files,save_preview_cover,set_tags
from test_library import client,png
import pytest


def test_generated_lane_updates_for_orm_and_bulk_payload_changes(client):
    with SessionLocal() as db:
        row=jobs.enqueue_import(db,{'platform':'pixiv','remote_id':'42','download':False})
        ident=row.id
        assert db.scalar(select(Job.queue_lane).where(Job.id==ident))=='details'
        row.payload={**row.payload,'download':True};db.commit()
        assert db.scalar(select(Job.queue_lane).where(Job.id==ident))=='download'
        db.execute(update(Job).where(Job.id==ident).values(payload={**row.payload,'download_origin':'manual'}));db.commit()
        assert db.scalar(select(Job.queue_lane).where(Job.id==ident))=='manual_download'
        db.execute(update(Job).where(Job.id==ident).values(payload={'platform':'pixiv','retry_queue':True}));db.commit()
        assert db.scalar(select(Job.queue_lane).where(Job.id==ident))=='retry'
    assert 'ix_jobs_lane_claim' in {i['name'] for i in inspect(engine).get_indexes('jobs')}


def test_preview_cover_never_loads_page_assets_tags_or_progress(client):
    with SessionLocal() as db:
        item=import_files(db,[('page.png',png())],title='Synthetic')
        item.preview_cover=save_preview_cover(('preview.png',png()),'local')
        item.progress=ReadingProgress(offset=.4);item.favorite=ItemFavorite()
        set_tags(db,item,['Synthetic'],'manual');db.commit();ident=item.id
    selects=[]
    def trace(_conn,_cursor,statement,_params,_context,_many):
        if statement.lstrip().upper().startswith('SELECT'):selects.append(statement)
    event.listen(engine,'before_cursor_execute',trace)
    try:
        response=client.get('/api/items/'+ident+'/cover')
        assert response.status_code==200
    finally:event.remove(engine,'before_cursor_execute',trace)
    assert len(selects)==1
    assert 'item_tags' not in selects[0] and 'assets' not in selects[0]


def test_tag_page_filters_source_before_offset_and_counts_unique_works(client):
    with SessionLocal() as db:
        for source,names in [('local',['A','B','D']),('pixiv',['A','C','D'])]:
            item=Item(title='Synthetic',source=source);db.add(item);db.flush()
            set_tags(db,item,names,'manual')
            set_tags(db,item,names,'source')
        db.commit()
    all_tags=client.get('/api/tags',params={'source':'pixiv'}).json()
    assert [r['name'] for r in all_tags]==['A','C','D']
    assert [r['count'] for r in all_tags]==[1,1,1]
    page=client.get('/api/tags',params={'source':'pixiv','offset':1,'limit':1}).json()
    assert page==[all_tags[1]]
    assert client.get('/api/tags',params={'q':'D','limit':1}).json()[0]['count']==2


def test_dashboard_untagged_counts_items_not_tag_origins(client):
    with SessionLocal() as db:
        item=Item(title='Tagged');db.add_all([item,Item(title='Untagged')]);db.flush()
        set_tags(db,item,['A','B'],'manual');set_tags(db,item,['A','B'],'source');db.commit()
    result=client.get('/api/dashboard').json()
    assert result['total_items']==2 and result['untagged_items']==1
    assert result['source_counts']=={'local':2}


@pytest.mark.parametrize('field', ('download_account_id', 'details_account_id', 'check_account_id', 'execution_account_id'))
def test_account_statistics_follow_generated_assignments_without_reading_payloads(client, field):
    from app.models import SourceAccount
    from app.pixiv_accounts import public_config
    with SessionLocal() as db:
        db.add(SourceAccount(id='a',platform='pixiv',label='Synthetic',credentials_encrypted='unused'))
        job=Job(kind='import',status='completed',payload={field:'a'})
        db.add(job);db.commit();ident=job.id
        selects=[]
        def trace(_conn,_cursor,statement,_params,_context,_many):
            if statement.lstrip().upper().startswith('SELECT'):selects.append(statement)
        event.listen(engine,'before_cursor_execute',trace)
        try:
            assert public_config(db)['download_accounts'][0]['completed']==1
        finally:event.remove(engine,'before_cursor_execute',trace)
        stats=[sql for sql in selects if 'FROM jobs' in sql]
        assert len(stats)==1 and 'JSON_EXTRACT' not in stats[0] and 'jobs.payload' not in stats[0]
        db.execute(update(Job).where(Job.id==ident).values(payload={field:'a'*100}));db.commit()
        assert db.scalar(select(getattr(Job,field)).where(Job.id==ident)) is None
        assert public_config(db)['download_accounts'][0]['completed']==0
        db.execute(update(Job).where(Job.id==ident).values(payload={field:None}));db.commit()
        assert db.scalar(select(getattr(Job,field)).where(Job.id==ident)) is None
        db.execute(update(Job).where(Job.id==ident).values(status='running',payload={field:'a','details_account_id':'a'}));db.commit()
        account=public_config(db)['download_accounts'][0]
        assert account['running']==1 and account['completed']==0


def test_tag_search_pagination_matches_unbounded_translation_and_literal_wildcards(client):
    from app.models import Tag
    with SessionLocal() as db:
        for source,names in [('local',['A','B','D','%_literal']),('pixiv',['A','C','D','%_literal'])]:
            item=Item(title='Synthetic',source=source);db.add(item);db.flush()
            set_tags(db,item,names,'manual');set_tags(db,item,names,'source')
        db.scalar(select(Tag).where(Tag.name=='D')).name_zh='中文译名'
        db.commit()
    for source in ('','local','pixiv','absent'):
        for query in ('','中文','%_','missing'):
            full=client.get('/api/tags',params={'source':source,'q':query}).json()
            pages=[]
            for offset in range(len(full)+1):
                page=client.get('/api/tags',params={'source':source,'q':query,'limit':1,'offset':offset}).json()
                pages.extend(page)
            assert pages==full


def test_task_history_preserves_tie_order_and_lane_filter(client):
    from datetime import datetime,timedelta
    with SessionLocal() as db:
        for index in range(230):
            db.add(Job(id=f'{index:036}',kind='import',status='completed',created_at=datetime(2026,1,1)+timedelta(seconds=index//2),payload={'platform':'pixiv','download':False}))
        db.add(Job(id='manual',kind='import_content',payload={'download_origin':'manual'}))
        db.commit()
    result=client.get('/api/jobs',params={'queue':'details'}).json()['items']
    expected=sorted(range(230),key=lambda index:(-(index//2),index))[:200]
    assert [row['id'] for row in result]==[f'{index:036}' for index in expected]
    assert all(row['queue']=='details' for row in result)


def test_collection_saved_count_counts_works_not_pages(client):
    with SessionLocal() as db:
        import_files(db,[('a.png',png()),('b.png',png())],source='pixiv',title='Synthetic')
        db.add(Item(title='Metadata',source='pixiv'));db.commit()
    collection=next(row for row in client.get('/api/library/collections').json() if row['source']=='pixiv')
    assert collection['item_count']==2 and collection['saved_item_count']==1
    assert collection['metadata_item_count']==1 and collection['page_count']==2


def test_account_counts_deduplicate_all_roles_and_keep_distinct_accounts(client):
    from app.models import SourceAccount
    from app.pixiv_accounts import public_config
    from app.job_account_sql import ACCOUNT_FIELDS
    with SessionLocal() as db:
        db.add_all([SourceAccount(id=ident,platform='pixiv',label=ident,credentials_encrypted='unused') for ident in ('a','b')])
        job=Job(kind='import',status='completed');db.add(job);db.commit()
        for values in [('a','a','a','a'),('a','b','b','a'),(None,'a','a','b'),(None,None,'a','a'),(None,None,None,'b')]:
            for status in ('running','completed','failed'):
                db.execute(update(Job).where(Job.id==job.id).values(payload=dict(zip(ACCOUNT_FIELDS,values)),status=status));db.commit()
                accounts=public_config(db)['download_accounts']
                for account in accounts:
                    for state in ('running','completed','failed'):
                        assert account[state]==int(state==status and account['id'] in values)
