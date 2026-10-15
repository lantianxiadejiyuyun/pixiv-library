"""Automatic cover repair must remain bounded and separate from novel content."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import JSON, select, text

from app import jobs, queue_control
from app.database import SessionLocal
from app.integrations.base import RemoteWork
from app.library import import_files, save_preview_cover, set_tags
from app.models import Item, Job, ReadingProgress, Subscription, utcnow
from test_library import client, png
from test_pixiv_accounts import accounts, enable


def scheduler():
    from app.novel_covers import NovelCoverScheduler
    return NovelCoverScheduler()


def novel(db, number, **changes):
    values = dict(id=f'01900000-0000-4000-8000-{number:012d}', title=f'小说 {number}',
                  source='pixiv', source_id=f'novel:{number}', type='novel',
                  created_at=datetime(2026, 1, 1) + timedelta(seconds=number))
    values.update(changes)
    item = Item(**values)
    db.add(item)
    db.flush()
    return item


def auto_job(db, item, *, status='queued', age=None, **fields):
    payload = dict(platform='pixiv', remote_id=item.source_id, item_id=item.id,
                   download=False, auto_novel_cover=True)
    payload.update(fields)
    job = Job(kind='import', status=status, payload=payload)
    if age is not None:
        job.created_at = job.updated_at = utcnow() - age
    db.add(job)
    db.flush()
    return job


class FakeConnector:
    def __init__(self, preview=True):
        self.calls = []
        self.preview = preview

    def normalize_id(self, value):
        return value

    def fetch(self, ident, *, download=True, checkpoint=lambda value: None, **_kwargs):
        self.calls.append((ident, download))
        assert download is False, 'Automatic cover repair must never request novel text'
        checkpoint(50)
        return RemoteWork(remote_id=ident, title='远端标题', item_type='novel',
                          source_url='https://www.pixiv.net/novel/show.php?id=' + ident.split(':')[-1],
                          author='远端作者', description='远端简介', tags=['远端标签'],
                          metadata_only=True, preview_cover=('cover.png', png()) if self.preview else None)


@pytest.fixture
def connector(client, monkeypatch):
    value = FakeConnector()
    jobs._stop.clear()
    queue_control.update_settings({'rate_limit_enabled': False})
    monkeypatch.setattr(jobs, 'get_connector', lambda *args, **kwargs: value)
    return value


def test_scan_only_eligible_pixiv_text_and_all_missing_json_representations(client, accounts):
    with SessionLocal() as db:
        expected = [novel(db, 1), novel(db, 2, preview_cover=JSON.NULL),
                    novel(db, 3, preview_cover={}), novel(db, 4, type='article')]
        db.execute(text('UPDATE items SET preview_cover = NULL WHERE id = :id'), {'id': expected[0].id})
        novel(db, 5, source='local')
        novel(db, 6, source='jm')
        novel(db, 7, type='manga')
        novel(db, 8, source_id='888')
        novel(db, 9, source_id=None)
        novel(db, 10, preview_cover={'relative_path': 'existing-cover.webp'})
        expected_ids = {item.id for item in expected}
        db.commit()
    assert scheduler().scan() == 4
    with SessionLocal() as db:
        queued = db.scalars(select(Job)).all()
        assert {job.payload['item_id'] for job in queued} == expected_ids
        assert all(job.kind == 'import' and job.payload['download'] is False and
                   job.payload['auto_novel_cover'] is True and queue_control.job_lane(job) == 'details'
                   for job in queued)


@pytest.mark.parametrize('pause', ['master', 'details', 'download'])
def test_scan_respects_network_and_details_pause_only(client, accounts, pause):
    with SessionLocal() as db:
        novel(db, 1)
        db.commit()
    if pause == 'master':
        queue_control.update_settings({'paused': True})
    else:
        queue_control.update_lane(pause, {'paused': True})
    worker = scheduler()
    assert worker.scan() == (1 if pause == 'download' else 0)
    if pause == 'master':
        queue_control.update_settings({'paused': False})
    elif pause == 'details':
        queue_control.update_lane('details', {'paused': False})
    if pause != 'download':
        assert worker.scan() == 1


def test_scan_without_pixiv_account_never_creates_failures(client):
    with SessionLocal() as db:
        novel(db, 1)
        db.commit()
    assert scheduler().scan() == 0
    with SessionLocal() as db:
        assert db.query(Job).count() == 0


@pytest.mark.parametrize('kind,status', [('import', 'queued'), ('import', 'running'),
                                        ('import_content', 'queued'), ('import_content', 'running')])
def test_scan_deduplicates_existing_manual_acquisition(client, accounts, kind, status):
    with SessionLocal() as db:
        item = novel(db, 1)
        job = Job(kind=kind, status=status, payload={'platform': 'pixiv', 'remote_id': item.source_id,
                                                   'item_id': item.id, 'download': True})
        db.add(job)
        db.commit()
        job_id = job.id
    assert scheduler().scan() == 0
    with SessionLocal() as db:
        assert db.query(Job).count() == 1
        assert db.get(Job, job_id).payload['download'] is True


def test_scan_caps_backlog_and_is_idempotent_across_scheduler_instances(client, accounts):
    with SessionLocal() as db:
        for number in range(1, 48):
            novel(db, number)
        db.commit()
    first, second = scheduler(), scheduler()
    assert first.scan() == 20
    assert second.scan() == 0
    assert first.scan() == 0
    with SessionLocal() as db:
        queued = db.scalars(select(Job)).all()
        assert len(queued) == 20
        for index, job in enumerate(queued[:3]):
            job.status = 'running' if index == 0 else 'completed'
            job.updated_at = utcnow()
        db.commit()
    assert first.scan() == 2  # Running work still occupies the automatic backlog cap.
    with SessionLocal() as db:
        assert db.query(Job).filter(Job.status.in_(('queued', 'running'))).count() == 20


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_terminal_automatic_job_cools_down_then_reuses_identity_and_account(client, accounts, status):
    with SessionLocal() as db:
        item = novel(db, 1)
        previous = auto_job(db, item, status=status, age=timedelta(hours=23),
                            details_account_id=accounts['content'], execution_account_id=accounts['main'])
        previous.error, previous.result, previous.progress = 'synthetic error', {'cover_saved': False}, 100
        previous.attempts = 2
        previous_id = previous.id
        db.commit()
    assert scheduler().scan() == 0
    with SessionLocal() as db:
        db.get(Job, previous_id).updated_at = utcnow() - timedelta(hours=25)
        db.commit()
    assert scheduler().scan() == 1
    with SessionLocal() as db:
        assert db.query(Job).count() == 1
        retried = db.get(Job, previous_id)
        assert retried.status == 'queued' and retried.payload['download'] is False
        assert retried.payload['details_account_id'] == accounts['content']
        assert retried.payload['execution_account_id'] == accounts['main']
        assert retried.attempts == 2


def test_cursor_reaches_missing_novel_after_200_recently_skipped_items(client, accounts):
    with SessionLocal() as db:
        for number in range(1, 202):
            item = novel(db, number)
            if number <= 200:
                auto_job(db, item, status='completed', age=timedelta(hours=1))
        wanted = item.id
        db.commit()
    worker = scheduler()
    assert sum(worker.scan() for _ in range(3)) == 1
    with SessionLocal() as db:
        queued = db.scalars(select(Job).where(Job.status == 'queued')).all()
        assert len(queued) == 1 and queued[0].payload['item_id'] == wanted


def test_downloaded_novel_auto_cover_preserves_body_personal_metadata_and_progress(client, accounts, connector):
    original = '原始正文第一段\n\n原始正文第二段'.encode()
    with SessionLocal() as db:
        item = import_files(db, [('novel.txt', original)], title='人工标题', item_type='novel',
                            source='pixiv', source_id='novel:123', author='人工作者', description='人工简介')
        item.title_zh, item.rating, item.rating_origin = '人工汉化', 'safe', 'manual'
        item.title_zh_source = item.title
        set_tags(db, item, ['人工标签'], origin='manual')
        db.add(ReadingProgress(item_id=item.id, page=0, section=0, offset=.375))
        subscription = Subscription(platform='pixiv', target_type='author', target='123', label='订阅',
                                    enabled=True, auto_download=False, next_check_at=utcnow() + timedelta(days=1))
        db.add(subscription)
        db.commit()
        item_id, subscription_id = item.id, subscription.id
    assert client.put(f'/api/items/{item_id}/favorite', json={'favorite': True, 'score': 5}).status_code == 200
    before = client.get(f'/api/items/{item_id}').json()
    assert scheduler().scan() == 1
    assert jobs.run_once(('details',))
    assert connector.calls == [('novel:123', False)]
    after = client.get(f'/api/items/{item_id}').json()
    assert after['cover_url'] and client.get(after['cover_url']).status_code == 200
    for field in ('assets', 'title', 'title_zh', 'author', 'description', 'tags', 'rating',
                  'is_favorite', 'user_score', 'favorite_group_ids', 'progress', 'content_state'):
        assert after[field] == before[field], field
    assert client.get(after['assets'][0]['url']).content == original
    with SessionLocal() as db:
        assert db.query(Job).count() == 1
        assert not db.get(Subscription, subscription_id).auto_download
    assert scheduler().scan() == 0


def test_optional_cover_failure_is_not_immediately_requeued(client, accounts, connector):
    connector.preview = False
    with SessionLocal() as db:
        novel(db, 1)
        db.commit()
    assert scheduler().scan() == 1
    assert jobs.run_once(('details',))
    with SessionLocal() as db:
        job = db.scalar(select(Job))
        assert job.status == 'completed' and job.result['cover_saved'] is False
    assert scheduler().scan() == 0
    assert connector.calls == [('novel:1', False)]


@pytest.mark.parametrize('change', ['cover', 'delete'])
def test_automatic_job_skips_network_if_item_was_covered_or_deleted(client, accounts, connector, change):
    with SessionLocal() as db:
        item_id = novel(db, 1).id
        db.commit()
    assert scheduler().scan() == 1
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        if change == 'delete':
            db.delete(item)
        else:
            item.preview_cover = save_preview_cover(('manual.png', png('blue')), 'pixiv')
            saved_cover = dict(item.preview_cover)
        db.commit()
    assert jobs.run_once(('details',))
    assert connector.calls == []
    with SessionLocal() as db:
        assert db.scalar(select(Job)).status == 'completed'
        item = db.get(Item, item_id)
        if change == 'delete':
            assert item is None
        else:
            assert item.preview_cover == saved_cover


def test_cover_added_during_remote_fetch_is_retained(client, accounts, connector, monkeypatch):
    with SessionLocal() as db:
        item_id = novel(db, 1).id
        db.commit()
    assert scheduler().scan() == 1
    saved_cover = save_preview_cover(('concurrent.png', png('blue')), 'pixiv')
    fetch = connector.fetch

    def concurrent_cover(*args, **kwargs):
        with SessionLocal() as db:
            db.get(Item, item_id).preview_cover = saved_cover
            db.commit()
        return fetch(*args, **kwargs)

    monkeypatch.setattr(connector, 'fetch', concurrent_cover)
    assert jobs.run_once(('details',))
    with SessionLocal() as db:
        assert db.get(Item, item_id).preview_cover == saved_cover
        assert db.scalar(select(Job)).status == 'completed'


def test_user_upgraded_automatic_job_downloads_body_and_is_not_reused_as_cover_retry(client, accounts, connector, monkeypatch):
    with SessionLocal() as db:
        item_id = novel(db, 1).id
        db.commit()
    assert scheduler().scan() == 1
    preview = save_preview_cover(('already-present.png', png('blue')), 'pixiv')
    original_text = '用户明确要求下载的小说正文'.encode()
    calls = []

    def body_fetch(ident, *, download=True, checkpoint=lambda value: None, **_kwargs):
        calls.append((ident, download))
        assert download is True
        checkpoint(50)
        return RemoteWork(remote_id=ident, title='原小说', item_type='novel',
                          source_url='https://www.pixiv.net/novel/show.php?id=1',
                          metadata_only=False, files=[('novel.txt', original_text)])

    monkeypatch.setattr(connector, 'fetch', body_fetch)
    with SessionLocal() as db:
        original_job_id = db.scalar(select(Job.id))
        db.get(Item, item_id).preview_cover = preview
        db.commit()
        upgraded = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': 'novel:1',
                                           'item_id': item_id, 'download': True})
        assert upgraded.id == original_job_id
        assert upgraded.payload['download'] is True
        assert upgraded.payload['auto_novel_cover'] is True
        assert queue_control.job_lane(upgraded) == 'download'
    assert scheduler().scan() == 0
    assert jobs.run_once(('download',))
    assert calls == [('novel:1', True)]
    after = client.get(f'/api/items/{item_id}').json()
    assert after['content_state'] == 'ready' and len(after['assets']) == 1
    assert client.get(after['assets'][0]['url']).content == original_text
    with SessionLocal() as db:
        assert db.get(Item, item_id).preview_cover == preview
        original_job = db.get(Job, original_job_id)
        assert original_job.status == 'completed'
        original_job.updated_at = utcnow() - timedelta(hours=25)
        db.get(Item, item_id).preview_cover = None
        db.commit()
    assert scheduler().scan() == 1
    with SessionLocal() as db:
        assert db.query(Job).count() == 2
        original_job = db.get(Job, original_job_id)
        assert original_job.status == 'completed' and original_job.payload['download'] is True
        repair = db.scalar(select(Job).where(Job.status == 'queued'))
        assert repair.id != original_job_id and repair.payload['download'] is False


def test_automatic_repair_uses_details_pool_and_account_slots(client, accounts, connector):
    enable(client, accounts)
    with SessionLocal() as db:
        novel(db, 1)
        db.commit()
    assert scheduler().scan() == 1
    with SessionLocal() as db:
        auto_id = db.scalar(select(Job.id))
        blockers = [Job(kind='import', status='running', worker_id=f'busy-{index}',
                        lease_until=utcnow() + timedelta(minutes=5),
                        payload={'platform': 'pixiv', 'remote_id': f'novel:{900 + index}', 'download': False,
                                 'details_account_id': accounts['main']}) for index in range(2)]
        db.add_all(blockers)
        db.commit()
        blocker_ids = [job.id for job in blockers]
    assert jobs.run_once(('download',)) is False
    assert jobs.run_once(('details',)) is False
    assert connector.calls == []
    with SessionLocal() as db:
        for ident in blocker_ids:
            db.get(Job, ident).status = 'completed'
        db.commit()
    assert jobs.run_once(('details',))
    with SessionLocal() as db:
        job = db.get(Job, auto_id)
        assert job.status == 'completed'
        assert job.payload['details_account_id'] == accounts['main']
        assert 'download_account_id' not in job.payload


def test_tick_throttles_success_and_failure_without_leaking_exception_text(monkeypatch, caplog):
    from app import novel_covers
    clock = [1000.0]
    monkeypatch.setattr(novel_covers.time, 'monotonic', lambda: clock[0])
    worker = scheduler()
    calls = []

    def fail():
        calls.append(clock[0])
        raise RuntimeError('synthetic-password-and-private-url-must-not-appear')

    monkeypatch.setattr(worker, 'scan', fail)
    worker.tick()
    clock[0] += 59
    worker.tick()
    assert len(calls) == 1
    clock[0] += 1
    worker.tick()
    assert len(calls) == 2
    assert caplog.records
    assert 'synthetic-password-and-private-url-must-not-appear' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    monkeypatch.setattr(worker, 'scan', lambda: calls.append(clock[0]) or 0)
    clock[0] += 60
    worker.tick()
    worker.tick()
    assert len(calls) == 3
