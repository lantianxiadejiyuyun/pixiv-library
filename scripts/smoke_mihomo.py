#!/usr/bin/env python3
"""Exercise the installed core without a DB or external requests.

Example Docker verification: run this in a standalone container with --network
none, read-only application code, and no production volume mounts.
"""
import argparse
from pathlib import Path
import socket
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.managed_core import ManagedCore, ManagedCoreError


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="qiye-mihomo-smoke-") as temporary:
        core = ManagedCore(args.binary, temporary, port(), port())
        try:
            assert core.start()["running"]
            assert core._request("GET", "/proxies/QIYE")["now"] == "REJECT"
            node = {"name": "q-offline", "type": "ss", "server": "unused.invalid", "port": 443,
                    "cipher": "aes-128-gcm", "password": "offline-smoke-only"}
            core.apply_nodes([node], "q-offline")
            assert core._request("GET", "/proxies/QIYE")["now"] == "q-offline"
            previous = core.config_path.read_bytes()
            try:
                core.apply_nodes([{**node, "cipher": "not-a-cipher"}], "q-offline")
            except ManagedCoreError:
                pass
            else:
                raise AssertionError("Invalid configuration unexpectedly accepted")
            assert core.config_path.read_bytes() == previous
            # An actual child exit is recovered by the app's supervisor.
            child = core._process
            child.kill()
            child.wait(timeout=5)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if core.status()["running"] and core._process.pid != child.pid:
                    break
                time.sleep(0.2)
            assert core.status()["running"] and core.status()["restart_count"] == 1
            assert core._request("GET", "/proxies/QIYE")["now"] == "q-offline"
            core.apply_nodes([], None)
            assert core._request("GET", "/proxies/QIYE")["now"] == "REJECT"
            child = core._process
            version = core.status()["version"]
        finally:
            core.stop()
        assert child.poll() is not None
        with socket.socket() as released:
            released.settimeout(0.5)
            assert released.connect_ex(("127.0.0.1", core.proxy_port)) != 0
        print(f"Mihomo {version}: start / reject / select / rollback / crash recovery / stop passed (no external requests)")


if __name__ == "__main__":
    main()
