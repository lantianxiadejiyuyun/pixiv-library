"""An AI task retried in its dedicated process reports its actual device."""
import sys

import pytest

from app.ai import service
from app.config import settings
from app import worker
from app.worker_runtime import supervisor
from test_library import client


@pytest.fixture
def model_key(client, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'model_dir', tmp_path)
    monkeypatch.setenv('LIBRARY_AI_PROVIDER', 'auto')
    monkeypatch.delenv('LIBRARY_WORKER_PROCESS', raising=False)
    folder = tmp_path / 'wd-tagger'
    folder.mkdir()
    files = [folder / name for name in ('model.onnx', 'selected_tags.csv')]
    for path in files:
        path.write_text('synthetic')
    return [path.stat().st_mtime_ns for path in files]


@pytest.mark.parametrize('state,provider', [('ready', 'CPUExecutionProvider'),
                                          ('fallback', 'CPUExecutionProvider'),
                                          ('ready', 'OpenVINOExecutionProvider')])
def test_only_retry_inference_updates_actual_execution_state(model_key, monkeypatch, state, provider):
    idle = {'requested_provider': 'auto', 'execution_state': 'not_loaded'}
    actual = {'requested_provider': 'auto', 'execution_state': state, 'actual_provider': provider}
    monkeypatch.setattr(supervisor, 'worker_data', lambda lane:
                        {'model_key': model_key, 'ai_status': actual if lane == 'retry' else idle})
    assert service.model_status() == actual


@pytest.mark.parametrize('mismatch', ['model', 'provider', 'heartbeat'])
def test_retry_status_from_old_model_provider_or_stale_worker_is_ignored(model_key, monkeypatch, mismatch):
    idle = {'requested_provider': 'auto', 'execution_state': 'not_loaded'}
    stale = {'model_key': [0, 0] if mismatch == 'model' else model_key,
             'ai_status': {'requested_provider': 'intel' if mismatch == 'provider' else 'auto',
                           'execution_state': 'ready', 'actual_provider': 'CPUExecutionProvider'}}
    monkeypatch.setattr(supervisor, 'worker_data', lambda lane:
                        {'model_key': model_key, 'ai_status': idle} if lane == 'ai' else
                        None if mismatch == 'heartbeat' else stale)
    assert service.model_status() == idle


def test_regular_ai_worker_remains_authoritative_after_it_has_run(model_key, monkeypatch):
    regular = {'requested_provider': 'auto', 'execution_state': 'ready', 'actual_provider': 'OpenVINOExecutionProvider'}
    retried = {'requested_provider': 'auto', 'execution_state': 'fallback', 'actual_provider': 'CPUExecutionProvider'}
    monkeypatch.setattr(supervisor, 'worker_data', lambda lane:
                        {'model_key': model_key, 'ai_status': regular if lane == 'ai' else retried})
    assert service.model_status() == regular


def test_retry_process_publishes_its_own_model_status_after_ai_runs(model_key, monkeypatch):
    actual = {'requested_provider': 'auto', 'execution_state': 'ready', 'actual_provider': 'CPUExecutionProvider'}
    monkeypatch.setattr(service, 'model_status', lambda: actual)
    assert worker.ai_snapshot('retry') == {'model_key': model_key, 'ai_status': actual}
    assert worker.ai_snapshot('download') == {}


def test_retry_downloads_do_not_import_ai_runtime_for_heartbeat(monkeypatch):
    monkeypatch.delitem(sys.modules, 'app.ai.service')
    assert worker.ai_snapshot('retry') == {}
