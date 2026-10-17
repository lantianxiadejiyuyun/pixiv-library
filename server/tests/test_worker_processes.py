"""CPU placement, dedicated claims and actual supervised process lifecycle."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from sqlalchemy import event, select

from app import cpu_affinity, jobs, queue_control
from app.config import settings
from app.database import SessionLocal, engine
from app.integrations.credentials import encrypt_credentials
from app.models import Item, Job, NetworkSetting, Subscription, utcnow
from app.worker_runtime import WorkerSupervisor
from test_library import client


def test_auto_placement_keeps_api_and_every_lane_disjoint_on_16_cpus():
    plan = cpu_affinity.plan(range(16))
    assert plan['api'] == [0, 1]
    assert len(plan['ai']) == 14 - (len(cpu_affinity.ROLES) - 2) * len(plan['download'])
    assert set(sum(plan.values(), [])) == set(range(16))
    assert not cpu_affinity.shared(plan)


def test_actual_os_affinity_is_applied_in_child_without_restricting_test_process():
    import subprocess
    import sys
    original = cpu_affinity.available_cpus()
    if os.name != 'nt' and not hasattr(os, 'sched_setaffinity'):
        pytest.skip('Affinity is not supported on this OS')
    code = f'from app.cpu_affinity import apply,available_cpus; import json; assert apply({original[:1]!r}); print(json.dumps(available_cpus()))'
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(result.stdout) == original[:1]
    assert cpu_affinity.available_cpus() == original


@pytest.mark.parametrize('count', [1, 2, 4, 6, 7, 8])
def test_small_nas_and_sparse_cpuset_never_select_inaccessible_cpus(count):
    available = [3 + index * 2 for index in range(count)]
    plan = cpu_affinity.plan(available)
    assert set(plan) == set(cpu_affinity.ROLES)
    assert all(ids and set(ids) <= set(available) for ids in plan.values())
    assert cpu_affinity.shared(plan) == (count < len(cpu_affinity.ROLES))
    if count > 1:
        assert all(not set(ids) & set(plan['api']) for role, ids in plan.items() if role != 'api')


@pytest.mark.parametrize('value', ['{}', '[]', 'bad-json', '{"api":[0]}'])
def test_manual_affinity_rejects_invalid_or_incomplete_maps(value):
    with pytest.raises(ValueError): cpu_affinity.plan(range(8), value)


def test_manual_affinity_strict_validation_and_explicit_shared_mode():
    plan = {role: [0] for role in cpu_affinity.ROLES}
    assert cpu_affinity.shared(cpu_affinity.plan([0, 3], json.dumps(plan)))
    for invalid in ([9], [True], [], [0, 0], ['0']):
        plan['download'] = invalid
        with pytest.raises(ValueError): cpu_affinity.plan([0, 3], json.dumps(plan))
    assert all(not ids for ids in cpu_affinity.plan([0, 1], 'off').values())


def test_auto_mode_falls_back_to_process_isolation_when_os_denies_affinity(monkeypatch):
    monkeypatch.setenv('LIBRARY_CPU_AFFINITY', 'auto')
    monkeypatch.setattr(cpu_affinity, 'available_cpus', lambda: [0, 1])
    def denied(_): raise PermissionError('synthetic OS policy')
    monkeypatch.setattr(cpu_affinity, 'apply', denied)
    worker = WorkerSupervisor(); worker.prepare()
    assert not worker.affinity_applied and all(not ids for ids in worker.placement.values())


def test_download_process_never_claims_details_or_ai(client, monkeypatch):
    with SessionLocal() as db:
        details = jobs.enqueue(db, 'import', {'platform': 'jm', 'download': False}).id
        ai = jobs.enqueue(db, 'ai', {}).id
        body = jobs.enqueue(db, 'import_content', {'platform': 'jm'}).id
    claim = jobs.claim_once(('download',), preflight=True)
    assert claim and claim[0] == body
    assert not jobs.claim_once(('download',), preflight=True)
    with SessionLocal() as db:
        assert db.get(Job, details).status == db.get(Job, ai).status == 'queued'


def test_paused_or_empty_lane_does_not_take_global_write_lock(client, monkeypatch):
    queue_control.update_settings({'paused': True})
    with SessionLocal() as db:
        jobs.enqueue(db, 'import_content', {'platform': 'jm'})
    def forbidden(_): raise AssertionError('Unnecessary global claim lock')
    monkeypatch.setattr(queue_control, 'lock_control', forbidden)
    assert jobs.claim_once(('download',), preflight=True) is None
    assert jobs.claim_once(('ai',), preflight=True) is None


def test_subscription_scheduler_batches_existing_job_lookup(client, monkeypatch):
    with SessionLocal() as db:
        for number in range(20):
            row = Subscription(platform='pixiv', target_type='author', target=str(number + 1), label='synthetic', enabled=True)
            db.add(row)
        db.commit()
    queries = []
    def observe(_conn, _cursor, statement, _params, _context, _many): queries.append(statement)
    monkeypatch.setattr(jobs, 'get_connector', lambda _: SimpleNamespace(supports_subscriptions=True))
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        with SessionLocal() as db: jobs.schedule_subscriptions(db)
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    assert len([query for query in queries if query.lstrip().upper().startswith('SELECT') and 'jobs' in query]) == 1
    with SessionLocal() as db: assert len(db.scalars(select(Job)).all()) == 20


def test_dead_worker_recovery_only_expires_its_exact_attempt_owner(client, tmp_path):
    from datetime import timedelta
    worker = WorkerSupervisor()
    worker.directory = tmp_path
    row = {'process': SimpleNamespace(pid=100)}
    own, replacement = '1' * 36, '2' * 36
    with SessionLocal() as db:
        first = Job(kind='ai', status='running', worker_id=own, lease_until=utcnow()+timedelta(minutes=5))
        second = Job(kind='ai', status='running', worker_id=replacement, lease_until=utcnow()+timedelta(minutes=5))
        db.add_all([first, second]); db.commit(); ids = first.id, second.id
    (tmp_path/'ai.json').write_text(json.dumps({'pid':100,'owners':[own]}))
    worker._recover('ai', row)
    with SessionLocal() as db:
        assert db.get(Job, ids[0]).lease_until < utcnow()
        assert db.get(Job, ids[1]).lease_until > utcnow()
    # PID mismatch is a stale status file, never evidence for recovery.
    assert worker._read('ai', {'process':SimpleNamespace(pid=101)}) is None


def test_db_recovery_does_not_block_runtime_status(client, monkeypatch):
    from threading import Event, Thread
    from concurrent.futures import ThreadPoolExecutor
    entered, release = Event(), Event()
    worker = WorkerSupervisor()
    worker.workers['ai'] = {'process':SimpleNamespace(pid=1,poll=lambda:1), 'started':0, 'recovered':False, 'restarts':0, 'next_start':0}
    def slow(*_): entered.set(); release.wait(5)
    monkeypatch.setattr(worker, '_recover', slow)
    monitor = Thread(target=worker._monitor)
    monitor.start()
    try:
        assert entered.wait(3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            value = pool.submit(worker.status).result(timeout=1)
        assert value['workers'][0]['state'] == 'restarting'
    finally:
        worker.stop_event.set(); release.set(); monitor.join(timeout=2)


def test_ai_runtime_status_comes_from_live_worker_and_invalidates_changed_model(client, tmp_path, monkeypatch):
    from app.ai import service
    from app.worker_runtime import supervisor
    monkeypatch.setattr(settings, 'model_dir', tmp_path)
    folder = tmp_path/'wd-tagger'; folder.mkdir()
    for name in ('model.onnx','selected_tags.csv'): (folder/name).write_text('synthetic')
    key = [path.stat().st_mtime_ns for path in (folder/'model.onnx',folder/'selected_tags.csv')]
    cached = {'available':True,'requested_provider':'auto','actual_provider':'CPUExecutionProvider','execution_state':'ready'}
    monkeypatch.setattr(supervisor, 'worker_data', lambda _: {'model_key':key,'ai_status':cached})
    assert service.model_status() == cached
    monkeypatch.setattr(supervisor, 'worker_data', lambda _: {'model_key':[0,0],'ai_status':cached})
    monkeypatch.setattr(service,'runtime_info',lambda:{'runtime_available':False,'available_providers':[],'runtime_error':'not loaded','runtime_version':None})
    assert service.model_status()['execution_state'] == 'not_loaded'


def test_real_processes_are_separate_restart_and_stop_without_touching_queue_preferences(client, monkeypatch):
    # Explicit isolated SQLite URL from conftest; children never read deploy/.env.
    monkeypatch.setenv('LIBRARY_CPU_AFFINITY', 'off')
    monkeypatch.setenv('LIBRARY_STORAGE_ROOT', str(settings.storage_root))
    monkeypatch.setenv('LIBRARY_IMPORT_ROOT', str(settings.import_root))
    monkeypatch.setenv('LIBRARY_MODEL_DIR', str(settings.model_dir))
    queue_control.update_settings({'paused': True, 'concurrency': 3})
    worker = WorkerSupervisor()
    processes = []
    try:
        worker.start()
        def ready():
            values = worker.status()['workers']
            return len(values) == len(cpu_affinity.ROLES)-1 and all(value['state'] == 'running' for value in values)
        deadline = time.monotonic()+25
        while not ready() and time.monotonic() < deadline: time.sleep(.1)
        assert ready(), worker.status()
        processes = [row['process'] for row in worker.workers.values()]
        assert len({process.pid for process in processes} | {os.getpid()}) == len(cpu_affinity.ROLES)
        original = worker.workers['download']['process']
        original.terminate(); original.wait(timeout=5)
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            current = worker.workers['download']['process']
            if current.pid != original.pid and worker.worker_data('download'): break
            time.sleep(.1)
        assert current.pid != original.pid and current.poll() is None
        processes.append(current)
        assert queue_control.read_settings()['paused'] is True
        assert queue_control.read_settings()['concurrency'] == 3
        assert 'owners' not in json.dumps(worker.status())
    finally:
        worker.stop()
    assert all(process.poll() is not None for process in processes)
