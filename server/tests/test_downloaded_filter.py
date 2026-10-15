from app.database import SessionLocal
from app.models import Asset, Item
from test_library import client, upload
from test_devices import bind, bearer


def test_downloaded_excludes_covers_incomplete_and_missing_bodies(client):
    complete = upload(client, title='完整漫画').json()
    missing = upload(client, title='文件丢失').json()
    partial = upload(client, title='多P未完整').json()
    with SessionLocal() as db:
        db.get(Asset, missing['assets'][0]['id']).status = 'missing'
        db.get(Item, partial['id']).remote_page_count = 3
        db.add(Item(title='仅封面', type='manga', source='pixiv', content_state='metadata', preview_cover={'size_bytes': 10}))
        db.add(Item(title='没有正文', type='novel', source='pixiv', content_state='ready'))
        db.commit()
    result = client.get('/api/items', params={'downloaded': 'true'}).json()
    assert result['total'] == 1 and [item['id'] for item in result['items']] == [complete['id']]
    assert client.get('/api/items').json()['total'] == 5


def test_downloaded_filter_paginates_in_database_and_combines_search_type_source(client):
    first = upload(client, title='本地漫画').json()
    second = upload(client, files=[('files', ('story.txt', '下载小说'.encode(), 'text/plain'))], title='下载小说', type='novel').json()
    with SessionLocal() as db:
        db.get(Item, second['id']).remote_page_count = 100  # Novel sections are not image pages.
        db.commit()
    for page in (1, 2):
        response = client.get('/api/items', params={'downloaded': 'true', 'page_size': 1, 'page': page}).json()
        assert response['total'] == 2 and len(response['items']) == 1
    assert client.get('/api/items', params={'downloaded': 'true', 'source': 'local', 'type': 'novel', 'q': '小说'}).json()['items'][0]['id'] == second['id']
    assert client.get('/api/items', params={'downloaded': 'true', 'source': 'pixiv'}).json()['total'] == 0
    device = bearer(bind(client))
    assert client.get('/api/items', params={'downloaded': 'true'}, headers=device).json()['total'] == 2
