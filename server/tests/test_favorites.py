from datetime import timedelta

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select

from app.database import SessionLocal
from app.dev_migrations import _configuration
from app.favorites_api import seed_default_groups
from app.models import FavoriteGroup, FavoriteGroupItem, Item, ItemFavorite, ItemVisit, ReadingProgress, utcnow
from app.integrations.base import RemoteWork
from app.jobs import merge_remote_item
from test_library import client, upload
from test_devices import bind, bearer
from test_pixiv_accounts import accounts


@pytest.fixture
def collection(client):
    with SessionLocal() as db:
        seed_default_groups(db)
    one = upload(client, title='漫画一').json()
    two = upload(client, files=[('files', ('novel.txt', '第一段\n第二段'.encode(), 'text/plain'))], title='小说二', type='novel').json()
    groups = client.get('/api/favorite-groups').json()['items']
    return one, two, groups


def mark(client, item, **value):
    return client.put(f"/api/items/{item['id']}/favorite", json={'favorite': True, **value})


def test_multi_group_favorites_scores_idempotence_and_filtered_pagination(client, collection):
    one, two, groups = collection
    ids = [g['id'] for g in groups[:2]]
    for _ in range(2):
        assert mark(client, one, group_ids=ids, score=5).json()['user_score'] == 5
    mark(client, two, group_ids=[])
    listing = client.get('/api/items', params={'favorite': True, 'page_size': 1}).json()
    assert listing['total'] == 2 and len(listing['items']) == 1
    grouped = client.get('/api/items', params={'favorite_group': ids[0]}).json()
    assert grouped['total'] == 1 and grouped['items'][0]['id'] == one['id']
    assert grouped['items'][0]['is_favorite'] is True
    assert client.get('/api/items', params={'favorite': True, 'favorite_group': 'ungrouped', 'type': 'novel', 'q': '小说'}).json()['total'] == 1
    assert client.get('/api/items', params={'favorite': True, 'source': 'pixiv'}).json()['total'] == 0
    with SessionLocal() as db:
        assert db.query(ItemFavorite).count() == 2
        assert db.query(FavoriteGroupItem).count() == 2
    counts = client.get('/api/favorite-groups').json()
    assert counts['total_favorites'] == 2 and counts['ungrouped_count'] == 1


def test_regroup_remove_keeps_score_original_content_and_progress(client, collection):
    one, _, groups = collection
    client.put(f"/api/items/{one['id']}/progress", json={'page': 0})
    mark(client, one, group_ids=[groups[0]['id']], score=4)
    mark(client, one)  # Omitted groups preserve memberships.
    assert client.get(f"/api/items/{one['id']}").json()['favorite_group_ids'] == [groups[0]['id']]
    mark(client, one, group_ids=[groups[1]['id']])
    assert client.get('/api/items', params={'favorite_group': groups[0]['id']}).json()['total'] == 0
    response = mark(client, one, favorite=False)
    assert response.status_code == 200 and response.json()['is_favorite'] is False
    detail = client.get(f"/api/items/{one['id']}").json()
    assert detail['user_score'] == 4 and detail['progress']['page'] == 0
    assert detail['assets'] == one['assets']


def test_score_without_favorite_and_clear_keeps_groups_content_and_progress(client, collection):
    one, _, groups = collection
    path = f"/api/items/{one['id']}"
    client.put(path + '/progress', json={'page': 0}).raise_for_status()
    response = client.put(path + '/score', json={'score': 4})
    assert response.status_code == 200
    assert response.json() == {'id': one['id'], 'user_score': 4}
    detail = client.get(path).json()
    assert not detail['is_favorite'] and detail['favorite_group_ids'] == []
    assert client.get('/api/favorite-groups').json()['total_favorites'] == 0
    assert client.get('/api/items', params={'favorite': True}).json()['total'] == 0
    assert detail['user_score'] == 4 and detail['progress']['page'] == 0
    assert detail['assets'] == one['assets']

    ids = [group['id'] for group in groups[:2]]
    assert mark(client, one, group_ids=ids).json()['user_score'] == 4
    for value in (5, 0):
        assert client.put(path + '/score', json={'score': value}).json()['user_score'] == value
        detail = client.get(path).json()
        assert detail['is_favorite'] and sorted(detail['favorite_group_ids']) == sorted(ids)
        assert detail['assets'] == one['assets'] and detail['progress']['page'] == 0
    client.put(path + '/score', json={'score': 3}).raise_for_status()
    assert mark(client, one, favorite=False).json()['user_score'] == 3
    assert client.get('/api/favorite-groups').json()['total_favorites'] == 0


@pytest.mark.parametrize('value', [
    {'score': -1}, {'score': 6}, {'score': True}, {'score': 2.5},
    {'score': '4'}, {'score': None}, {}, {'score': 3, 'favorite': True},
    {'score': 3, 'group_ids': []},
])
def test_invalid_score_cannot_change_personal_state(client, collection, value):
    one, _, groups = collection
    ids = [groups[0]['id']]
    mark(client, one, group_ids=ids, score=2).raise_for_status()
    path = f"/api/items/{one['id']}"
    assert client.put(path + '/score', json=value).status_code == 422
    detail = client.get(path).json()
    assert detail['user_score'] == 2 and detail['is_favorite']
    assert detail['favorite_group_ids'] == ids


def test_score_endpoint_device_access_origin_revocation_and_missing_item(client, collection):
    one, _, _ = collection
    path = f"/api/items/{one['id']}/score"
    assert client.put('/api/items/missing/score', json={'score': 1}).status_code == 404
    device = bind(client)
    headers = bearer(device)
    client.cookies.clear()
    assert client.put(path, json={'score': 4}).status_code == 401
    assert client.put(path, headers=headers, json={'score': 4}).status_code == 200
    assert not client.get(f"/api/items/{one['id']}", headers=headers).json()['is_favorite']
    assert client.put(path, headers={**headers, 'Origin': 'https://evil.test'}, json={'score': 1}).status_code == 403
    assert client.get(f"/api/items/{one['id']}", headers=headers).json()['user_score'] == 4
    assert client.put(path + '/extra', headers=headers, json={'score': 1}).status_code == 403
    assert client.post(path, headers=headers, json={'score': 1}).status_code == 403
    client.post('/api/devices/disconnect', headers=headers).raise_for_status()
    assert client.put(path, headers=headers, json={'score': 5}).status_code == 401


@pytest.mark.parametrize('value', [{'score': 6}, {'score': -1}, {'score': True}, {'score': 2.5}, {'favorite': 'true'}, {'group_ids': ['missing']}, {'extra': 'no'}])
def test_invalid_favorite_writes_leave_existing_state_unchanged(client, collection, value):
    one, _, groups = collection
    before = mark(client, one, group_ids=[groups[0]['id']], score=3).json()
    assert mark(client, one, **value).status_code in (400, 422)
    detail = client.get(f"/api/items/{one['id']}").json()
    assert detail['favorite_group_ids'] == before['favorite_group_ids'] and detail['user_score'] == 3


def test_group_create_rename_delete_preserves_favorites_and_does_not_reseed(client, collection):
    one, _, groups = collection
    assert [g['name'] for g in groups] == ['想看', '在看', '已看', '推荐']
    created = client.post('/api/favorite-groups', json={'name': '  自定义  '}).json()
    mark(client, one, group_ids=[created['id']], score=2)
    assert client.put('/api/favorite-groups/' + created['id'], json={'name': '珍藏'}).json()['name'] == '珍藏'
    assert client.post('/api/favorite-groups', json={'name': '珍藏'}).status_code == 409
    assert client.post('/api/favorite-groups', json={'name': ' \n '}).status_code == 422
    client.delete('/api/favorite-groups/' + created['id']).raise_for_status()
    detail = client.get(f"/api/items/{one['id']}").json()
    assert detail['is_favorite'] and detail['favorite_group_ids'] == [] and detail['user_score'] == 2
    for group in groups:
        client.delete('/api/favorite-groups/' + group['id']).raise_for_status()
    assert client.get('/api/favorite-groups').json()['items'] == []


def test_history_recency_unique_and_clear_does_not_affect_favorites_or_reading(client, collection):
    one, two, groups = collection
    mark(client, one, group_ids=[groups[0]['id']], score=5)
    for item in (one, two, one):
        client.post(f"/api/items/{item['id']}/visit").raise_for_status()
    history = client.get('/api/items', params={'history': True}).json()
    assert history['total'] == 2 and history['items'][0]['id'] == one['id']
    assert client.get('/api/items', params={'history': True, 'type': 'novel'}).json()['total'] == 1
    assert client.post('/api/items/missing/visit').status_code == 404
    client.delete('/api/history').raise_for_status()
    assert client.get('/api/items', params={'history': True}).json()['total'] == 0
    assert client.get('/api/items', params={'favorite': True}).json()['total'] == 1
    assert client.get('/api/items').json()['total'] == 2


def test_device_access_stays_narrow_and_favorites_are_shared(client, collection):
    one, _, groups = collection
    device = bind(client)
    client.cookies.clear()
    headers = bearer(device)
    assert client.get('/api/favorite-groups').status_code == 401
    assert client.get('/api/favorite-groups', headers=headers).status_code == 200
    assert client.put(f"/api/items/{one['id']}/favorite", headers=headers, json={'favorite': True, 'group_ids': [groups[0]['id']], 'score': 5}).status_code == 200
    assert client.post(f"/api/items/{one['id']}/visit", headers=headers).status_code == 200
    assert client.get('/api/items?favorite=true&history=true', headers=headers).json()['total'] == 1
    assert client.delete('/api/favorite-groups/' + groups[0]['id'], headers=headers).status_code == 200
    assert client.delete('/api/history', headers=headers).status_code == 200
    assert client.delete('/api/items/' + one['id'], headers=headers).status_code == 403
    assert client.put('/api/queue/settings', headers=headers, json={'paused': True}).status_code == 403
    assert client.post('/api/favorite-groups', headers={**headers, 'Origin': 'https://evil.test'}, json={'name': 'forbidden'}).status_code == 403


def test_item_deletion_cascades_only_its_history_favorites_and_memberships(client, collection):
    one, _, groups = collection
    mark(client, one, group_ids=[groups[0]['id']])
    client.post(f"/api/items/{one['id']}/visit")
    client.delete('/api/items/' + one['id']).raise_for_status()
    with SessionLocal() as db:
        assert db.query(ItemFavorite).count() == db.query(FavoriteGroupItem).count() == db.query(ItemVisit).count() == 0
        assert db.query(FavoriteGroup).count() == 4


def test_image_flow_progress_accepts_fraction_but_not_novel_chapter(client, collection):
    one, _, _ = collection
    path = f"/api/items/{one['id']}/progress"
    result = client.put(path, json={'page': 0, 'offset': .375})
    assert result.status_code == 200 and result.json()['offset'] == .375
    assert client.get('/api/items/' + one['id']).json()['progress']['offset'] == .375
    assert client.put(path, json={'page':0,'section':1,'offset':.5}).status_code == 400
    assert client.put(path, json={'page':0,'offset':1.1}).status_code == 422


def test_metadata_page_count_and_upstream_bookmarks_exist_before_body_and_preserve_personal_data(client):
    with SessionLocal() as db:
        item = merge_remote_item(db, 'pixiv', RemoteWork(remote_id='123', title='未下载漫画', source_url='https://www.pixiv.net/artworks/123', metadata_only=True, remote_page_count=12, bookmark_count=3456))
        db.commit(); ident = item.id
    mark(client, {'id': ident}, score=4)
    with SessionLocal() as db:
        merge_remote_item(db, 'pixiv', RemoteWork(remote_id='123', title='新标题', source_url='https://www.pixiv.net/artworks/123', metadata_only=True, remote_page_count=14))
        db.commit()
    detail = client.get('/api/items/' + ident).json()
    assert detail['page_count'] == detail['remote_page_count'] == 14
    assert detail['source_bookmark_count'] == 3456 and detail['assets'] == []
    assert detail['is_favorite'] and detail['user_score'] == 4


def test_favorites_migration_seeds_four_groups_and_schema_matches_models(tmp_path):
    engine = create_engine('sqlite:///' + (tmp_path / 'favorites.db').as_posix())
    config = _configuration(str(engine.url))
    command.upgrade(config, 'head')
    from sqlalchemy.orm import Session
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.database import Base
    with Session(engine) as db:
        assert [group.name for group in db.scalars(select(FavoriteGroup).order_by(FavoriteGroup.id))] == ['想看', '在看', '已看', '推荐']
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(config, 'b58ce962d713')
    assert 'favorite_groups' not in inspect(engine).get_table_names()
    command.upgrade(config, 'head')
    engine.dispose()


@pytest.mark.parametrize('repair', [False, True])
def test_novel_cover_is_saved_separately_from_body_and_can_repair_downloaded_work(client, accounts, monkeypatch, repair):
    from app import jobs, queue_control
    from app.library import import_files
    from test_library import png
    original = '原始小说正文，不得被封面替换'.encode()
    existing = None
    if repair:
        with SessionLocal() as db:
            item = import_files(db, [('novel.txt', original)], title='原小说', item_type='novel', source='pixiv', source_id='novel:123')
            existing = item.id
        mark(client, {'id': existing}, score=5)
    calls = []
    class Connector:
        def normalize_id(self, value): return value
        def fetch(self, ident, **kwargs):
            calls.append(kwargs.get('download', True))
            return RemoteWork(remote_id=ident, title='小说', source_url='https://www.pixiv.net/novel/show.php?id=123', item_type='novel',
                metadata_only=not kwargs.get('download', True), preview_cover=('cover.png', png()),
                files=[] if repair else [('novel.txt', original)])
    monkeypatch.setattr(jobs, 'get_connector', lambda *args, **kwargs: Connector())
    jobs._stop.clear(); queue_control.update_settings({'rate_limit_enabled': False})
    with SessionLocal() as db:
        job = jobs.enqueue_import(db, {'platform':'pixiv','remote_id':'novel:123','download':not repair, **({'item_id':existing} if existing else {})})
        ident = job.id
    assert jobs.run_once()
    assert calls == [not repair]
    result = client.get('/api/jobs/' + ident).json()
    assert result['status'] == 'completed', result
    detail = client.get('/api/items/' + result['item_id']).json()
    assert detail['cover_url'].endswith('/cover')
    assert len(detail['assets']) == 1 and detail['assets'][0]['mime_type'] == 'text/plain'
    assert client.get(detail['assets'][0]['url']).content == original
    assert client.get(detail['cover_url']).headers['content-type'] == 'image/webp'
    if repair:
        assert detail['is_favorite'] and detail['user_score'] == 5
