"""Bounded, read-only telemetry. Requests never sample NAS files or run SQL."""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import time
from threading import Event, RLock, Thread

import psutil
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text

from .auth import require_admin_cookie

router = APIRouter(dependencies=[Depends(require_admin_cookie)])


def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def ratio(used, total):
    return round(min(100, max(0, used / total * 100)), 2) if total and used is not None else None


def delta_rate(current, previous, seconds):
    if current is None or previous is None or seconds <= 0 or current < previous:
        return None
    return (current - previous) / seconds


class Cgroup:
    def __init__(self, root=Path('/sys/fs/cgroup'), enabled=None):
        self.root = Path(root)
        self.enabled = enabled if enabled is not None else Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()

    def read(self, name):
        try:
            value = (self.root / name).read_text(encoding='ascii')[:16384].strip()
            return value
        except (OSError, ValueError, UnicodeError):
            return ''

    def number(self, *names):
        for name in names:
            value = self.read(name)
            if value.isdecimal():
                return int(value)
        return None

    def values(self):
        if not self.enabled:
            return {}
        quota = None
        maximum = self.read('cpu.max').split()
        if len(maximum) == 2 and maximum[0].isdecimal() and maximum[1].isdecimal() and int(maximum[1]) > 0:
            quota = int(maximum[0]) / int(maximum[1])
        if quota is None:
            q = self.number('cpu/cpu.cfs_quota_us', 'cpu,cpuacct/cpu.cfs_quota_us')
            p = self.number('cpu/cpu.cfs_period_us', 'cpu,cpuacct/cpu.cfs_period_us')
            if q and p:
                quota = q / p
        cpuset = self.read('cpuset.cpus.effective') or self.read('cpuset/cpuset.cpus')
        try:
            count = 0
            for block in cpuset.split(',') if cpuset else []:
                ends = [int(value) for value in block.split('-')]
                count += ends[-1] - ends[0] + 1
            if count > 0:
                quota = min(quota, count) if quota else count
        except ValueError:
            pass
        def counters(name):
            return {parts[0]: int(parts[1]) for line in self.read(name).splitlines()
                    if len(parts := line.split()) == 2 and parts[1].isdecimal()}
        cpu = counters('cpu.stat')
        usage = cpu.get('usage_usec')
        ns = self.number('cpuacct/cpuacct.usage', 'cpu,cpuacct/cpuacct.usage')
        memory = counters('memory.stat') or counters('memory/memory.stat')
        return {'capacity': quota, 'cpu_seconds': usage / 1e6 if usage is not None else ns / 1e9 if ns is not None else None,
            'memory_current': self.number('memory.current', 'memory/memory.usage_in_bytes'),
            'memory_limit': self.number('memory.max', 'memory/memory.limit_in_bytes'),
            'inactive_file': memory.get('inactive_file', memory.get('total_inactive_file', 0)),
            'oom_kills': counters('memory.events').get('oom_kill'),
            'throttled_seconds': cpu.get('throttled_usec', 0) / 1e6}


class PerformanceMonitor:
    def __init__(self):
        self.lock = RLock()
        self.stop_event = Event()
        self.threads = []
        self.history = deque(maxlen=150)
        self.latest = None
        self.database = None
        self.storage = None
        self.previous = None
        self.cgroup = Cgroup()
        self.process_times = {}

    def start(self):
        if os.getenv('LIBRARY_ENV') == 'test' or self.threads:
            return
        # A blocked old NAS probe must never resume its loop after a restart.
        self.stop_event = Event()
        self.threads = [Thread(target=self._loop, args=(fn, interval, self.stop_event), name='performance-'+name, daemon=True)
            for fn, interval, name in ((self.sample, 2, 'system'), (self.probe_database, 10, 'database'), (self.probe_storage, 30, 'storage'))]
        for thread in self.threads:
            thread.start()

    def _loop(self, fn, interval, stopped):
        while not stopped.is_set():
            try:
                fn()
            except Exception:
                # Never log exception strings, SQL, paths, connection URLs or environments.
                pass
            stopped.wait(interval)

    def stop(self):
        self.stop_event.set()
        deadline = time.monotonic() + .6
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        self.threads = []

    def sample(self):
        from .worker_runtime import status
        runtime = status()
        moment, wall = time.monotonic(), time.time()
        cg = self.cgroup.values()
        host = psutil.virtual_memory()
        cpu = psutil.cpu_times()._asdict()
        total = sum(value for key, value in cpu.items() if key not in ('guest', 'guest_nice'))
        idle = cpu.get('idle', 0) + cpu.get('iowait', 0)
        cpu_capacity = min(float(psutil.cpu_count() or 1), cg.get('capacity') or float(psutil.cpu_count() or 1))
        prior = self.previous
        elapsed = moment - prior['time'] if prior else 0
        host_cpu = ratio((total - prior['total']) - (idle - prior['idle']), total - prior['total']) if prior and total > prior['total'] and idle >= prior['idle'] else None
        try:
            interfaces = psutil.net_io_counters(pernic=True, nowrap=False)
            interfaces = {name: value for name, value in interfaces.items() if name != 'lo' and not name.lower().startswith('loopback')}
        except (OSError, psutil.Error):
            interfaces = {}
        rates = []
        for name, value in interfaces.items():
            old = prior['network'].get(name) if prior else None
            rates.append((delta_rate(value.bytes_recv, old.bytes_recv if old else None, elapsed),
                          delta_rate(value.bytes_sent, old.bytes_sent if old else None, elapsed)))
        def rate(index):
            values = [row[index] for row in rates if row[index] is not None]
            return round(sum(values), 1) if values else None
        process_rows, native_cpu, native_rss = [], 0, 0
        root = psutil.Process()
        children = {p.pid: p for p in root.children(recursive=True)}
        candidates = [(root.pid, 'api', runtime.get('api_cpus', []), 'running', 0)]
        candidates += [(row['pid'], row['lane'], row.get('cpus', []), row['state'], row.get('restarts', 0)) for row in runtime.get('workers', [])]
        next_times = {}
        for pid, lane, cpus, state, restarts in candidates + [(pid, 'other_process', [], 'running', 0) for pid in children if pid not in {row[0] for row in candidates}]:
            row = {'lane': lane, 'pid': pid, 'cpus': cpus, 'state': state, 'restarts': restarts, 'cpu_percent': None, 'rss_bytes': None}
            try:
                proc = root if pid == root.pid else children[pid]
                with proc.oneshot():
                    born = proc.create_time()
                    times = proc.cpu_times()
                    used = times.user + times.system
                    rss = proc.memory_info().rss
                old = self.process_times.get((pid, born))
                percent = delta_rate(used, old, elapsed)
                percent = round(percent * 100, 2) if percent is not None else None
                row.update(cpu_percent=percent, rss_bytes=rss)
                next_times[pid, born] = used
                native_cpu += percent or 0
                native_rss += rss
            except (KeyError, OSError, psutil.Error):
                pass
            process_rows.append(row)
        self.process_times = next_times
        service_cpu = delta_rate(cg.get('cpu_seconds'), prior['service_cpu'] if prior else None, elapsed)
        service_cpu = ratio(service_cpu, cpu_capacity) if service_cpu is not None else None
        if not self.cgroup.enabled:
            service_cpu = min(100, native_cpu / cpu_capacity) if prior else None
        limit = cg.get('memory_limit')
        if not limit or limit > host.total:
            limit = host.total
        current = cg.get('memory_current') if self.cgroup.enabled else native_rss
        working = max(0, current - cg.get('inactive_file', 0)) if current is not None else None
        memory = {'used_bytes': working, 'current_bytes': current, 'limit_bytes': limit,
            'percent': ratio(working, limit), 'basis': 'cgroup_working_set' if self.cgroup.enabled else 'process_rss_sum',
            'oom_kills': cg.get('oom_kills')}
        try:
            load = [round(value, 2) for value in os.getloadavg()]
        except (AttributeError, OSError):
            load = None
        sample = {'at': wall, 'host_cpu': host_cpu, 'service_cpu': service_cpu,
            'host_memory': ratio(host.total - host.available, host.total), 'service_memory': memory['percent'],
            'download_bps': rate(0), 'upload_bps': rate(1)}
        latest = {'sampled_at': stamp(), 'platform': platform.system(), 'container': self.cgroup.enabled,
            'cpu': {'host_percent': host_cpu, 'service_percent': service_cpu, 'logical_cpus': psutil.cpu_count() or 1,
                'service_capacity': cpu_capacity, 'load_average': load, 'throttled_seconds': cg.get('throttled_seconds')},
            'memory': {'host': {'total_bytes': host.total, 'available_bytes': host.available,
                'used_bytes': host.total - host.available, 'percent': sample['host_memory']}, 'service': memory},
            'network': {'download_bps': sample['download_bps'], 'upload_bps': sample['upload_bps'],
                'scope': 'container' if self.cgroup.enabled else 'host'},
            'uptime_seconds': max(0, int(wall - psutil.boot_time())),
            'service_uptime_seconds': max(0, int(wall - root.create_time())), 'processes': process_rows,
            'worker_mode': runtime.get('mode', 'disabled')}
        self.previous = {'time': moment, 'total': total, 'idle': idle, 'network': interfaces, 'service_cpu': cg.get('cpu_seconds')}
        with self.lock:
            self.latest = (moment, latest)
            self.history.append(sample)

    def probe_database(self):
        from .database import SessionLocal, engine
        from .models import Job
        from .queue_control import QueueControl, LANES, COLLECTION_LANES, lane_settings
        started = time.monotonic()
        result = {'status': 'unavailable', 'latency_ms': None, 'queues': [], 'pool': None}
        try:
            with SessionLocal() as db:
                db.execute(text('SELECT 1'))
                result['latency_ms'] = round((time.monotonic() - started) * 1000, 2)
                control = db.get(QueueControl, 1)
                limits = lane_settings(control) if control else {}
                counts = {(lane, state): count for lane, state, count in db.execute(
                    select(Job.queue_lane, Job.status, func.count()).where(Job.queue_lane.in_(list(LANES)), Job.status.in_(('queued', 'running')))
                    .group_by(Job.queue_lane, Job.status))}
                result['queues'] = [{'id': key, 'label': value, 'queued': counts.get((key, 'queued'), 0),
                    'running': counts.get((key, 'running'), 0), **limits.get(key, {}),
                    'effective_paused': bool(limits.get(key, {}).get('paused') or control and control.paused and key in COLLECTION_LANES)}
                    for key, value in LANES.items()]
            pool = engine.pool
            result['pool'] = {'size': pool.size(), 'checked_out': pool.checkedout(), 'overflow': max(0, pool.overflow())} if hasattr(pool, 'checkedout') else None
            result['status'] = 'ok'
        except Exception:
            pass
        with self.lock:
            self.database = (time.monotonic(), result)

    def probe_storage(self):
        from .config import settings
        rows = []
        for ident, label, path in (('system', '系统盘', Path.cwd().anchor), ('library', '书库存储 / NAS', settings.storage_root)):
            row = {'id': ident, 'label': label, 'status': 'unavailable'}
            try:
                disk = psutil.disk_usage(str(path))
                row.update(status='ok', total_bytes=disk.total, used_bytes=disk.used,
                           free_bytes=disk.free, percent=disk.percent)
            except (OSError, ValueError):
                pass
            rows.append(row)
        with self.lock:
            self.storage = (time.monotonic(), rows)

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            latest = deepcopy(self.latest)
            database = deepcopy(self.database)
            storage = deepcopy(self.storage)
            history = deepcopy(list(self.history))
        return {'status': 'ready' if latest and now - latest[0] <= 8 else 'sampling' if not latest else 'stale',
            'refresh_seconds': 2, 'sample_age_seconds': round(max(0, now - latest[0]), 1) if latest else None,
            'current': latest[1] if latest else None, 'history': history,
            'database': {**database[1], 'stale': now - database[0] > 30} if database else None,
            'storage': storage[1] if storage else [], 'storage_stale': not storage or now - storage[0] > 90}


monitor = PerformanceMonitor()


@router.get('/server/performance')
def performance():
    return monitor.snapshot()
