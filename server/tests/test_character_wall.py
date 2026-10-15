import pytest
from sqlalchemy import select

from test_library import client, png
from test_devices import bind, bearer
from test_character_search import character, mock_provider, provider_response
from app import character_search as research
from app.database import SessionLocal
from app.library import import_files, set_tags
from app.models import CharacterProfile, Item, ItemTag, Tag


def seed():
    with SessionLocal() as db:
        ready = import_files(db, [('page.png', png())], title='离线漫画', source='pixiv', source_id='42', tags=['角色:hero', 'ordinary'])
        other = Item(title='封面资料', source='jm')
        db.add(other); db.flush(); set_tags(db, other, ['角色:hero', 'character:second'], 'source')
        # Multiple origins for the same work must not inflate counts or covers.
        tag = db.scalar(select(Tag).where(Tag.name == '角色:hero'))
        db.add(ItemTag(item_id=ready.id, tag_id=tag.id, origin='ai'))
        empty = Tag(name='角色:   '); db.add(empty); db.commit()
        return tag.id, ready.id, other.id


def test_wall_groups_roles_only_deduplicates_origins_filters_and_pages(client):
    ident, ready, _ = seed()
    response = client.get('/api/characters').json()
    assert response['total'] == 2
    hero = response['items'][0]
    assert hero['id'] == ident and hero['name'] == 'hero' and hero['count'] == 2
    assert len(hero['covers']) == 2 and len({cover['id'] for cover in hero['covers']}) == 2
    downloaded = client.get('/api/characters', params={'downloaded': True}).json()
    assert downloaded['total'] == 1 and downloaded['items'][0]['count'] == 1
    assert downloaded['items'][0]['covers'][0]['id'] == ready
    assert client.get('/api/characters', params={'source': 'jm'}).json()['items'][0]['count'] == 1
    assert client.get('/api/characters', params={'source': 'local'}).json()['total'] == 0
    first = client.get('/api/characters?page_size=1').json()
    second = client.get('/api/characters?page_size=1&page=2').json()
    assert first['items'][0]['id'] != second['items'][0]['id']
    detail = client.get(f'/api/characters/{ident}').json()
    assert detail['count'] == client.get('/api/items', params={'tag': ident}).json()['total'] == 2


def test_confirmed_profile_is_stored_plain_offline_and_aliases_searchable(client, monkeypatch):
    ident, ready, _ = seed()
    mock_provider(monkeypatch, lambda request: provider_response({'data': {'Character': character()}}))
    response = client.post(f'/api/characters/{ident}/profile', json={'candidate_id': 17})
    assert response.status_code == 200, response.text
    profile = response.json()['profile']
    assert profile['id'] == '17' and profile['aliases'] == ['Naruto', '鸣人']
    assert 'script' not in profile['description'] and 'image.invalid' not in profile['description']
    assert profile['source_url'] == 'https://anilist.co/character/17'
    monkeypatch.setattr(research, '_provider_data', lambda *args: (_ for _ in ()).throw(AssertionError('Offline read used network')))
    assert client.get(f'/api/characters/{ident}').json()['profile'] == profile
    assert client.get('/api/characters', params={'q':'鸣人'}).json()['items'][0]['id'] == ident
    with SessionLocal() as db:
        assert db.get(Item, ready).title == '离线漫画'
        assert db.get(CharacterProfile, ident).description == profile['description']


def test_add_network_character_without_works_and_failed_refresh_preserves_profile(client, monkeypatch):
    mock_provider(monkeypatch, lambda request: provider_response({'data': {'Character': character()}}))
    created = client.post('/api/characters', json={'candidate_id':17})
    assert created.status_code == 200
    ident = created.json()['id']
    assert created.json()['count'] == 0
    assert client.get('/api/characters').json()['total'] == 1
    assert client.get('/api/characters?downloaded=true').json()['total'] == 0
    assert client.post('/api/characters', json={'candidate_id':17}).json()['id'] == ident
    assert client.get('/api/characters').json()['total'] == 1
    saved = client.get(f'/api/characters/{ident}').json()['profile']
    monkeypatch.setattr(research, 'fetch_character_profile', lambda _: (_ for _ in ()).throw(research.CharacterSearchError('temporary', 502)))
    assert client.post(f'/api/characters/{ident}/profile', json={'candidate_id':17}).status_code == 502
    assert client.get(f'/api/characters/{ident}').json()['profile'] == saved


@pytest.mark.parametrize('candidate', [True, '17', -1, 0, 2**31])
def test_bad_profile_identifier_never_calls_network(client, monkeypatch, candidate):
    monkeypatch.setattr(research, 'fetch_character_profile', lambda _: (_ for _ in ()).throw(AssertionError('Invalid request reached network')))
    assert client.post('/api/characters', json={'candidate_id':candidate}).status_code == 422


def test_device_wall_permissions_and_search_are_narrow_and_authenticated(client, monkeypatch):
    ident, _, _ = seed()
    headers = bearer(bind(client))
    assert client.get('/api/characters', headers=headers).status_code == 200
    assert client.get(f'/api/characters/{ident}', headers=headers).status_code == 200
    mock_provider(monkeypatch, lambda request: provider_response({'data': {'Page': {'characters':[character()]}}}))
    assert client.post('/api/characters/search', headers=headers, json={'query':'Naruto'}).status_code == 200
    assert client.get('/api/sources/pixiv/account-roles', headers=headers).status_code == 403
    assert client.delete(f'/api/characters/{ident}', headers=headers).status_code == 403
    client.post('/api/auth/logout')
    assert client.get('/api/characters').status_code == 401
    assert client.post('/api/characters/search', json={'query':'Naruto'}).status_code == 401


def test_missing_or_noncharacter_tag_cannot_receive_a_profile_and_search_is_literal(client, monkeypatch):
    _, _, _ = seed()
    with SessionLocal() as db:
        ordinary = db.scalar(select(Tag).where(Tag.name == 'ordinary')).id
    monkeypatch.setattr(research, 'fetch_character_profile', lambda _: (_ for _ in ()).throw(AssertionError('Invalid target used network')))
    assert client.get(f'/api/characters/{ordinary}').status_code == 404
    assert client.post(f'/api/characters/{ordinary}/profile', json={'candidate_id':17}).status_code == 404
    assert client.post('/api/characters/missing/profile', json={'candidate_id':17}).status_code == 404
    assert client.get('/api/characters?q=%25').json()['total'] == 0


def test_provider_identity_mismatch_does_not_bind_profile(client, monkeypatch):
    mock_provider(monkeypatch, lambda request: provider_response({'data': {'Character': character(99)}}))
    assert client.post('/api/characters', json={'candidate_id':17}).status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(CharacterProfile)) is None


def test_pixiv_author_defaults_include_all_media_but_other_subscriptions_keep_images(client):
    author = client.post('/api/subscriptions', json={'platform':'pixiv','target_type':'author','target':'123','auto_download':False}).json()
    assert author['enabled'] is True and author['content_scope'] == 'all' and author['auto_download'] is False
    tag = client.post('/api/subscriptions', json={'platform':'pixiv','target_type':'tag','target':'role'}).json()
    assert tag['content_scope'] == 'images'
    explicit = client.post('/api/subscriptions', json={'platform':'pixiv','target_type':'author','target':'124','content_scope':'novels','enabled':False}).json()
    assert explicit['content_scope'] == 'novels' and explicit['enabled'] is False
    other = client.post('/api/subscriptions', json={'platform':'jm','target_type':'tag','target':'role'}).json()
    assert other['content_scope'] == 'images'
