from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import create_engine, select
from sqlalchemy.pool import QueuePool

from app import jobs, library
from app.database import SessionLocal, engine, worker_pool_options
from app.integrations.base import RemoteWork
from app.models import Item, ItemTag, Job
from test_library import client


def test_mysql_collation_aliases_link_once_and_preserve_other_origins(client, monkeypatch):
    original = library.ensure_tag
    monkeypatch.setattr(library, 'ensure_tag', lambda db, name: original(db, name.casefold()))
    with SessionLocal() as db:
        work = RemoteWork(remote_id='42', title='Synthetic', source_url='https://example.invalid/42',
                          tags=['Warhammer40k', 'warhammer40k'], metadata_only=True)
        item = jobs.merge_remote_item(db, 'pixiv', work)
        db.commit()
        assert len(db.scalars(select(ItemTag).where(ItemTag.item_id == item.id)).all()) == 1
        library.set_tags(db, item, ['Warhammer40k', 'warhammer40k'], 'manual')
        db.commit()
        library.set_tags(db, item, ['Warhammer40k', 'warhammer40k'], 'source')
        db.commit()
        links = db.scalars(select(ItemTag).where(ItemTag.item_id == item.id)).all()
        assert sorted(link.origin for link in links) == ['manual', 'source']


def test_checkpoint_does_not_reopen_outer_read_transaction_during_network(client, monkeypatch):
    jobs._stop.clear()
    checked_out = []

    class Connector:
        def normalize_id(self, value):
            return value

        def fetch(self, value, *, download, checkpoint):
            checkpoint(30)
            checked_out.append(engine.pool.checkedout())
            checkpoint(60)
            checked_out.append(engine.pool.checkedout())
            return RemoteWork(remote_id=value, title='Synthetic', source_url='https://example.invalid/42', metadata_only=True)

    monkeypatch.setattr(jobs, 'get_connector', lambda *args: Connector())
    with SessionLocal() as db:
        ident = jobs.enqueue(db, 'import', {'platform': 'jm', 'remote_id': '42', 'download': False}).id
    assert jobs.run_once(('details',))
    assert checked_out == [0, 0]
    with SessionLocal() as db:
        assert db.get(Job, ident).status == 'completed'


def test_worker_pool_has_headroom_for_eight_nested_checkpoint_sessions(tmp_path):
    options = {**worker_pool_options(), 'pool_timeout': 1}
    pool_engine = create_engine('sqlite:///' + str(tmp_path / 'pool.db'), poolclass=QueuePool,
                                connect_args={'check_same_thread': False}, **options)
    outer_ready, inner_ready = Barrier(8), Barrier(8)

    def task(_):
        with pool_engine.connect():
            outer_ready.wait(timeout=5)
            with pool_engine.connect():
                inner_ready.wait(timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(task, range(8)))
        assert pool_engine.pool.checkedout() == 0
        assert pool_engine.pool.checkedin() == 2
    finally:
        pool_engine.dispose()
