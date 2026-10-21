from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from test_library import client
from app.database import SessionLocal
from app.integrations.base import ConnectorError, RemoteWork
from app.integrations.pixiv import PixivConnector
from app.library import append_novel_images, import_files
from app.models import Asset, Item
from app.novel_text import image_keys, section_blocks


def picture():
    stream = BytesIO()
    Image.new('RGB', (120, 200), 'white').save(stream, 'PNG')
    return stream.getvalue()


def test_connector_saves_uploaded_and_referenced_pages_once(monkeypatch):
    from app.integrations import pixiv
    calls = []
    class HTTP:
        def __init__(self, *a, **kw): pass
        def fetch(self, url): calls.append(url); return picture()
        def close(self): pass
    monkeypatch.setattr(pixiv, 'SafeHTTP', HTTP)
    api = SimpleNamespace(illust_detail=lambda ident: {'illust': {'id': int(ident), 'meta_pages': [
        {'image_urls': {'original': 'https://i.pximg.net/first.png'}},
        {'image_urls': {'original': 'https://i.pximg.net/second.png'}}]}})
    result = RemoteWork('novel:1', 'Synthetic', '')
    text = 'Before[uploadedimage:2]After[uploadedimage:2][pixivimage:3-2]'
    PixivConnector({})._novel_images(api, {'images': {'2': {'urls': {'original': 'https://i.pximg.net/upload.png'}}}}, text, result, lambda _: None)
    assert calls == ['https://i.pximg.net/upload.png', 'https://i.pximg.net/second.png']
    assert list(result.inline_images.values()) == ['uploadedimage:2', 'pixivimage:3-2']
    assert [b['type'] for b in section_blocks(text, {})] == ['text', 'image', 'text', 'image', 'image']
    assert len(image_keys(text)) == 2


@pytest.mark.parametrize('url', ['http://i.pximg.net/a.png', 'https://localhost/a.png', 'https://evil.test/a.png'])
def test_untrusted_image_links_never_reach_network(url):
    with pytest.raises(ConnectorError):
        PixivConnector({})._novel_images(None, {'images': {'2': {'urls': {'original': url}}}},
            '[uploadedimage:2]', RemoteWork('novel:1', 'Synthetic', ''), lambda _: None)


def test_backfill_preserves_text_progress_and_manual_metadata(client):
    text = '[chapter:One]Before[uploadedimage:2]After[newpage]Last'
    with SessionLocal() as db:
        item = import_files(db, [('novel.txt', text.encode())], title='Manual title', item_type='novel',
            source='pixiv', source_id='novel:1')
        ident, text_id = item.id, item.assets[0].id
        item.user_score = 4
        db.commit()
    client.put(f'/api/items/{ident}/progress', json={'page': 0, 'section': 1, 'offset': .6})
    with SessionLocal() as db:
        assert append_novel_images(db, ident, [('picture.png', picture())], {'picture.png': 'uploadedimage:2'}, before_commit=lambda _: None) == 1
        assert append_novel_images(db, ident, [('picture.png', picture())], {'picture.png': 'uploadedimage:2'}, before_commit=lambda _: None) == 0
    item = client.get(f'/api/items/{ident}').json()
    assert item['title'] == 'Manual title' and item['user_score'] == 4
    assert item['page_count'] == 1 and len(item['assets']) == 1 and len(item['inline_assets']) == 1
    assert item['progress'] == {'page': 0, 'section': 1, 'offset': .6}
    assert item['assets'][0]['id'] == text_id
    assert client.get(item['assets'][0]['url']).content.decode() == text
    sections = client.get(f'/api/assets/{text_id}/text-sections').json()['sections']
    image = sections[0]['blocks'][1]
    assert image['width'] == 120 and image['height'] == 200
    assert client.get(image['url']).content == picture()
    # Card summaries preserve one text document, while counting all stored bytes.
    cards = client.get('/api/items').json()['items']
    assert cards[0]['page_count'] == 1


def test_new_import_keeps_text_first_and_unknown_references_visible(client):
    with SessionLocal() as db:
        item = import_files(db, [('a.png', picture()), ('z.txt', b'[uploadedimage:2][uploadedimage:3]')],
            item_type='novel', inline_images={'a.png': 'uploadedimage:2'})
        assert item.assets[0].mime_type == 'text/plain'
        text_id = item.assets[0].id
    blocks = client.get(f'/api/assets/{text_id}/text-sections').json()['sections'][0]['blocks']
    assert blocks[0]['type'] == 'image' and blocks[1]['unavailable'] is True


def test_cross_item_images_cannot_be_resolved(client):
    with SessionLocal() as db:
        first = import_files(db, [('one.txt', b'[uploadedimage:2]')], item_type='novel')
        text_id = first.assets[0].id
        import_files(db, [('two.txt', b'[uploadedimage:2]'), ('a.png', picture())], item_type='novel', inline_images={'a.png': 'uploadedimage:2'})
    block = client.get(f'/api/assets/{text_id}/text-sections').json()['sections'][0]['blocks'][0]
    assert block == {'type': 'image', 'key': 'uploadedimage:2', 'unavailable': True}


def test_backfill_job_preserves_existing_text_and_obeys_cancel_guard(client, monkeypatch):
    from app import jobs, queue_control
    from app.models import Job
    from app.integrations.base import JobCancelled
    with SessionLocal() as db:
        item = import_files(db, [('one.txt', b'Original[uploadedimage:2]')], item_type='novel', source='pixiv', source_id='novel:1')
        ident = item.id
    class Connector:
        def normalize_id(self, value): return value
        def fetch_novel_images(self, value, text, **kwargs):
            assert text == 'Original[uploadedimage:2]'
            return RemoteWork(value, 'New remote title', '', files=[('a.png', picture())], inline_images={'a.png': 'uploadedimage:2'})
    monkeypatch.setattr(jobs, 'get_connector', lambda *a: Connector())
    payload = {'platform': 'pixiv', 'remote_id': 'novel:1', 'item_id': ident, 'novel_images_only': True}
    with SessionLocal() as db:
        def cancel(_): raise JobCancelled()
        with pytest.raises(JobCancelled):
            jobs._import_pixiv(db, payload, lambda _: None, cancel, None)
        db.rollback()
        assert len(db.get(Item, ident).assets) == 1
        result = jobs._import_pixiv(db, payload, lambda _: None, lambda _: None, None)
        assert result['illustrations_added'] == 1
    item = client.get('/api/items/'+ident).json()
    assert item['title'] == 'one' and len(item['assets']) == 1 and len(item['inline_assets']) == 1
