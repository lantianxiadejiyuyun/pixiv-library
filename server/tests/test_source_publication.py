from datetime import datetime, timezone
from dataclasses import replace

import pytest
from sqlalchemy import select, text, inspect
from alembic import command
from alembic.config import Config
from pathlib import Path

from app.database import SessionLocal, engine
from app.integrations.pixiv import PixivConnector
from app.source_dates import source_timestamp
from app import jobs
from app.models import Item, ItemFavorite, Job, ReadingProgress
from app.library import import_files
from test_library import client, png


@pytest.mark.parametrize('novel', [False, True])
def test_pixiv_original_publication_uses_timezone_not_library_dates(novel):
    data = {'id': 42, 'title': 'Synthetic publication', 'type': 'illust', 'create_date': '2020-02-29T08:15:20+09:00'}
    work = PixivConnector._metadata_work(data, novel=novel)
    assert work.source_published_at == datetime(2020, 2, 28, 23, 15, 20)


@pytest.mark.parametrize('value', [None, '', '2020-01-01', '2020-01-01T00:00:00', 'bad', True, 1234,
                                  '2020-02-30T00:00:00Z', '2020-01-01T00:00:00+25:00'])
def test_missing_or_invalid_dates_never_become_import_time(value):
    assert source_timestamp(value) is None


def test_publication_merge_preserves_known_value_and_manual_data(client):
    with SessionLocal() as db:
        data = {'id': 42, 'title': 'Synthetic title', 'type': 'illust', 'create_date': '2021-04-05T12:30:00Z'}
        work = PixivConnector._metadata_work(data, novel=False)
        record = jobs.merge_remote_item(db, 'pixiv', work)
        record.title = 'My edited title'; record.user_score = 5
        record.favorite = ItemFavorite(); record.progress = ReadingProgress(offset=.4)
        db.commit(); created = record.created_at
        jobs.merge_remote_item(db, 'pixiv', replace(work, source_published_at=None))
        db.commit()
        assert record.source_published_at == datetime(2021,4,5,12,30)
        assert record.created_at == created and record.title == 'My edited title'
        assert record.user_score == 5 and record.progress.offset == .4 and record.favorite
        ident = record.id
    view = client.get('/api/items/'+ident).json()
    assert view['source_published_at'] == '2021-04-05T12:30:00Z'
    assert view['source_published_at'] != view['created_at']
    listed = client.get('/api/items').json()['items']
    assert listed[0]['source_published_at'] == view['source_published_at']


def test_waiting_rerun_backfills_downloaded_work_without_changing_files(client, monkeypatch):
    jobs._stop.clear()
    with SessionLocal() as db:
        record = import_files(db, [('page.png',png())], title='Keep title', source='pixiv', source_id='42')
        ident = record.id; asset = (record.assets[0].id, record.assets[0].sha256, record.assets[0].relative_path)
        created = record.created_at
        job = jobs.enqueue_import(db, {'platform':'pixiv','remote_id':'42','item_id':ident,'download':False,'refresh_metadata':True})
        job_id = job.id
    class Connector:
        def normalize_id(self, value): return value
        def fetch(self, value, **kwargs):
            return PixivConnector._metadata_work({'id':42,'title':'Remote title','create_date':'2019-05-06T18:00:00+09:00'},novel=False)
    monkeypatch.setattr(jobs, 'get_connector', lambda *args: Connector())
    assert jobs.run_once(('details',))
    with SessionLocal() as db:
        record = db.get(Item,ident)
        assert db.get(Job,job_id).status == 'completed'
        assert record.source_published_at == datetime(2019,5,6,9)
        assert record.title == 'Keep title' and record.created_at == created
        assert (record.assets[0].id,record.assets[0].sha256,record.assets[0].relative_path) == asset


def test_existing_schema_upgrade_keeps_old_rows_unknown_and_downgrades(client):
    with SessionLocal() as db:
        db.add(Item(id='old',title='Old row'));db.commit()
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE items DROP COLUMN source_published_at'))
    config = Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    config.attributes['database_url'] = str(engine.url)
    command.stamp(config, 'd97b164ea031')
    command.upgrade(config, 'ea794db01c82')
    with engine.connect() as connection:
        assert connection.execute(text("SELECT title,source_published_at FROM items WHERE id='old'")).one() == ('Old row', None)
    command.downgrade(config, 'd97b164ea031')
    assert 'source_published_at' not in {c['name'] for c in inspect(engine).get_columns('items')}
    command.upgrade(config, 'ea794db01c82')
