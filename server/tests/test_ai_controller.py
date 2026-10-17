import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ai_controller import main as controller, vision
from ai_controller.docker_engine import DockerEngine, NODE_LABEL, SERVICE_LABEL
from ai_controller.operations import Operations

KEY = 'controller-fixture-' + 'x'*32
HEADERS = {'Authorization': 'Bearer '+KEY}


@pytest.fixture
def node(monkeypatch, tmp_path):
    monkeypatch.setenv('QIYE_CONTROLLER_KEY', KEY)
    monkeypatch.setenv('QIYE_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('QIYE_NODE_ID', 'fixture-node')
    with TestClient(controller.app) as client:
        yield client


@pytest.mark.parametrize('path', ['/v1/status', '/v1/ollama/models', '/v1/operations'])
def test_agent_requires_key_before_any_docker_or_model_access(node, monkeypatch, path):
    monkeypatch.setattr(controller, 'DockerEngine', lambda: pytest.fail('Unauthorized Docker access'))
    assert node.get(path).status_code == 401
    assert node.get(path, headers={'Authorization': 'Bearer wrong-key'}).status_code == 401


def test_agent_fails_startup_without_a_random_key(monkeypatch):
    monkeypatch.delenv('QIYE_CONTROLLER_KEY', raising=False)
    with pytest.raises(RuntimeError, match='QIYE_CONTROLLER_KEY'):
        with TestClient(controller.app):
            pass


@pytest.mark.parametrize('service,action', [('db', 'stop'), ('ollama', 'exec'), ('vision', 'remove'), ('controller', 'restart')])
def test_no_arbitrary_container_or_command_surface(node, service, action):
    assert node.post(f'/v1/services/{service}/{action}', headers=HEADERS).status_code == 422
    assert node.post('/containers/create', headers=HEADERS, json={'Image': 'untrusted'}).status_code == 404


def docker_fixture(monkeypatch, *, labels=None, count=1):
    monkeypatch.setenv('QIYE_NODE_ID', 'fixture-node')
    seen = []
    def handle(request):
        seen.append(request)
        assert request.url.host == 'docker'
        if request.url.path == '/version':
            return httpx.Response(200, json={'ApiVersion': '1.51'})
        if request.url.path.endswith('/containers/json'):
            filters = json.loads(request.url.params['filters'])
            assert NODE_LABEL+'=fixture-node' in filters['label']
            row = {'Id': 'a'*64, 'State': 'running', 'Status': 'Up', 'Image': 'synthetic',
                'Labels': labels if labels is not None else {NODE_LABEL: 'fixture-node', SERVICE_LABEL: 'ollama'}}
            return httpx.Response(200, json=[row]*count)
        assert request.url.path == '/v1.51/containers/'+'a'*64+'/stop'
        assert request.method == 'POST' and request.url.params['t'] == '15'
        return httpx.Response(204)
    return DockerEngine(transport=httpx.MockTransport(handle)), seen


def test_docker_actions_negotiate_version_and_require_matching_node_labels(monkeypatch):
    engine, seen = docker_fixture(monkeypatch)
    assert engine.action('ollama', 'stop')['service'] == 'ollama'
    assert len([request for request in seen if request.method == 'POST']) == 1


@pytest.mark.parametrize('labels,count', [({NODE_LABEL: 'other-node', SERVICE_LABEL: 'ollama'}, 1),
    ({NODE_LABEL: 'fixture-node', SERVICE_LABEL: 'database'}, 1), (None, 2)])
def test_wrong_or_duplicate_labels_never_reach_docker_mutation(monkeypatch, labels, count):
    engine, seen = docker_fixture(monkeypatch, labels=labels, count=count)
    with pytest.raises(HTTPException):
        engine.action('ollama', 'stop')
    assert not any(request.method == 'POST' for request in seen)


def test_uncreated_container_does_not_create_or_mount_host_paths(monkeypatch):
    engine, seen = docker_fixture(monkeypatch, count=0)
    assert engine.status('ollama')['state'] == 'not_created'
    with pytest.raises(HTTPException) as failure:
        engine.action('ollama', 'start')
    assert failure.value.status_code == 409
    assert not any(request.method == 'POST' for request in seen)


def test_ollama_chat_only_accepts_bounded_text_requests_and_nonstreaming_results(node, monkeypatch):
    calls = []
    monkeypatch.setattr(controller, 'json_request', lambda method, url, **kw: calls.append((method, url, kw)) or {'done': True, 'message': {'content': '{}'}})
    base = {'model': 'synthetic:1', 'messages': [{'role': 'user', 'content': '合成数据'}], 'stream': False}
    assert node.post('/v1/ollama/api/chat', headers=HEADERS, json=base).status_code == 200
    assert calls[0][1] == 'http://ollama:11434/api/chat'
    assert node.post('/v1/ollama/api/chat', headers=HEADERS, json={**base, 'stream': True}).status_code == 422
    assert node.post('/v1/ollama/api/chat', headers=HEADERS, json={**base, 'tools': [{'function': 'host-exec'}]}).status_code == 422
    assert node.post('/v1/ollama/api/chat', headers=HEADERS, json={**base, 'options': {'num_predict': 999999}}).status_code == 422
    assert len(calls) == 1


def test_pull_deduplicates_and_restart_marks_incomplete_operations_failed(node, monkeypatch):
    store = Operations()
    monkeypatch.setattr(store.executor, 'submit', lambda *a, **kw: None)
    first = store.enqueue('synthetic:1', 'http://ollama:11434')
    assert store.enqueue('synthetic:1', 'http://ollama:11434')['id'] == first['id']
    for index in range(7):
        store.enqueue('synthetic:'+str(index+2), 'http://ollama:11434')
    with pytest.raises(HTTPException) as failure:
        store.enqueue('synthetic:overflow', 'http://ollama:11434')
    assert failure.value.status_code == 429
    store.recover()
    assert store.read(first['id'])['status'] == 'failed'
    store.executor.shutdown(wait=False)


def test_pull_stream_progress_is_persisted_without_untrusted_error_text(node, monkeypatch):
    store = Operations()
    original = httpx.Client
    output = [b'{"status":"pulling","total":100,"completed":30}\n{"status":"success"}\n']
    def factory(**kwargs):
        return original(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=output[0])), **kwargs)
    monkeypatch.setattr(httpx, 'Client', factory)
    value = {'id': '00000000-0000-4000-8000-000000000001', 'model': 'synthetic:1', 'status': 'queued', 'completed': 0, 'total': 0, 'phase': '', 'error': None}
    store.pull(dict(value), 'http://ollama:11434')
    done = store.read(value['id'])
    assert done['status'] == 'completed' and done['completed'] == 30 and done['total'] == 100
    output[0] = b'{"error":"private-key-not-to-echo"}\n'
    store.pull(dict(value), 'http://ollama:11434')
    assert store.read(value['id'])['status'] == 'failed'
    assert 'private-key' not in json.dumps(store.read(value['id']))
    store.executor.shutdown(wait=False)


def test_vision_auth_and_invalid_image_never_run_model(monkeypatch):
    monkeypatch.setenv('QIYE_CONTROLLER_KEY', KEY)
    from app.ai import service
    monkeypatch.setattr(service, 'infer_image', lambda _: pytest.fail('Invalid image reached model'))
    with TestClient(vision.app) as client:
        assert client.post('/v1/vision/analyze', content=b'not-an-image').status_code == 401
        assert client.post('/v1/vision/analyze', content=b'not-an-image', headers=HEADERS).status_code == 422


def test_vision_uses_temporary_file_and_removes_it_after_inference(monkeypatch):
    monkeypatch.setenv('QIYE_CONTROLLER_KEY', KEY)
    from app.ai import service
    from test_library import png
    paths = []
    def infer(path):
        assert path.exists() and path.read_bytes().startswith(b'\x89PNG')
        paths.append(path)
        return {'rating': 'general'}
    monkeypatch.setattr(service, 'infer_image', infer)
    with TestClient(vision.app) as client:
        assert client.post('/v1/vision/analyze', content=png(), headers=HEADERS).status_code == 200
    assert paths and not paths[0].exists()


def test_busy_vision_returns_explicit_backpressure(monkeypatch):
    monkeypatch.setenv('QIYE_CONTROLLER_KEY', KEY)
    assert vision._inference.acquire(blocking=False)
    try:
        with TestClient(vision.app) as client:
            assert client.post('/v1/vision/analyze', content=b'fixture', headers=HEADERS).status_code == 429
    finally:
        vision._inference.release()


def test_unauthorized_requests_are_rejected_before_receiving_any_body(monkeypatch):
    import asyncio
    from ai_controller.common import ControllerGate
    monkeypatch.setenv('QIYE_CONTROLLER_KEY', KEY)
    messages = []
    async def receive(): pytest.fail('Unauthorized body must not be read')
    async def application(*args): pytest.fail('Unauthorized request reached application')
    async def send(message): messages.append(message)
    asyncio.run(ControllerGate(application)({'type': 'http', 'path': '/v1/ollama/api/chat', 'headers': []}, receive, send))
    assert messages[0]['status'] == 401


@pytest.mark.parametrize('path,maximum', [('/v1/ollama/api/chat', 512*1024), ('/v1/vision/analyze', 16*1024*1024)])
def test_controller_rejects_oversized_body_before_parsing(node, path, maximum):
    assert node.post(path, headers={**HEADERS, 'Content-Length': str(maximum+1)}, content=b'fixture').status_code == 413
