"""One lane per process, one dispatcher per lane, lazy bounded task threads."""
import json
import logging
import os
from pathlib import Path
import signal
from threading import Thread
import time
from concurrent.futures import Future, ThreadPoolExecutor


def main():
    # Apply affinity before importing DB drivers, NumPy, ORT or creating threads.
    from . import cpu_affinity
    lane = os.environ["LIBRARY_WORKER_PROCESS"]
    if lane not in cpu_affinity.ROLES[1:]:
        raise ValueError("Invalid queue process lane")
    requested = json.loads(os.environ["LIBRARY_WORKER_CPUS"])
    applied = cpu_affinity.apply(requested)
    actual = cpu_affinity.available_cpus()
    from . import jobs, queue_control
    from .database import engine
    jobs._stop.clear()
    parent = int(os.environ["LIBRARY_WORKER_PARENT_PID"])
    destination = Path(os.environ["LIBRARY_WORKER_STATUS_FILE"])
    stop_file = Path(os.environ["LIBRARY_WORKER_STOP_FILE"])
    active = {}
    log = logging.getLogger(__name__)
    def stop(*_):
        jobs._stop.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def watch_parent():
        while not jobs._stop.wait(1):
            if os.getppid() != parent:
                # Lost supervisor: no unmanaged downloader is allowed to linger.
                if os.name == "posix" and os.getpgrp() == os.getpid():
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                os._exit(0)
            if stop_file.exists():
                jobs._stop.set()
    Thread(target=watch_parent, name="queue-parent-watch", daemon=True).start()
    def publish():
        value = {"pid": os.getpid(), "heartbeat": time.time(), "cpus": actual,
                 "affinity_applied": applied, "active": len(active), "owners": list(active.values())}
        if lane == "ai":
            from .ai.service import model_status
            from .config import settings
            directory = settings.model_dir / "wd-tagger"
            value["ai_status"] = model_status()
            value["model_key"] = [path.stat().st_mtime_ns if path.is_file() else None for path in (directory / "model.onnx", directory / "selected_tags.csv")]
        temporary = destination.with_suffix(".partial")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(destination)
    idle = 0
    try:
        with ThreadPoolExecutor(max_workers=queue_control.MAX_CONCURRENCY, thread_name_prefix=lane) as executor:
            while not jobs._stop.is_set():
                for future in list(active):
                    if future.done():
                        try:
                            future.result()
                        except Exception:
                            log.warning("Queue attempt interrupted: %s", lane)
                        del active[future]
                publish()
                claimed = False
                if lane == "details":
                    from .novel_covers import scheduler
                    scheduler.tick()
                while len(active) < queue_control.MAX_CONCURRENCY and not jobs._stop.is_set():
                    try:
                        claim = jobs.claim_once((lane,), preflight=True)
                    except Exception:
                        log.warning("Queue dispatcher temporarily unavailable: %s", lane)
                        break
                    if not claim:
                        break
                    ident, owner = claim
                    # Publish ownership before execution for crash recovery.
                    pending = Future()
                    active[pending] = owner
                    publish()
                    del active[pending]
                    active[executor.submit(jobs.execute_claim, ident, owner)] = owner
                    claimed = True
                idle = 0 if claimed else min(4, idle + 1)
                jobs._stop.wait(.2 if claimed else (1 if active else min(5, 1 + idle)))
    finally:
        jobs._stop.set()
        publish()
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        main()
    except Exception:
        # Driver errors may contain credentials; the supervisor reports failure.
        logging.getLogger(__name__).error("Queue process stopped unexpectedly")
        raise SystemExit(1) from None
