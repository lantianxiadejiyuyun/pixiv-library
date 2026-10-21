import pytest
from app.database import SessionLocal
from app.models import Item, ItemTag, Tag, ItemFavorite
from test_library import client


@pytest.fixture
def catalog(client):
    with SessionLocal() as db:
        db.add_all([
            Tag(id="blue", name="blue_eyes", name_zh="蓝眼睛"),
            Tag(id="comma", name="blue, red", name_zh="双色"),
            Tag(id="prefix", name="blue_eyes_extra"),
            Tag(id="substring", name="a_blue_eyes"),
            Tag(id="wild", name="blueXeyes"),
            Tag(id="unused", name="unused"),
            Item(id="both", title="Both", source="pixiv", type="manga"),
            Item(id="one", title="One", source="pixiv", type="novel"),
            Item(id="other", title="Other", source="local", type="manga"),
        ])
        db.flush()
        db.add_all([
            ItemTag(item_id="both", tag_id="blue", origin="source"),
            ItemTag(item_id="both", tag_id="blue", origin="ai"),
            ItemTag(item_id="both", tag_id="comma"),
            ItemTag(item_id="one", tag_id="blue"),
            ItemTag(item_id="other", tag_id="comma"),
            *[ItemTag(item_id="other", tag_id=id_) for id_ in ("prefix", "substring", "wild")],
            ItemFavorite(item_id="both"),
        ])
        db.commit()
    return client


def result(client, **params):
    response = client.get('/api/items', params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    return body['total'], {item['id'] for item in body['items']}


def test_all_any_and_duplicate_assignments(catalog):
    assert result(catalog, tag_ids='blue,comma') == (1, {'both'})
    assert result(catalog, tag_ids='blue,comma', tag_mode='any') == (3, {'both', 'one', 'other'})
    assert result(catalog, tag_ids='blue,blue, comma,,') == (1, {'both'})
    assert result(catalog, tag_ids='blue,unknown') == (0, set())
    assert result(catalog, tag_ids='blue,unknown', tag_mode='any') == (2, {'both', 'one'})


def test_filters_pagination_and_legacy_names(catalog):
    assert result(catalog, tag_ids='blue,comma', tag_mode='any', source='pixiv', type='novel') == (1, {'one'})
    assert result(catalog, tag_ids='blue,comma', tag_mode='any', favorite=True) == (1, {'both'})
    assert result(catalog, tag_ids='blue,comma', tag_mode='any', q='Both') == (1, {'both'})
    assert result(catalog, tag_ids='blue,comma', tag_mode='any', sort='title', page_size=1, page=2) == (3, {'one'})
    assert result(catalog, tag='blue, red') == (2, {'both', 'other'})
    assert result(catalog, tag='blue_eyes') == (2, {'both', 'one'})
    assert result(catalog, tag_ids='') == (3, {'both', 'one', 'other'})


def test_multiselect_limits_and_invalid_mode(catalog):
    for params in ({'tag_mode': 'bad'}, {'tag_ids': ','.join(str(i) for i in range(21))}, {'tag_ids': 'x' * 37}):
        assert catalog.get('/api/items', params=params).status_code == 422


def test_suggestions_rank_literal_original_and_translation(catalog):
    rows = catalog.get('/api/tags', params={'q': 'blue_eyes', 'suggest': True}).json()
    assert [row['id'] for row in rows] == ['blue', 'prefix', 'substring']
    assert rows[0]['count'] == 2  # Different origins count one book once.
    assert catalog.get('/api/tags', params={'q': '蓝眼', 'suggest': True}).json()[0]['id'] == 'blue'
    rows = catalog.get('/api/tags', params={'q': 'blue', 'suggest': True, 'source': 'pixiv'}).json()
    assert {row['id'] for row in rows} == {'blue', 'comma'}
    assert catalog.get('/api/tags', params={'q': 'unused', 'suggest': True}).json() == []


def test_suggestions_are_bounded_even_without_limit(catalog):
    with SessionLocal() as db:
        for n in range(30):
            tag = Tag(name=f'extra{n}')
            db.add(tag)
            db.flush()
            db.add(ItemTag(item_id='both', tag_id=tag.id))
        db.commit()
    assert len(catalog.get('/api/tags', params={'suggest': True}).json()) == 12
    assert len(catalog.get('/api/tags', params={'suggest': True, 'limit': 1000}).json()) == 20
