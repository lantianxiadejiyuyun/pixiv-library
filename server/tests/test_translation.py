import json

import httpx
import pytest
from sqlalchemy import select

from app import jobs, translation
from app.database import SessionLocal
from app.integrations.base import ConnectorError
from app.models import Item, ItemTag, Job, NetworkSetting, Tag
from test_devices import bind, bearer
from test_library import client, upload


def configure(client, **changes):
    data = {"enabled": True, "provider": "ollama", "base_url": "http://127.0.0.1:11434",
            "model": "local-translate", **changes}
    response = client.put('/api/translation/settings', json=data)
    assert response.status_code == 200, response.text
    return response.json()


def sample(client, title='A quiet afternoon'):
    item = upload(client, title=title).json()
    response = client.patch('/api/items/' + item['id'], json={'author': 'Exact Author', 'tags': ['library', 'blue_eyes']})
    assert response.status_code == 200
    return response.json()


def test_chinese_search_exact_author_and_tag_identity(client):
    first = sample(client)
    other = sample(client, title='Unrelated')
    client.patch('/api/items/' + other['id'], json={'author': 'Exact Author Extended', 'tags': ['other']})
    with SessionLocal() as db:
        item = db.get(Item, first['id'])
        item.title_zh, item.title_zh_source = '安静的午后', item.title
        db.scalar(select(Tag).where(Tag.name == 'library')).name_zh = '图书馆'
        db.scalar(select(Tag).where(Tag.name == 'blue_eyes')).name_zh = '蓝眼睛'
        # Multiple tag origins must not multiply the count.
        tag = item.item_tags[0].tag
        db.add(ItemTag(item_id=item.id, tag_id=tag.id, origin='source'))
        db.commit()
    for term in ['午后', 'afternoon', '图书馆', 'library']:
        assert [row['id'] for row in client.get('/api/items', params={'q': term}).json()['items']] == [first['id']]
    results = client.get('/api/items', params={'author': 'Exact Author', 'source': 'local'}).json()
    assert results['total'] == 1 and results['items'][0]['display_title'] == '安静的午后'
    tags = client.get('/api/tags', params={'q': '图书馆', 'source': 'local', 'limit': 1}).json()
    assert len(tags) == 1 and tags[0]['name'] == 'library' and tags[0]['count'] == 1
    assert client.get('/api/items', params={'tag': tags[0]['id']}).json()['total'] == 1
    assert client.get('/api/tags', params={'q': '_'}).json()[0]['name'] == 'blue_eyes'
    assert client.get('/api/tags', params={'source': 'pixiv'}).json() == []
    client.patch('/api/items/' + first['id'], json={'title': 'Changed title'})
    detail = client.get('/api/items/' + first['id']).json()
    assert detail['title_zh'] is None and detail['display_title'] == 'Changed title'
    assert client.get('/api/items', params={'q': '午后'}).json()['total'] == 0


def test_device_may_translate_but_never_read_or_change_model_configuration(client):
    item = sample(client)
    configure(client, api_key='private-model-secret')
    device = bind(client)
    headers = bearer(device)
    assert client.get('/api/translation/status', headers=headers).json()['enabled']
    for path, method in [('/translation/settings', 'get'), ('/translation/settings', 'put'),
                         ('/translation/test', 'post'), ('/translation/batch', 'post')]:
        response = getattr(client, method)('/api' + path, headers=headers)
        assert response.status_code == 403 and 'private-model-secret' not in response.text
    assert client.post('/api/items/' + item['id'] + '/translate', headers=headers, json={'consent': False}).status_code == 422
    first = client.post('/api/items/' + item['id'] + '/translate', headers=headers, json={'consent': True}).json()
    second = client.post('/api/items/' + item['id'] + '/translate', headers=headers, json={'consent': True}).json()
    assert first['job_id'] == second['job_id']
    state = client.get('/api/items/' + item['id'] + '/translation', headers=headers).json()
    assert state['status'] == 'queued' and 'private-model-secret' not in json.dumps(state)
    with SessionLocal() as db:
        assert 'private-model-secret' not in db.get(NetworkSetting, translation.KEY).value_encrypted
        assert 'private-model-secret' not in json.dumps(db.get(Job, first['job_id']).payload)


def test_configuration_secret_preservation_and_endpoint_change(client):
    assert not client.get('/api/translation/status').json()['enabled']
    configure(client, api_key='private-model-secret')
    assert configure(client)['has_api_key']
    assert not configure(client, base_url='http://127.0.0.1:11435')['has_api_key']
    for address in ['https://name:password@example.com/v1', 'http://example.com', 'http://169.254.169.254',
                    'https://example.com/v1?key=secret', 'https://example.com/a/../v1']:
        response = client.put('/api/translation/settings', json={'enabled': True, 'provider': 'openai',
            'model': 'translate', 'base_url': address})
        assert response.status_code == 422


def test_job_persists_chinese_without_changing_originals_and_reuses_shared_tag_cache(client, monkeypatch):
    item = sample(client)
    configure(client)
    calls = []
    def model(config, title, tags):
        calls.append((title, tags))
        return ('安静的午后' if title is not None else None), {key: '中文标签' for key in tags}
    monkeypatch.setattr(translation, 'model_translate', model)
    queued = client.post('/api/items/' + item['id'] + '/translate', json={'consent': True}).json()
    jobs._stop.clear()
    assert jobs.run_once()
    detail = client.get('/api/items/' + item['id']).json()
    assert detail['title'] == item['title'] and detail['title_zh'] == '安静的午后'
    assert {tag['name'] for tag in detail['tags']} == {'library', 'blue_eyes'}
    assert all(tag['name_zh'] == '中文标签' for tag in detail['tags'])
    assert client.get('/api/items/' + item['id'] + '/translation').json()['status'] == 'completed'
    cached = client.post('/api/items/' + item['id'] + '/translate', json={'consent': True}).json()
    assert cached['cached'] and len(calls) == 1
    second = sample(client, title='Another title')
    client.post('/api/items/' + second['id'] + '/translate', json={'consent': True})
    assert jobs.run_once()
    assert calls[-1][1] == {}  # Same tag IDs already have Chinese names.
    with SessionLocal() as db:
        assert db.get(Job, queued['job_id']).status == 'completed'


@pytest.mark.parametrize('change', ['title', 'delete', 'config'])
def test_slow_results_never_overwrite_changed_deleted_or_reconfigured_work(client, monkeypatch, change):
    item = sample(client)
    configure(client)
    with SessionLocal() as db:
        payload = {'item_id': item['id'], 'settings_revision': translation.fingerprint(translation.read_config(db))}
    def model(config, title, tags):
        with SessionLocal() as separate:
            current = separate.get(Item, item['id'])
            if change == 'delete':
                separate.delete(current)
            elif change == 'title':
                current.title = 'Edited during inference'
            else:
                row = separate.get(NetworkSetting, translation.KEY)
                row.value_encrypted = translation.encrypt_credentials({**config, 'enabled': False})
            separate.commit()
        return '过期结果', {key: '过期标签' for key in tags}
    monkeypatch.setattr(translation, 'model_translate', model)
    with SessionLocal() as db, pytest.raises(ConnectorError):
        translation.process_translation(db, payload, lambda value: None, lambda db: None)
    with SessionLocal() as db:
        current = db.get(Item, item['id'])
        assert current is None or current.title_zh is None
        assert all(tag.name_zh is None for tag in db.scalars(select(Tag)))


@pytest.mark.parametrize('provider', ['ollama', 'openai'])
def test_real_http_contract_for_both_model_types_and_no_redirects(client, monkeypatch, provider):
    config = {**translation.DEFAULTS, 'enabled': True, 'model': 'translate', 'provider': provider,
              'base_url': 'http://127.0.0.1:11434' if provider == 'ollama' else 'https://model.example/v1',
              'api_key': 'private-model-secret'}
    real_client = httpx.Client
    seen = []
    def handler(request):
        data = json.loads(request.content)
        seen.append(request)
        assert data['stream'] is False and data['model'] == 'translate'
        assert request.headers['Authorization'] == 'Bearer private-model-secret'
        assert json.loads(data['messages'][1]['content']) == {'title': 'Title', 'tags': [{'id': 'id1', 'text': 'library'}]}
        result = json.dumps({'title': '标题', 'tags': [{'id': 'id1', 'text': '图书馆'}]})
        return httpx.Response(200, json={'message': {'content': result}} if provider == 'ollama'
            else {'choices': [{'message': {'content': result}, 'finish_reason': 'stop'}]})
    def create(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return real_client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(translation.httpx, 'Client', create)
    assert translation.model_translate(config, 'Title', {'id1': 'library'}) == ('标题', {'id1': '图书馆'})
    assert seen[0].url.path == ('/api/chat' if provider == 'ollama' else '/v1/chat/completions')


@pytest.mark.parametrize('response', [
    {'title': '标题', 'tags': []},
    {'title': '标题', 'tags': [{'id': 'wrong', 'text': '标签'}]},
    {'title': '标题', 'tags': [{'id': 'id1', 'text': '标签'}, {'id': 'id1', 'text': '重复'}]},
    {'title': '', 'tags': [{'id': 'id1', 'text': '标签'}]},
])
def test_invalid_model_output_is_rejected_atomically(monkeypatch, response):
    real_client = httpx.Client
    monkeypatch.setattr(translation.httpx, 'Client', lambda **kwargs: real_client(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'message': {'content': json.dumps(response)}}))))
    with pytest.raises(ConnectorError, match='完整的翻译'):
        translation.model_translate(translation.DEFAULTS, 'Title', {'id1': 'library'})


def test_batch_is_bounded_and_duplicate_safe(client):
    sample(client)
    sample(client, title='Another title')
    configure(client)
    assert client.post('/api/translation/batch', json={'consent': True, 'limit': 1}).json()['queued'] == 1
    assert client.post('/api/translation/batch', json={'consent': True, 'limit': 1}).json()['queued'] == 1
    assert client.post('/api/translation/batch', json={'consent': True, 'limit': 1}).json()['queued'] == 0
