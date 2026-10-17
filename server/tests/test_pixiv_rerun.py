import pytest

from app import jobs
from app.database import SessionLocal
from app.integrations.base import RemoteWork
from app.library import import_files
from app.models import Item, ItemFavorite, Job, ReadingProgress
from test_library import client, png


@pytest.mark.parametrize('refresh', [False, True])
@pytest.mark.parametrize('covered', [False, True])
def test_metadata_rerun_can_refresh_downloaded_work_without_replacing_body(client, monkeypatch, refresh, covered):
    jobs._stop.clear()
    with SessionLocal() as db:
        item = import_files(db, [('page.png', png())], title='Manually edited title', source='pixiv', source_id='42')
        item.source_bookmark_count = 20
        if covered:
            item.preview_cover = 'pixiv/synthetic-cover.webp'
        item.user_score = 4
        item.favorite = ItemFavorite()
        item.progress = ReadingProgress(page=0, section=0, offset=0.4)
        db.commit()
        ident = item.id
        asset = (item.assets[0].id, item.assets[0].sha256, item.assets[0].relative_path)
        favorite_time = item.favorite.created_at
        job_id = jobs.enqueue_import(db, {'platform': 'pixiv', 'remote_id': '42', 'download': False,
                                         'refresh_metadata': refresh}).id
    seen = []

    class Connector:
        def normalize_id(self, value):
            return value

        def fetch(self, value, *, download, checkpoint, include_preview=True):
            assert download is False
            assert include_preview is not covered
            seen.append(value)
            return RemoteWork(remote_id=value, title='Remote title', source_url='https://www.pixiv.net/artworks/42',
                              metadata_only=True, remote_page_count=1, bookmark_count=88)

    monkeypatch.setattr(jobs, 'get_connector', lambda *args: Connector())
    assert jobs.run_once(('details',))
    assert seen == (['42'] if refresh else [])
    with SessionLocal() as db:
        job, item = db.get(Job, job_id), db.get(Item, ident)
        assert job.status == 'completed', job.error
        assert item.source_bookmark_count == (88 if refresh else 20)
        assert item.title == 'Manually edited title'
        assert len(item.assets) == 1
        assert (item.assets[0].id, item.assets[0].sha256, item.assets[0].relative_path) == asset
        assert item.user_score == 4 and item.favorite.created_at == favorite_time
        assert item.progress.offset == 0.4
