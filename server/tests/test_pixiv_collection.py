from types import SimpleNamespace

import pytest
from sqlalchemy import select, func

from app import jobs, pixiv_collection
from app.database import SessionLocal
from app.integrations.base import JobCancelled, RemoteWork
from app.integrations.credentials import encrypt_credentials
from app.models import Item, ItemFavorite, Job, SourceAccount
from app.pixiv_favorites import observe
from test_library import client
from test_devices import bind, bearer
from test_pixiv_favorite_follow import accounts, item, work


def enable(client):
    response = client.put('/api/pixiv/collection-settings', json={'enabled': True})
    assert response.status_code == 200


@pytest.mark.parametrize('state', [True, False])
def test_only_main_positive_bookmarks_download_and_preserve_manual_favorite(client, state):
    enable(client)
    with SessionLocal() as db:
        main, download, *_ = accounts(db)
        record = item(db, favorite=True)
        old = record.favorite.created_at
        observe(db, record, work(state), main)
        observe(db, record, work(state), main)
        db.commit()
        queued = db.scalars(select(Job)).all()
        assert len(queued) == (1 if state else 0)
        if state:
            assert queued[0].payload['download'] is True
            assert queued[0].payload['main_bookmark_download'] is True
        assert record.favorite.created_at == old and record.user_score == 4


def test_auxiliary_favorite_is_verified_before_any_automatic_download(client):
    enable(client)
    with SessionLocal() as db:
        main, aux, *_ = accounts(db)
        record = item(db)
        observe(db, record, work(True), aux)
        db.commit()
        queued = db.scalars(select(Job)).all()
        assert len(queued) == 1 and queued[0].kind == 'pixiv_bookmark'
        assert record.favorite is None


def test_scan_has_public_private_all_types_and_no_duplicate_active_roots(client):
    enable(client)
    with SessionLocal() as db: accounts(db)
    response = client.post('/api/pixiv/bookmarks/sync')
    assert response.status_code == 200 and len(response.json()['job_ids']) == 6
    assert client.post('/api/pixiv/bookmarks/sync').json()['job_ids'] == []
    with SessionLocal() as db:
        queued = db.scalars(select(Job)).all()
        assert {(j.payload['category'], j.payload['restrict']) for j in queued} == {
            (category, restrict) for category in ('illustration', 'manga', 'novel') for restrict in ('public', 'private')}
        assert not pixiv_collection.due(db)


def test_scan_page_commits_main_favorite_download_and_next_page_together(client, monkeypatch):
    enable(client)
    with SessionLocal() as db:
        main, *_ = accounts(db)
    payload = {'platform': 'pixiv', 'pixiv_bookmark_scan': True, 'main_account_id': main,
               'category': 'manga', 'restrict': 'private', 'scan_batch': 'synthetic', 'page': 0}
    fake = SimpleNamespace(_api=lambda: None, _authenticated_user_id='123', _positive_id=str,
                           fetch_author_page=lambda *a, **kw: {'works': [work(None)], 'next_cursor': {'max_bookmark_id': '10'}, 'unavailable': 0})
    monkeypatch.setattr(jobs, 'get_connector', lambda *args: fake)
    with SessionLocal() as db:
        result = pixiv_collection.scan_page(db, payload, lambda _: None, lambda _: None)
        assert result['has_more'] is True
        record = db.scalar(select(Item).where(Item.source_id == '42'))
        assert record.favorite is not None
        children = db.scalars(select(Job)).all()
        assert sorted(j.kind for j in children) == ['import', 'source_sync']
        followup = next(j for j in children if j.kind == 'source_sync')
        assert followup.payload['cursor'] == {'max_bookmark_id': '10'}
        assert followup.payload.get('retry_queue') is not True


def test_scan_lost_lease_rolls_back_favorite_download_and_cursor(client, monkeypatch):
    enable(client)
    with SessionLocal() as db: main, *_ = accounts(db)
    fake = SimpleNamespace(_api=lambda: None, _authenticated_user_id='123', _positive_id=str,
                           fetch_author_page=lambda *a, **kw: {'works': [work(None)], 'next_cursor': None, 'unavailable': 0})
    monkeypatch.setattr(jobs, 'get_connector', lambda *args: fake)
    with SessionLocal() as db:
        with pytest.raises(JobCancelled):
            pixiv_collection.scan_page(db, {'main_account_id': main, 'category': 'novel', 'restrict': 'public'},
                                       lambda _: None, lambda _: (_ for _ in ()).throw(JobCancelled()))
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Item)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_phone_credential_import_requires_device_and_administrator_password(client):
    with SessionLocal() as db:
        main, *_ = accounts(db)
        db.get(SourceAccount, main).credentials_encrypted = encrypt_credentials({'refresh_token': 'main-token-synthetic'})
        db.commit()
    device = bind(client)
    client.cookies.clear()
    url = '/api/pixiv/direct/credentials'
    assert client.post(url, json={'admin_password': 'local-library'}).status_code == 401
    denied = client.post(url, headers=bearer(device), json={'admin_password': 'wrong'})
    assert denied.status_code == 403 and 'main-token-synthetic' not in denied.text
    response = client.post(url, headers=bearer(device), json={'admin_password': 'local-library'})
    assert response.status_code == 200
    assert response.json()['refresh_token'] == 'main-token-synthetic'
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/pixiv/collection-settings', headers=bearer(device)).status_code == 403
    assert client.post('/api/pixiv/bookmarks/sync', headers=bearer(device)).status_code == 403
