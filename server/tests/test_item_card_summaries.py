"""Card queries preserve reader metadata without loading individual page objects."""
from datetime import datetime

from sqlalchemy import event, select
from sqlalchemy.orm import Session, raiseload

from app.author_api import FeedInput, scope_key
from app.database import SessionLocal
from app.integrations.credentials import encrypt_credentials
from app.library import build_item_cards, build_item_view, set_tags
from app.models import (Asset, AuthorFeed, AuthorFeedEntry, AuthorProfile, FavoriteGroup,
                        FavoriteGroupItem, Item, ItemFavorite, ReadingProgress, SourceAccount)
from test_library import client


def asset(position, *, mime='image/png', status='available', size=123):
    return Asset(position=position, relative_path='synthetic/unused', original_name=f'{position}.bin',
                 mime_type=mime, status=status, size_bytes=size, sha256='0' * 64)


def test_summaries_match_detailed_serialization_for_all_card_states(client):
    with SessionLocal() as db:
        group = FavoriteGroup(name='Synthetic grouping')
        db.add(group)
        db.flush()
        items = [
            Item(title='Metadata', type='manga', remote_page_count=19),
            Item(title='Failure', type='illustration', content_state='failed', content_error='remote failure'),
            Item(title='Complete', type='manga', assets=[asset(2, size=401), asset(0, mime='text/plain'), asset(1)]),
            Item(title='Missing', type='manga', assets=[asset(0, status='missing'), asset(1)]),
            Item(title='Novel with cover', type='novel', assets=[asset(0, mime='text/plain'), asset(1)]),
            Item(title='Video', type='video', assets=[asset(0, mime='video/mp4', size=900)]),
            Item(title='No novel pages', type='novel', remote_page_count=7),
            Item(title='Preview', preview_cover={'relative_path': 'synthetic/preview'}, assets=[asset(0)]),
        ]
        db.add_all(items)
        db.flush()
        items[2].progress = ReadingProgress(page=1, section=2, offset=.5)
        items[2].favorite = ItemFavorite(groups=[FavoriteGroupItem(group_id=group.id)])
        items[2].source_published_at = datetime(2024, 3, 4)
        items[2].source_bookmark_count = 14
        items[2].title_zh, items[2].title_zh_source = '译名', 'Complete'
        set_tags(db, items[2], ['Original'], 'source')
        items[2].item_tags[0].tag.name_zh = '标签'
        db.commit()
        expected = {item.id: build_item_view(item) for item in items}
    with SessionLocal() as db:
        cards = build_item_cards(db, list(db.scalars(select(Item).options(raiseload(Item.assets)))))
    assert {card['id']: card for card in cards} == expected
    assert build_item_cards(None, []) == []


def test_author_list_never_hydrates_page_assets_and_detail_still_lists_them(client):
    with SessionLocal() as db:
        item = Item(title='Long manga', author='Synthetic author', source='pixiv',
                    assets=[asset(page, size=page + 1) for page in range(600)])
        db.add(item)
        db.commit()
        ident = item.id
    loaded = []

    def track(_session, instance):
        if isinstance(instance, Asset):
            loaded.append(instance)

    event.listen(Session, 'loaded_as_persistent', track)
    try:
        response = client.get('/api/items', params={'author': 'Synthetic author', 'source': 'pixiv'})
        assert response.status_code == 200
        assert loaded == []
        value = response.json()['items'][0]
        assert value['page_count'] == 600 and value['size_bytes'] == 600 * 601 // 2
        assert client.get('/api/dashboard').status_code == 200
        assert loaded == []
        detail = client.get('/api/items/' + ident).json()
        assert len(detail['assets']) == 600 and len(loaded) == 600
    finally:
        event.remove(Session, 'loaded_as_persistent', track)


def test_summary_updates_immediately_after_page_status_or_count_changes(client):
    with SessionLocal() as db:
        item = Item(title='Changing manga', author='Synthetic', assets=[asset(0)])
        db.add(item)
        db.commit()
        ident = item.id
    first = client.get('/api/items', params={'author': 'Synthetic'}).json()['items'][0]
    assert first['page_count'] == 1 and first['content_state'] == 'ready' and first['cover_url']
    with SessionLocal() as db:
        item = db.get(Item, ident)
        item.assets[0].status = 'missing'
        item.assets.append(asset(1, size=77))
        db.commit()
        cover = item.assets[1].id
    second = client.get('/api/items', params={'author': 'Synthetic'}).json()['items'][0]
    assert second['page_count'] == 2 and second['content_state'] == 'failed'
    assert second['size_bytes'] == 200 and second['cover_url'] == f'/api/assets/{cover}/thumbnail'


def test_author_feed_keeps_order_pagination_and_only_loads_card_summaries(client):
    with SessionLocal() as db:
        account = SourceAccount(platform='pixiv', label='Synthetic',
                                credentials_encrypted=encrypt_credentials({'refresh_token': 'synthetic'}))
        db.add_all([account, AuthorProfile(remote_id='10')])
        db.flush()
        feed = AuthorFeed(author_id='10', account_id=account.id,
                          scope_key=scope_key('10', FeedInput(), account.id),
                          section='works', category='illustration', restrict='public', generation=2, complete=True)
        db.add(feed)
        db.flush()
        expected = []
        for index in range(27):
            item = Item(title=f'Synthetic {index}', source='pixiv', type='illustration',
                        assets=[asset(page) for page in range(60 if index == 0 else 1)])
            db.add(item)
            db.flush()
            db.add(AuthorFeedEntry(feed_id=feed.id, item_id=item.id, generation=2 if index < 25 else 1, position=index))
            expected.append(item.id)
        db.commit()
        account_id = account.id
    loaded = []

    def track(_session, instance):
        if isinstance(instance, Asset):
            loaded.append(instance)

    event.listen(Session, 'loaded_as_persistent', track)
    try:
        pages = [client.get('/api/pixiv/authors/10/feed', params={'account_id': account_id, 'page': page}).json()
                 for page in (1, 2)]
        assert [item['id'] for page in pages for item in page['items']] == expected
        assert all(page['total'] == 27 and page['complete'] for page in pages)
        assert pages[0]['items'][0]['page_count'] == 60
        assert loaded == []
    finally:
        event.remove(Session, 'loaded_as_persistent', track)
