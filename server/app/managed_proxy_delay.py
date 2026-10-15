"""Small, bounded background batches for the single managed Mihomo process.

These short-lived diagnostics are deliberately not persisted as queue jobs.
Only current node delays are saved; a service restart discards batch progress.
"""
from collections import OrderedDict, deque
from copy import deepcopy
from threading import Event, RLock, Thread
import time
import uuid

from . import managed_proxy as service
from .proxy_subscription import SubscriptionError

CONCURRENCY = 6
HISTORY_LIMIT = 5
HISTORY_SECONDS = 15 * 60


class DelayBatches:
    def __init__(self):
        self._lock = RLock()
        self._history = OrderedDict()
        self._threads = []
        self._stopping = False

    def _prune(self):
        moment = time.monotonic()
        for ident, batch in list(self._history.items()):
            if batch["public"]["status"] != "running" and moment - batch["finished"] > HISTORY_SECONDS:
                del self._history[ident]
        while len(self._history) > HISTORY_LIMIT:
            removable = next((ident for ident, batch in self._history.items()
                              if batch["public"]["status"] != "running"), None)
            if removable is None:
                break
            del self._history[removable]
        self._threads = [thread for thread in self._threads if thread.is_alive()]

    def _running(self):
        return next((batch for batch in self._history.values() if batch["public"]["status"] == "running"), None)

    def start(self, db, node_ids=None):
        with self._lock:
            if self._stopping:
                raise SubscriptionError("服务正在停止，请稍后重试")
            if active := self._running():
                return deepcopy(active["public"])
        signatures = service.delay_targets(db, node_ids)
        with self._lock:
            # Concurrent clicks share one batch instead of multiplying probes.
            if self._stopping:
                raise SubscriptionError("服务正在停止，请稍后重试")
            if active := self._running():
                return deepcopy(active["public"])
            self._prune()
            ident = uuid.uuid4().hex
            public = {"batch_id": ident, "status": "running", "total": len(signatures), "completed": 0,
                      "succeeded": 0, "failed": 0, "started_at": service.timestamp(), "finished_at": None,
                      "message": "正在后台测速，不会自动切换当前节点；服务重启后进度会清除",
                      "results": [{"node_id": node_id, "status": "pending", "delay": None,
                                   "ok": None, "message": None} for node_id in signatures]}
            batch = {"public": public, "signatures": signatures, "pending": deque(range(len(signatures))),
                     "cancelled": Event(), "finished": None}
            self._history[ident] = batch
            self._prune()
            for _ in range(min(CONCURRENCY, len(signatures))):
                thread = Thread(target=self._worker, args=(batch,), name="managed-proxy-delay", daemon=True)
                self._threads.append(thread)
                thread.start()
            return deepcopy(public)

    def get(self, batch_id=None):
        with self._lock:
            self._prune()
            batch = self._history.get(batch_id) if batch_id else next(reversed(self._history.values()), None)
            return deepcopy(batch["public"]) if batch else None

    def _worker(self, batch):
        while True:
            with self._lock:
                public = batch["public"]
                if batch["cancelled"].is_set() or not batch["pending"]:
                    return
                index = batch["pending"].popleft()
                row = public["results"][index]
                row["status"] = "running"
                node_id = row["node_id"]
            result = service.probe_delay(node_id, batch["cancelled"])
            with self._lock:
                if batch["cancelled"].is_set():
                    return
                public["results"][index] = result
                public["completed"] += 1
                public["succeeded" if result["ok"] else "failed"] += 1
                finished = public["completed"] == public["total"]
            if finished:
                self._finish(batch)
                return

    def _finish(self, batch):
        message = "全部节点测速完成；未切换当前节点。延迟仅反映固定测试网址的连接情况"
        try:
            results = service.persist_delay_results(batch["public"]["results"], batch["signatures"], batch["cancelled"])
        except Exception:
            results = batch["public"]["results"]
            message = "测速已完成，但结果未能保存；可查看本次结果后重试"
        with self._lock:
            if batch["cancelled"].is_set():
                return
            public = batch["public"]
            batch["finished"] = time.monotonic()
            public.update(status="completed", results=results, message=message, finished_at=service.timestamp(),
                          succeeded=sum(row["ok"] is True for row in results),
                          failed=sum(row["ok"] is not True for row in results))

    def resume(self):
        with self._lock:
            self._stopping = False

    def stop(self):
        with self._lock:
            self._stopping = True
            for batch in self._history.values():
                public = batch["public"]
                if public["status"] != "running":
                    continue
                batch["cancelled"].set()
                batch["finished"] = time.monotonic()
                batch["pending"].clear()
                for row in public["results"]:
                    if row["status"] in {"pending", "running"}:
                        row.update(status="skipped", delay=None, ok=False, message="服务停止，测速已取消")
                public.update(status="cancelled", completed=public["total"], finished_at=service.timestamp(),
                              succeeded=sum(row["ok"] is True for row in public["results"]),
                              failed=sum(row["ok"] is not True for row in public["results"]),
                              message="服务停止，未完成的测速已取消")
            threads = list(self._threads)
        # Waiting for capacity observes cancellation every 0.1s. Active probes
        # have a 5-second probe / 8-second client timeout. Joining is bounded
        # globally, and workers never launch or restart the core.
        deadline = time.monotonic() + 9
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))


batches = DelayBatches()
