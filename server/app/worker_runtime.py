"""Supervise independent queue processes without sharing SQLAlchemy pools."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
from threading import Event, RLock, Thread, current_thread
import time

from . import cpu_affinity

log = logging.getLogger(__name__)


def enabled():
    return os.getenv("LIBRARY_WORKER_ENABLED", "true").lower() not in ("0", "false", "no")


class WorkerSupervisor:
    def __init__(self):
        self.lock = RLock()
        self.stop_event = Event()
        self.thread = None
        self.original_cpus = None
        self.placement = None
        self.affinity_applied = False
        self.directory = None
        self.workers = {}

    def prepare(self):
        with self.lock:
            if self.placement is not None:
                return
            original = cpu_affinity.available_cpus()
            setting = os.getenv("LIBRARY_CPU_AFFINITY", "auto")
            placement = cpu_affinity.plan(original, setting)
            try:
                applied = cpu_affinity.apply(placement["api"])
            except OSError:
                if setting != "auto":
                    raise
                placement, applied = cpu_affinity.plan(original, "off"), False
                log.warning("CPU affinity unavailable; queue processes remain isolated")
            self.original_cpus, self.placement, self.affinity_applied = original, placement, applied

    def _launch(self, lane, row):
        environment = os.environ.copy()
        environment.update({"LIBRARY_WORKER_PROCESS": lane, "LIBRARY_WORKER_ENABLED": "false",
            "LIBRARY_WORKER_CPUS": json.dumps(self.placement[lane]),
            "LIBRARY_WORKER_PARENT_PID": str(os.getpid()),
            "LIBRARY_WORKER_STATUS_FILE": str(self.directory / (lane + ".json")),
            "LIBRARY_WORKER_STOP_FILE": str(self.directory / "stop"),
            "LIBRARY_MANAGED_PROXY_RUNTIME": "false"})
        # Prevent BLAS/OpenMP pools from multiplying all host CPUs in each child.
        threads = str(max(1, min(4, len(self.placement[lane]) or 1)))
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            environment[key] = threads
        executable = sys.executable
        if os.name == "nt" and getattr(sys, "_base_executable", executable) != executable:
            # Windows venv python.exe is a redirector with a different child PID.
            # Launch the interpreter directly while retaining the venv prefix.
            environment["__PYVENV_LAUNCHER__"] = executable
            executable = sys._base_executable
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        row["process"] = subprocess.Popen([executable, "-m", "app.worker"], env=environment,
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, **options)
        row["started"] = time.monotonic()
        row["recovered"] = False

    def start(self):
        self.prepare()
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.directory = Path(tempfile.mkdtemp(prefix="qiye-workers-"))
            self.stop_event.clear()
            try:
                for lane in cpu_affinity.ROLES[1:]:
                    row = {"process": None, "restarts": 0, "next_start": 0., "started": 0., "recovered": False}
                    self.workers[lane] = row
                    self._launch(lane, row)
            except Exception:
                self.stop()
                raise
            self.thread = Thread(target=self._monitor, name="queue-process-supervisor", daemon=True)
            self.thread.start()

    def _read(self, lane, row):
        process = row.get("process")
        if not self.directory or process is None:
            return None
        try:
            path = self.directory / (lane + ".json")
            if path.stat().st_size > 128 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) and value.get("pid") == process.pid else None
        except (OSError, ValueError, TypeError):
            return None

    def _recover(self, lane, row):
        """Expire only the exact lost attempt owners recorded by this child."""
        snapshot = self._read(lane, row) or {}
        owners = snapshot.get("owners", [])
        if not isinstance(owners, list) or len(owners) > 8 or any(not isinstance(owner, str) or len(owner) != 36 for owner in owners):
            return
        if owners:
            from datetime import timedelta
            from sqlalchemy import update
            from .database import SessionLocal
            from .models import Job, utcnow
            with SessionLocal() as db:
                db.execute(update(Job).where(Job.status == "running", Job.worker_id.in_(owners))
                           .values(lease_until=utcnow() - timedelta(seconds=1)))
                db.commit()

    def _monitor(self):
        while not self.stop_event.wait(1):
            with self.lock:
                rows = list(self.workers.items())
            for lane, row in rows:
                if self.stop_event.is_set():
                    return
                process = row["process"]
                if process is not None and process.poll() is None:
                    continue
                moment = time.monotonic()
                if not row["recovered"]:
                    try:
                        # Never hold the status lock across DB recovery I/O.
                        self._recover(lane, row)
                    except Exception:
                        log.warning("Queue process recovery deferred: %s", lane)
                        continue
                    row["recovered"] = True
                    row["next_start"] = moment + min(60, 2 ** min(row["restarts"], 6))
                if moment < row["next_start"]:
                    continue
                with self.lock:
                    if self.stop_event.is_set() or self.workers.get(lane) is not row:
                        return
                    try:
                        row["restarts"] += 1
                        self._launch(lane, row)
                    except Exception:
                        row["next_start"] = moment + 10
                        log.warning("Queue process restart deferred: %s", lane)

    def worker_data(self, lane):
        with self.lock:
            row = self.workers.get(lane)
            if not row or row["process"] is None or row["process"].poll() is not None:
                return None
            value = self._read(lane, row)
            return value if value and 0 <= time.time() - value.get("heartbeat", 0) < 20 else None

    def status(self):
        with self.lock:
            placement = self.placement or {}
            workers = []
            for lane, row in self.workers.items():
                process = row["process"]
                snapshot = self.worker_data(lane)
                state = "running" if snapshot else "starting" if process is not None and process.poll() is None else "restarting"
                if state == "starting" and time.monotonic() - row["started"] > 20:
                    state = "unresponsive"
                workers.append({"lane": lane, "pid": process.pid if process else None, "state": state,
                    "cpus": snapshot.get("cpus", []) if snapshot else [], "requested_cpus": placement.get(lane, []),
                    "affinity_applied": snapshot.get("affinity_applied", False) if snapshot else False,
                    "active": snapshot.get("active", 0) if snapshot else 0, "restarts": row["restarts"]})
            return {"mode": "process" if self.workers else "disabled", "api_pid": os.getpid(),
                "api_cpus": placement.get("api", []) if self.affinity_applied else [],
                "affinity_applied": self.affinity_applied, "shared_cpus": cpu_affinity.shared(placement),
                "workers": workers}

    def stop(self):
        self.stop_event.set()
        with self.lock:
            if self.directory:
                (self.directory / "stop").touch()
            processes = [row["process"] for row in self.workers.values() if row["process"] is not None]
        # Signal every lane first; one slow request cannot delay the other lanes.
        deadline = time.monotonic() + 6
        for process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    process.terminate()
        deadline = time.monotonic() + 2
        for process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait(timeout=2)
        if self.thread and self.thread is not current_thread():
            self.thread.join(timeout=2)
        with self.lock:
            self.workers.clear()
            if self.original_cpus and self.affinity_applied:
                cpu_affinity.apply(self.original_cpus)
            self.placement = None
            self.affinity_applied = False


supervisor = WorkerSupervisor()


def prepare():
    if os.getenv("LIBRARY_WORKER_MODE", "process") not in ("process", "thread"):
        raise ValueError("LIBRARY_WORKER_MODE must be process or thread")
    if enabled() and os.getenv("LIBRARY_WORKER_MODE", "process") == "process":
        supervisor.prepare()


def status():
    if enabled() and os.getenv("LIBRARY_WORKER_MODE", "process") == "thread":
        return {"mode": "thread", "workers": [], "api_cpus": [], "shared_cpus": True, "affinity_applied": False}
    return supervisor.status()
