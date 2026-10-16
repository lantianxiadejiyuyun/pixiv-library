import pytest
from app.database import SessionLocal
from app.library import import_files, validate_content
from app.models import Asset
from app.storage import resolve_asset_path
from test_library import client, png
from test_devices import bind, bearer


def mp4():
    def atom(kind, content): return (len(content) + 8).to_bytes(4, 'big') + kind + content
    return atom(b'ftyp', b'isom\0\0\0\0isommp42') + atom(b'moov', atom(b'trak', b'synthetic-video-track')) + atom(b'mdat', b'synthetic-media-data')


@pytest.mark.parametrize('name', ['clip.mp4', 'clip.m4v', 'clip.mov'])
def test_video_container_upload_range_head_and_device_progress(client, name):
    payload = mp4()
    with SessionLocal() as db:
        item = import_files(db, [(name, payload)], title='Synthetic video', item_type='article', source='kemono')
        ident, asset_id = item.id, item.assets[0].id
        assert db.get(Asset, asset_id).relative_path.startswith('collections/kemono/')
    device = bearer(bind(client))
    url = f'/api/assets/{asset_id}/content'
    headers = {**device, 'Range': 'bytes=8-19'}
    partial = client.get(url, headers=headers)
    assert partial.status_code == 206
    assert partial.content == payload[8:20]
    assert partial.headers['content-range'] == f'bytes 8-19/{len(payload)}'
    assert partial.headers['content-type'] == ('video/quicktime' if name.endswith('.mov') else 'video/mp4')
    assert partial.headers['x-content-type-options'] == 'nosniff'
    head = client.head(url, headers=device)
    assert head.status_code == 200 and head.content == b'' and int(head.headers['content-length']) == len(payload)
    assert client.head('/api/sources', headers=device).status_code in (403, 405)
    assert client.get(url, headers={**device, 'Range': 'bytes=999999-'}).status_code == 416
    client.put(f'/api/items/{ident}/progress', headers=device, json={'page': 0, 'section': 0, 'offset': .63}).raise_for_status()
    assert client.get(f'/api/items/{ident}', headers=device).json()['progress']['offset'] == .63
    client.cookies.clear()
    assert client.get(url).status_code == 401
    assert client.head(url).status_code == 401


@pytest.mark.parametrize('name,data', [('fake.mp4', b'<html>error</html>'), ('short.mp4', mp4()[:-3]),
    ('wrong.webm', mp4()), ('short.mov', b'\0\0\0\x01mdat')])
def test_invalid_video_never_becomes_a_saved_work(client, name, data):
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            import_files(db, [('cover.png', png()), (name, data)], item_type='article')
        assert db.query(Asset).count() == 0


def test_webm_header_and_mixed_order(client):
    body = b'\x1a\x45\xdf\xa3\x9f\x42\x82\x84webm\x18\x53\x80\x67' + b'\x01' * 40
    assert validate_content('clip.webm', body)[1] == 'video/webm'
    with SessionLocal() as db:
        item = import_files(db, [('00001.png', png()), ('00002.mp4', mp4()), ('zz-content.txt', b'Synthetic article')], item_type='article')
        assert [asset.mime_type for asset in item.assets] == ['image/png', 'video/mp4', 'text/plain']
        assert all(resolve_asset_path(asset.relative_path).is_file() for asset in item.assets)
