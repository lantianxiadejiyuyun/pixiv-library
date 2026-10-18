"""Real decode/staging and concurrent HTTP regressions for NAS OOM failures."""
from concurrent.futures import ThreadPoolExecutor
import io
from threading import Event, Lock
import time
import tracemalloc

from PIL import Image
import pytest
from sqlalchemy import select

from app import image_thumbnails, jobs
from app.config import settings
from app.database import SessionLocal
from app.download_staging import DownloadFiles, StagedFile, staging_context
from app.integrations.base import JobCancelled, RemoteWork
from app.library import import_files
from app.models import Asset, Item, Job
from app.storage import resolve_asset_path
from test_integrations import add_account
from test_library import client, png, upload


def test_download_batch_keeps_book_bytes_off_heap_and_uses_one_descriptor(tmp_path, monkeypatch):
    monkeypatch.setenv('LIBRARY_DOWNLOAD_TEMP_DIR', str(tmp_path))
    tracemalloc.start()
    try:
        with staging_context():
            files = DownloadFiles()
            for n in range(64):
                files.append((f'{n}.bin', bytes([n]) * (1024 * 1024)))
            assert all(isinstance(data, StagedFile) for _, data in files)
            assert len({id(data.stream) for _, data in files}) == 1
            assert tracemalloc.get_traced_memory()[1] < 8 * 1024 * 1024
            assert files[23][1].read() == bytes([23]) * 1024 * 1024
            stream = files[0][1].stream
        assert stream.closed
    finally:
        tracemalloc.stop()
    assert not list(tmp_path.iterdir())
    # Small standalone connector consumers keep their existing bytes interface.
    outside = DownloadFiles(); outside.append(('x', b'abc'))
    assert outside == [('x', b'abc')]


@pytest.mark.parametrize('failure', [None, ValueError, JobCancelled])
def test_staging_cleanup_and_nested_context_isolation(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('LIBRARY_DOWNLOAD_TEMP_DIR', str(tmp_path))
    files = DownloadFiles()
    def attempt():
        with staging_context():
            files.append(('first.txt', b'first'))
            with staging_context():
                inner = DownloadFiles(); inner.append(('inner.txt', b'inner'))
            assert inner[0][1].stream.closed
            files.append(('last.txt', b'last'))
            assert files[0][1].stream is files[1][1].stream
            assert files[0][1].read() == b'first'
            assert files[1][1].read() == b'last'
            if failure:
                raise failure()
    if failure:
        with pytest.raises(failure): attempt()
    else:
        attempt()
    assert all(file.stream.closed for _, file in files)
    assert not list(tmp_path.iterdir())


def test_staged_import_validates_all_pages_before_saving_and_normalizes_text(client):
    with staging_context(), SessionLocal() as db:
        files = DownloadFiles()
        files.append(('2.png', png('blue')))
        files.append(('1.png', png('red')))
        files.append(('3.txt', b'\xef\xbb\xbftext'))
        item = import_files(db, files, source='pixiv', source_id='123')
        assert [asset.original_name for asset in item.assets] == ['1.png', '2.png', '3.txt']
        assert [resolve_asset_path(a.relative_path).read_bytes() for a in item.assets] == [png('red'), png('blue'), b'text']
    before = set(settings.storage_root.rglob('*'))
    with staging_context(), SessionLocal() as db:
        files = DownloadFiles()
        files.append(('1.png', png('green')))
        files.append(('2.png', b'invalid'))
        with pytest.raises(ValueError): import_files(db, files, source='pixiv', source_id='456')
        assert db.scalar(select(Item).where(Item.source_id == '456')) is None
    assert before == set(settings.storage_root.rglob('*'))


def test_real_queue_attempt_stages_and_closes_complete_pixiv_book(client, monkeypatch):
    add_account(client)
    held = []
    class Connector:
        def normalize_id(self, value): return value
        def fetch(self, value, **_):
            work = RemoteWork(remote_id=value, title='staged book', source_url='https://www.pixiv.net/artworks/123', remote_page_count=2)
            for name, data in [('1.png', png('red')), ('2.png', png('blue'))]:
                work.files.append((name, data))
                assert isinstance(work.files[-1][1], StagedFile)
                held.append(work.files[-1][1])
            return work
    monkeypatch.setattr(jobs, 'get_connector', lambda *args: Connector())
    jobs._stop.clear()
    queued = client.post('/api/sources/pixiv/import', json={'remote_id': '123'}).json()
    assert jobs.run_once()
    result = client.get('/api/jobs/' + queued['id']).json()
    assert result['status'] == 'completed', result
    with SessionLocal() as db:
        assert len(db.scalars(select(Asset)).all()) == 2
    assert held and all(file.stream.closed for file in held)


@pytest.mark.parametrize('orientation', [1, 6, 8])
def test_jpeg_reduces_pixels_before_orientation_and_keeps_display_dimensions(monkeypatch, orientation):
    buffer = io.BytesIO()
    with Image.new('RGB', (1200, 800), 'red') as picture:
        exif = Image.Exif(); exif[274] = orientation
        picture.save(buffer, 'JPEG', exif=exif)
    original_transpose = image_thumbnails.ImageOps.exif_transpose
    def transpose(image):
        assert image.width * image.height <= 480 * 640
        return original_transpose(image)
    monkeypatch.setattr(image_thumbnails.ImageOps, 'exif_transpose', transpose)
    data = image_thumbnails.render(io.BytesIO(buffer.getvalue()), (480, 640))
    with Image.open(io.BytesIO(data)) as result:
        assert result.size == ((480, 320) if orientation == 1 else (427, 640))


def test_thumbnail_decode_failure_is_bounded_and_original_still_available(client, monkeypatch):
    item = upload(client).json()
    monkeypatch.setattr(image_thumbnails, 'MAX_PIXELS', 10)
    response = client.get(item['cover_url'])
    assert response.status_code == 400
    assert client.get(item['assets'][0]['url']).content == png()
    monkeypatch.setattr(image_thumbnails, 'MAX_PIXELS', 36_000_000)
    assert client.get(item['cover_url']).status_code == 200


def test_parallel_cover_misses_coalesce_without_blocking_health_or_cached_images(client, monkeypatch):
    cached_item = upload(client, files=[('files', ('cached.png', png('blue'), 'image/png'))]).json()
    assert client.get(cached_item['cover_url']).status_code == 200
    item = upload(client).json()
    entered, release = Event(), Event()
    original = image_thumbnails.render
    calls = []
    def slow(*args, **kwargs):
        calls.append(1); entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(image_thumbnails, 'render', slow)
    with ThreadPoolExecutor(max_workers=44) as pool:
        pending = [pool.submit(client.get, item['cover_url']) for _ in range(40)]
        try:
            assert entered.wait(3)
            assert pool.submit(client.get, '/api/health').result(timeout=3).status_code == 200
            assert pool.submit(client.get, cached_item['cover_url']).result(timeout=3).status_code == 200
            assert pool.submit(client.get, '/api/items').result(timeout=3).status_code == 200
        finally:
            release.set()
        assert all(future.result(timeout=10).status_code == 200 for future in pending)
    assert len(calls) == 1
