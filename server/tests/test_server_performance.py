import json
from types import SimpleNamespace
from threading import Event, Thread
import time

import pytest

from test_library import client
from app import server_performance as telemetry
from app.database import SessionLocal
from app.models import Job


def test_cgroup_v2_reads_fractional_cpu_and_memory_limits(tmp_path):
    values = {'cpu.max': '150000 100000', 'cpuset.cpus.effective': '0-3,6',
        'cpu.stat': 'usage_usec 6000000\nthrottled_usec 200000\n', 'memory.current': '2048',
        'memory.max': '4096', 'memory.stat': 'anon 1024\ninactive_file 512\n', 'memory.events': 'oom_kill 2\n'}
    for name, value in values.items(): (tmp_path/name).write_text(value)
    value = telemetry.Cgroup(tmp_path, enabled=True).values()
    assert value['capacity'] == 1.5 and value['cpu_seconds'] == 6
    assert value['memory_current'] == 2048 and value['memory_limit'] == 4096
    assert value['inactive_file'] == 512 and value['oom_kills'] == 2
    (tmp_path/'cpu.max').write_text('max 100000')
    assert telemetry.Cgroup(tmp_path, enabled=True).values()['capacity'] == 5
    assert telemetry.Cgroup(tmp_path, enabled=False).values() == {}


def test_cgroup_v1_fallback_and_unavailable_counters(tmp_path):
    for name, value in {'cpu/cpu.cfs_quota_us':'200000','cpu/cpu.cfs_period_us':'100000',
        'cpuacct/cpuacct.usage':'7000000000','memory/memory.usage_in_bytes':'1024',
        'memory/memory.limit_in_bytes':'2048','memory/memory.stat':'total_inactive_file 256'}.items():
        path=tmp_path/name;path.parent.mkdir(exist_ok=True);path.write_text(value)
    value=telemetry.Cgroup(tmp_path,enabled=True).values()
    assert value['capacity']==2 and value['cpu_seconds']==7 and value['inactive_file']==256
    assert telemetry.delta_rate(5,10,2) is None
    assert telemetry.delta_rate(20,10,2)==5
    assert telemetry.delta_rate(20,None,2) is None


def fake_sampler(monkeypatch):
    state={'now':10.,'wall':1000.,'user':100.,'idle':100.,'usage':0.,'recv':100,'sent':100,'proc':0.}
    class Process:
        def __init__(self,pid=1): self.pid=pid
        def children(self,**kw): return [Process(2)]
        def create_time(self): return 900.
        def cpu_times(self): return SimpleNamespace(user=state['proc'],system=0)
        def memory_info(self): return SimpleNamespace(rss=1024)
        def oneshot(self):
            from contextlib import nullcontext
            return nullcontext()
    monkeypatch.setattr(telemetry.time,'monotonic',lambda:state['now'])
    monkeypatch.setattr(telemetry.time,'time',lambda:state['wall'])
    monkeypatch.setattr(telemetry.psutil,'Process',Process)
    monkeypatch.setattr(telemetry.psutil,'cpu_times',lambda:SimpleNamespace(_asdict=lambda:{'user':state['user'],'idle':state['idle'],'guest':999}))
    monkeypatch.setattr(telemetry.psutil,'cpu_count',lambda:10)
    monkeypatch.setattr(telemetry.psutil,'boot_time',lambda:100)
    monkeypatch.setattr(telemetry.psutil,'virtual_memory',lambda:SimpleNamespace(total=8000,available=4000))
    monkeypatch.setattr(telemetry.psutil,'net_io_counters',lambda **kw:{'eth0':SimpleNamespace(bytes_recv=state['recv'],bytes_sent=state['sent']),
        'lo':SimpleNamespace(bytes_recv=999999999,bytes_sent=999999999)})
    from app import worker_runtime
    monkeypatch.setattr(worker_runtime,'status',lambda:{'mode':'process','api_cpus':[0],
        'workers':[{'lane':'download','pid':2,'cpus':[1],'state':'running','restarts':0}]})
    monitor=telemetry.PerformanceMonitor()
    monitor.cgroup=SimpleNamespace(enabled=True,values=lambda:{'capacity':9,'cpu_seconds':state['usage'],
        'memory_current':2000,'memory_limit':5000,'inactive_file':500,'oom_kills':0})
    return monitor,state


def test_sampling_distinguishes_host_capacity_from_pinned_api_and_caches(monkeypatch):
    monitor,state=fake_sampler(monkeypatch)
    monitor.sample()
    first=monitor.snapshot()['current']
    assert first['cpu']['service_percent'] is None and first['network']['download_bps'] is None
    state.update(now=12.,wall=1002.,user=160.,idle=140.,usage=9.,recv=1100,sent=300,proc=1.)
    monitor.sample()
    data=monitor.snapshot();current=data['current']
    assert current['cpu']['host_percent']==60 and current['cpu']['service_percent']==50
    assert current['cpu']['service_capacity']==9 and current['cpu']['logical_cpus']==10
    assert current['memory']['service']['used_bytes']==1500 and current['memory']['service']['percent']==30
    assert current['network']['download_bps']==500 and current['network']['upload_bps']==100
    assert current['processes'][0]['cpu_percent']==50
    data['history'][0]['host_cpu']=900
    assert monitor.snapshot()['history'][0]['host_cpu'] is None
    state['now']=25
    assert monitor.snapshot()['status']=='stale'


def test_counter_reset_is_unknown_and_history_is_bounded(monkeypatch):
    monitor,state=fake_sampler(monkeypatch)
    for i in range(165):
        state.update(now=10.+i*2,user=100.+i,idle=100.+i,usage=float(i),recv=100+i,sent=100+i)
        monitor.sample()
    assert len(monitor.snapshot()['history'])==150
    state.update(now=400.,user=1.,idle=1.,usage=0.,recv=1,sent=1)
    monitor.sample()
    data=monitor.snapshot()['current']
    assert data['cpu']['host_percent'] is None and data['cpu']['service_percent'] is None
    assert data['network']['download_bps'] is None


def test_dashboard_does_not_block_on_nas_or_query_inline(client,monkeypatch):
    monitor=telemetry.PerformanceMonitor()
    monitor.latest=(time.monotonic(),{'sampled_at':'synthetic','processes':[]})
    monkeypatch.setattr(telemetry,'monitor',monitor)
    entered,release=Event(),Event()
    def blocked(_):
        entered.set();release.wait(3)
        raise OSError('private NAS path must not be returned')
    monkeypatch.setattr(telemetry.psutil,'disk_usage',blocked)
    thread=Thread(target=monitor.probe_storage,daemon=True);thread.start()
    try:
        assert entered.wait(1)
        started=time.perf_counter()
        result=client.get('/api/server/performance')
        assert result.status_code==200 and time.perf_counter()-started<.5
        assert result.json()['storage_stale'] is True
        assert 'private NAS' not in result.text
    finally:
        release.set();thread.join(1)
    assert all(row['status']=='unavailable' for row in monitor.snapshot()['storage'])


def test_database_queues_exclude_history_and_monitor_requires_admin(client,monkeypatch):
    monitor=telemetry.PerformanceMonitor()
    monkeypatch.setattr(telemetry,'monitor',monitor)
    with SessionLocal() as db:
        db.add_all([Job(kind='ai',status=status,payload={}) for status in ('queued','running','completed','failed')]);db.commit()
    monitor.probe_database()
    database=monitor.snapshot()['database']
    assert database['status']=='ok'
    row=next(row for row in database['queues'] if row['id']=='ai')
    assert row['queued']==1 and row['running']==1
    assert 'completed' not in row and 'failed' not in row
    from test_devices import bind,bearer
    headers=bearer(bind(client))
    assert client.get('/api/server/performance',headers=headers).status_code==403
    client.cookies.clear()
    assert client.get('/api/server/performance').status_code==401


def test_probe_errors_do_not_expose_database_credentials(client,monkeypatch):
    from app import database
    class BadSession:
        def __enter__(self):raise RuntimeError('mysql://secret:password@private-host')
        def __exit__(self,*args):pass
    monkeypatch.setattr(database,'SessionLocal',BadSession)
    monitor=telemetry.PerformanceMonitor();monitor.probe_database()
    value=json.dumps(monitor.snapshot())
    assert 'password' not in value and 'private-host' not in value
    assert monitor.snapshot()['database']['status']=='unavailable'
