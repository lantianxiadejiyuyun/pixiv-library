from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from app import queue_control
from app.database import SessionLocal
from app.integrations.base import JobCancelled
from app.models import QueueControl
from test_library import client


def test_switching_off_releases_local_wait_without_sampling_new_intervals(client, monkeypatch):
    now = datetime(2026, 1, 1)
    monkeypatch.setattr(queue_control, 'utcnow', lambda: now)
    samples, waits = [], []
    monkeypatch.setattr(queue_control.random, 'uniform', lambda lo, hi: samples.append((lo, hi)) or hi)
    def sleep(seconds):
        waits.append(seconds)
        assert client.put('/api/queue/settings', json={'rate_limit_enabled': False}).status_code == 200
    monkeypatch.setattr(queue_control, 'time', SimpleNamespace(sleep=sleep))
    with queue_control.request_context(lambda: None):
        queue_control.wait_for_request()
        queue_control.wait_for_request()
        queue_control.wait_for_request()
    assert samples == [(2, 5)] and waits == [.25]
    with SessionLocal() as db:
        control = db.get(QueueControl, 1)
        assert control.next_request_at is None and control.min_interval_seconds == 2 and control.max_interval_seconds == 5


def test_disabled_pacing_preserves_429_backoff_across_off_on_toggle(client, monkeypatch):
    now = [datetime(2026, 1, 1)]
    monkeypatch.setattr(queue_control, 'utcnow', lambda: now[0])
    monkeypatch.setattr(queue_control.random, 'uniform', lambda *args: (_ for _ in ()).throw(AssertionError('disabled pacing sampled')))
    waits = []
    def sleep(seconds):
        waits.append(seconds); now[0] += timedelta(seconds=seconds)
    monkeypatch.setattr(queue_control, 'time', SimpleNamespace(sleep=sleep))
    queue_control.update_settings({'rate_limit_enabled': False})
    with queue_control.request_context(lambda: None):
        queue_control.defer_requests(2)
        queue_control.update_settings({'rate_limit_enabled': True})
        queue_control.update_settings({'rate_limit_enabled': False})
        queue_control.wait_for_request()
        queue_control.wait_for_request()
    assert sum(waits) == 2
    with SessionLocal() as db:
        assert db.get(QueueControl, 1).backoff_until == now[0]


def test_disabled_pacing_still_obeys_pause_cancellation_and_single_worker(client):
    settings = queue_control.update_settings({'rate_limit_enabled': False, 'paused': True})
    assert settings['concurrency'] == 1
    with queue_control.request_context(lambda: None), pytest.raises(queue_control.QueuePaused):
        queue_control.wait_for_request()
    queue_control.update_settings({'paused': False})
    with queue_control.request_context(lambda: (_ for _ in ()).throw(JobCancelled())), pytest.raises(JobCancelled):
        queue_control.wait_for_request()
