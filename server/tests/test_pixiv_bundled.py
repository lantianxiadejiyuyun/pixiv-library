from sqlalchemy import select
from test_library import client, png
from app import jobs, queue_control
from app.database import SessionLocal
from app.models import Item, Job
from app.integrations.base import ConnectorError, RemoteWork


class Combined:
    def __init__(self, callback=None): self.calls, self.callback = [], callback
    def normalize_id(self, ident): return ident
    def fetch(self, ident, *, download=True, checkpoint=lambda value: None):
        self.calls.append(download)
        if self.callback: self.callback()
        return RemoteWork(remote_id=ident, title='一起下载', source_url='https://www.pixiv.net/artworks/123',
            remote_page_count=1, files=[('1.png', png())] if download else [], metadata_only=not download)


def prepare(monkeypatch, connector):
    jobs._stop.clear()
    queue_control.update_settings({'request_interval_seconds': 0})
    monkeypatch.setattr(jobs, 'get_connector', lambda *args: connector)


def test_single_account_fetches_details_and_body_once_in_one_job(client, monkeypatch):
    connector = Combined(); prepare(monkeypatch, connector)
    with SessionLocal() as db:
        ident = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123'}).id
    assert jobs.run_once()
    result = client.get('/api/jobs/' + ident).json()
    assert connector.calls == [True]
    assert result['status'] == 'completed' and result['result']['content_job_id'] == ident
    assert client.get('/api/items/' + result['item_id']).json()['page_count'] == 1
    with SessionLocal() as db:
        assert db.query(Job).count() == 1 and db.query(Item).count() == 1


def test_metadata_only_pixiv_still_does_not_download_body(client, monkeypatch):
    connector = Combined(); prepare(monkeypatch, connector)
    with SessionLocal() as db:
        ident = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123', 'download': False}).id
    assert jobs.run_once()
    assert connector.calls == [False]
    result = client.get('/api/jobs/' + ident).json()
    assert result['label'] is None and result['result']['metadata_only'] is True
    assert client.get('/api/items/' + result['item_id']).json()['assets'] == []
    with SessionLocal() as db: assert db.query(Job).count() == 1
    with SessionLocal() as db:
        old_cover = db.get(Job, ident)
        old_cover.payload = {**old_cover.payload, 'download': True}; db.commit()
    assert client.get('/api/jobs/' + ident).json()['label'] is None  # Historical staged cover remains a cover.


def test_combined_cancellation_does_not_publish_details_or_create_body_job(client, monkeypatch):
    def cancel():
        with SessionLocal() as db:
            job = db.scalar(select(Job).where(Job.status == 'running'))
            job.status = 'cancelled'; db.commit()
    connector = Combined(cancel); prepare(monkeypatch, connector)
    with SessionLocal() as db:
        ident = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '123'}).id
    assert jobs.run_once()
    assert client.get('/api/jobs/' + ident).json()['status'] == 'cancelled'
    with SessionLocal() as db:
        assert db.query(Item).count() == 0 and db.query(Job).count() == 1


def test_existing_metadata_is_downloaded_in_same_job_and_legacy_body_job_still_runs(client, monkeypatch):
    connector = Combined(); prepare(monkeypatch, connector)
    with SessionLocal() as db:
        item = Item(title='人工标题', type='manga', source='pixiv', source_id='123', content_state='metadata')
        db.add(item); db.commit(); item_id = item.id
    request = client.post('/api/items/' + item_id + '/download').json()
    assert jobs.run_once()
    assert connector.calls == [True]
    ready = client.get('/api/items/' + item_id).json()
    assert ready['title'] == '人工标题' and ready['content_state'] == 'ready'
    with SessionLocal() as db:
        assert db.get(Job, request['id']).status == 'completed'
        item = Item(title='旧正文队列', type='manga', source='pixiv', source_id='456', content_state='queued')
        db.add(item); db.flush()
        job = Job(kind='import_content', status='queued', payload={'platform': 'pixiv', 'remote_id': '456', 'item_id': item.id})
        db.add(job); db.commit(); legacy_id = job.id
    assert jobs.run_once()
    assert client.get('/api/jobs/' + legacy_id).json()['status'] == 'completed'
    assert connector.calls == [True, True]
