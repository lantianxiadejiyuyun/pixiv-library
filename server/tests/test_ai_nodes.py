import io
import json
import zipfile

import httpx
import pytest
from sqlalchemy import select

from app import ai_nodes, jobs, translation
from app.ai import service
from app.database import SessionLocal
from app.integrations.base import ConnectorError
from app.library import import_files
from app.models import Item, ItemTag, NetworkSetting
from test_devices import bind, bearer
from test_library import client, png

KEY = 'synthetic-controller-key-' + 'a' * 32
URL = '/api/ai/nodes'


def add(client, **changes):
    response = client.post(URL, json={'label': 'AI 电脑', 'base_url': 'http://192.168.1.20:9010', 'key': KEY, **changes})
    assert response.status_code == 200, response.text
    return response.json()['id']


def prediction(**changes):
    return {'model_version': 'wd-tagger:synthetic', 'rating': 'adult',
        'rating_scores': {'general': .01, 'sensitive': .02, 'questionable': .07, 'explicit': .9},
        'tags': [{'name': '角色:合成人物', 'score': .99, 'category': 4}],
        'execution': {'actual_provider': 'CPUExecutionProvider', 'device': 'CPU'}, **changes}


def test_registry_encrypts_secret_and_never_returns_it(client):
    ident = add(client)
    assert KEY not in client.get(URL).text
    with SessionLocal() as db:
        stored = db.get(NetworkSetting, ai_nodes.KEY).value_encrypted
        assert KEY not in stored and '192.168.1.20' not in stored
        assert ai_nodes.node_by_id(db, ident)['key'] == KEY
    assert client.get(URL).json()['items'][0]['has_key']


def test_node_control_is_admin_only_even_for_paired_readers(client):
    ident = add(client)
    token = bearer(bind(client))
    assert client.get(URL, headers=token).status_code == 403
    assert client.post(URL+'/'+ident+'/test', headers=token).status_code == 403
    assert client.post(URL+'/'+ident+'/services/ollama/stop', headers=token).status_code == 403
    client.cookies.clear()
    assert client.get(URL).status_code == 401


@pytest.mark.parametrize('value', ['http://example.com:9010', 'http://169.254.169.254',
    'http://user:password@192.168.1.20', 'http://192.168.1.20/?key=secret',
    'http://192.168.1.20/../docker', 'file:///var/run/docker.sock'])
def test_node_addresses_reject_credentials_queries_and_metadata_hosts(client, value):
    assert client.post(URL, json={'label': 'Synthetic', 'base_url': value, 'key': KEY}).status_code == 422


def test_edit_preserves_key_only_for_same_address_and_does_not_enable_routing(client):
    ident = add(client)
    value = {'label': '改名', 'base_url': 'http://192.168.1.20:9010', 'enabled': True}
    assert client.put(URL+'/'+ident, json=value).status_code == 200
    assert client.get(URL).json()['image_node_id'] is None
    assert client.put(URL+'/'+ident, json={**value, 'base_url': 'http://192.168.1.21:9010'}).status_code == 400
    with SessionLocal() as db:
        assert ai_nodes.node_by_id(db, ident)['key'] == KEY


def test_active_image_and_translation_nodes_cannot_be_removed(client):
    ident = add(client)
    assert client.put(URL+'/routing', json={'image_node_id': ident}).status_code == 200
    assert client.delete(URL+'/'+ident).status_code == 409
    assert client.put(URL+'/routing', json={'image_node_id': None}).status_code == 200
    assert client.post(URL+'/'+ident+'/translation', json={'model': 'synthetic:1'}).status_code == 200
    assert client.delete(URL+'/'+ident).status_code == 409
    settings = client.get('/api/translation/settings').json()
    assert settings['provider'] == 'controller' and settings['controller_node_id'] == ident
    assert KEY not in json.dumps(settings)


def test_disabled_selected_node_reports_unavailable_and_does_not_fall_back(client):
    ident = add(client)
    client.put(URL+'/routing', json={'image_node_id': ident}).raise_for_status()
    client.put(URL+'/'+ident, json={'label': 'AI 电脑', 'base_url': 'http://192.168.1.20:9010', 'enabled': False}).raise_for_status()
    state = client.get('/api/ai/status').json()
    assert state['available'] is False and state['provider'] == 'remote'
    with SessionLocal() as db, pytest.raises(ConnectorError, match='未切换'):
        ai_nodes.image_target(db)


def test_controller_transport_uses_only_selected_origin_bearer_and_no_redirects(monkeypatch):
    original = httpx.Client
    seen = []
    def factory(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        def handle(request):
            seen.append(request)
            assert request.headers['Authorization'] == 'Bearer '+KEY
            return httpx.Response(302, headers={'Location': 'https://other.test/private'})
        return original(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(ai_nodes.httpx, 'Client', factory)
    with pytest.raises(ConnectorError, match='HTTP 302') as error:
        ai_nodes.request_node({'base_url': 'http://192.168.1.20:9010', 'key': KEY}, 'GET', '/v1/status')
    assert len(seen) == 1 and str(seen[0].url) == 'http://192.168.1.20:9010/v1/status'
    assert KEY not in str(error.value)


def test_remote_image_analysis_uses_existing_ai_queue_and_preserves_manual_annotations(client, monkeypatch):
    ident = add(client)
    client.put(URL+'/routing', json={'image_node_id': ident}).raise_for_status()
    with SessionLocal() as db:
        item = import_files(db, [('page.png', png())], title='合成作品', tags=['人工标签'])
        item.rating, item.rating_origin = 'general', 'manual'; db.commit(); item_id = item.id
    calls = []
    def infer(node, method, path, **kwargs):
        assert node['id'] == ident and path == '/v1/vision/analyze'
        assert kwargs['content'].startswith(b'\x89PNG')
        calls.append(node['id'])
        return prediction()
    monkeypatch.setattr(ai_nodes, 'request_node', infer)
    monkeypatch.setattr(service, 'infer_image', lambda _: pytest.fail('Local model must not run'))
    jobs._stop.clear()
    response = client.post('/api/ai/analyze', json={'item_ids': [item_id]}); response.raise_for_status()
    assert jobs.run_once(('ai',))
    job = client.get('/api/jobs/'+response.json()['jobs'][0]['id']).json()
    assert job['status'] == 'completed', job
    assert calls == [ident]
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        assert item.rating == 'general' and item.rating_origin == 'manual'
        assert {link.tag.name for link in item.item_tags} == {'人工标签', '角色:合成人物'}


@pytest.mark.parametrize('changes', [
    {'rating': 'general'}, {'rating_scores': {'unexpected': .99}},
    {'tags': [{'name': 'Synthetic', 'score': 2, 'category': 0}]},
    {'execution': {'secret': 'not-a-status-field'}}, {'rating_scores': {'explicit': float('nan')}},
])
def test_invalid_remote_predictions_do_not_enter_library(client, monkeypatch, tmp_path, changes):
    path = tmp_path/'image.png'; path.write_bytes(png())
    monkeypatch.setattr(ai_nodes, 'request_node', lambda *a, **kw: prediction(**changes))
    with pytest.raises(ConnectorError, match='未保存'):
        ai_nodes.infer_remote({}, path)


def test_large_transparent_image_is_resized_with_white_background_without_reading_all_bytes(monkeypatch, tmp_path):
    from PIL import Image
    from types import SimpleNamespace
    image_path = tmp_path/'transparent.png'
    Image.new('RGBA', (20, 20), (0, 0, 0, 0)).save(image_path)
    class LargePath:
        def stat(self): return SimpleNamespace(st_size=17*1024*1024)
        def __fspath__(self): return str(image_path)
        def read_bytes(self): pytest.fail('Large original must not be buffered twice')
    def request(*args, **kwargs):
        with Image.open(io.BytesIO(kwargs['content'])) as resized:
            assert resized.format == 'JPEG' and resized.getpixel((0, 0)) == (255, 255, 255)
        return prediction()
    monkeypatch.setattr(ai_nodes, 'request_node', request)
    assert ai_nodes.infer_remote({}, LargePath())['rating'] == 'adult'


def test_changing_node_during_inference_preserves_existing_ai_results(client, monkeypatch):
    ident = add(client)
    client.put(URL+'/routing', json={'image_node_id': ident}).raise_for_status()
    with SessionLocal() as db:
        item = import_files(db, [('page.png', png())], title='合成作品', tags=['人工标签']); item_id = item.id
    def infer(*args, **kwargs):
        client.put(URL+'/routing', json={'image_node_id': None}).raise_for_status()
        return prediction()
    monkeypatch.setattr(ai_nodes, 'request_node', infer)
    with SessionLocal() as db:
        with pytest.raises(ConnectorError, match='配置已更改'):
            service.analyze_item(db, item_id)
        db.rollback()
        assert [link.tag.name for link in db.get(Item, item_id).item_tags] == ['人工标签']


def test_controller_translation_uses_node_credentials_and_existing_output_validation(client, monkeypatch):
    ident = add(client)
    client.post(URL+'/'+ident+'/translation', json={'model': 'synthetic:1'}).raise_for_status()
    original = httpx.Client
    seen = []
    def factory(**kwargs):
        def handle(request):
            seen.append(request)
            assert str(request.url) == 'http://192.168.1.20:9010/v1/ollama/api/chat'
            assert request.headers['Authorization'] == 'Bearer '+KEY
            return httpx.Response(200, json={'done': True, 'message': {'content': json.dumps({'title': '安静的午后', 'tags': [{'id': 'sample', 'text': '图书馆'}]})}})
        return original(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(translation.httpx, 'Client', factory)
    response = client.post('/api/translation/test')
    assert response.status_code == 200 and response.json()['tag'] == '图书馆'
    assert len(seen) == 1


def test_controller_key_changes_invalidate_queued_translation_revision(client):
    ident = add(client)
    client.post(URL+'/'+ident+'/translation', json={'model': 'synthetic:1'}).raise_for_status()
    with SessionLocal() as db:
        previous = translation.fingerprint(translation.read_config(db))
    client.put(URL+'/'+ident, json={'label': 'AI 电脑', 'base_url': 'http://192.168.1.20:9010', 'key': 'b'*43}).raise_for_status()
    with SessionLocal() as db:
        assert previous != translation.fingerprint(translation.read_config(db))


def test_deployment_zip_contains_no_live_configuration_or_models(client):
    response = client.get(URL+'/deployment'); response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert 'deploy/compose.ai-node.yaml' in names
        assert 'scripts/bootstrap_ai_node.py' in names
        assert 'deploy/ai-node.env' not in names
        assert not any(name.endswith(('.onnx', '.db', '.pyc')) for name in names)
