"""Bounded reads and database-maintained queue indexes retain API semantics."""
from sqlalchemy import event,select,update,inspect
from app.database import SessionLocal,engine
from app.models import Item,ItemTag,Job,ReadingProgress,ItemFavorite
from app import jobs,queue_control
from app.library import import_files,save_preview_cover,set_tags
from test_library import client,png


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
