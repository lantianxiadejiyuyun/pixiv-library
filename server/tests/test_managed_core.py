import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.managed_core import GROUP, ManagedCore, ManagedCoreError, PROJECT_ROOT, _child_environment


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def sample_node(name="q-node"):
    return {"name": name, "type": "ss", "server": "example.invalid", "port": 443,
            "cipher": "aes-128-gcm", "password": "subscription-secret"}


@pytest.fixture
def manager(tmp_path, monkeypatch):
    binary = tmp_path / "core"
    binary.write_bytes(b"test")
    result = ManagedCore(binary, tmp_path / "state", unused_port(), unused_port())
    monkeypatch.setattr(result, "_prepare_directory", lambda: result.state_dir.mkdir(exist_ok=True))
    monkeypatch.setattr(result, "_validate", lambda path: None)
    monkeypatch.setattr(result, "_activate", lambda selected: None)
    monkeypatch.setattr(result, "_ensure_supervisor", lambda: None)
    yield result
    result.stop()


def test_status_does_not_touch_disk_or_reveal_configuration(tmp_path):
    manager = ManagedCore(tmp_path / "absent", tmp_path / "not-created", unused_port(), unused_port())
    status = manager.status()
    assert status["state"] == "missing" and not status["running"]
    assert not manager.state_dir.exists()
    assert manager._secret not in json.dumps(status)
    assert manager.proxy_url.startswith("http://127.0.0.1:")


def test_generated_profile_is_reject_by_default_and_never_inherits_remote_settings(manager):
    original = sample_node()
    manager.apply_nodes([original], "missing-name")
    original["password"] = "modified-after-call"
    data = json.loads(manager.config_path.read_text(encoding="utf-8"))
    assert data["proxies"][0]["password"] == "subscription-secret"
    assert data["proxy-groups"][0]["proxies"] == ["REJECT", "q-node"]
    assert data["proxy-groups"][0]["default-selected"] == "REJECT"
    assert data["rules"] == ["MATCH,QIYE"]
    assert data["tun"] == {"enable": False} and data["allow-lan"] is False
    assert data["bind-address"] == "127.0.0.1"
    assert data["external-controller"].startswith("127.0.0.1:")
    assert not set(data) & {"script", "proxy-providers", "rule-providers", "external-ui", "listeners"}
    assert "subscription-secret" not in json.dumps(manager.status())
    if os.name != "nt":
        assert manager.config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("nodes", [[sample_node("REJECT")], [sample_node(), sample_node()],
    [{**sample_node(), "type": "direct"}], [{**sample_node(), "dialer-proxy": "DIRECT"}],
    [{**sample_node(), "private-key": "/etc/passwd"}], {"proxies": [sample_node()], "tun": {"enable": True}}])
def test_reject_profile_and_unsafe_nodes_before_any_file_write(manager, nodes):
    with pytest.raises(ManagedCoreError):
        manager.apply_nodes(nodes)
    assert not manager.config_path.exists()


def test_validation_failure_keeps_existing_disk_and_memory(manager, monkeypatch):
    manager.apply_nodes([sample_node()], "q-node")
    old = manager.config_path.read_bytes()
    def reject(path):
        raise ManagedCoreError("rejected")
    monkeypatch.setattr(manager, "_validate", reject)
    with pytest.raises(ManagedCoreError):
        manager.apply_nodes([sample_node("different")], "different")
    assert manager.config_path.read_bytes() == old
    assert manager.status()["selected"] == "q-node"
    assert list(manager.state_dir.glob("candidate-*")) == []


def test_controller_reload_failure_rolls_back_disk_and_runtime(manager, monkeypatch):
    manager.apply_nodes([sample_node()], "q-node")
    old = manager.config_path.read_bytes()
    activated = []
    def activate(selected):
        activated.append((selected, json.loads(manager.config_path.read_text(encoding="utf-8"))))
        if selected == "new-node":
            raise ManagedCoreError("controller failed")
    monkeypatch.setattr(manager, "_activate", activate)
    with pytest.raises(ManagedCoreError):
        manager.apply_nodes([sample_node("new-node")], "new-node")
    assert [entry[0] for entry in activated] == ["new-node", "q-node"]
    assert activated[-1][1]["proxies"] == [sample_node()]
    assert manager.config_path.read_bytes() == old
    assert manager.status()["selected"] == "q-node"


def test_empty_config_disables_previous_selected_node(manager):
    manager.apply_nodes([sample_node()], "q-node")
    manager.apply_nodes([], "q-node")
    assert manager.status()["selected"] == "REJECT"
    assert manager.status()["node_count"] == 0
    assert json.loads(manager.config_path.read_text(encoding="utf-8"))["proxy-groups"][0]["proxies"] == ["REJECT"]


def test_rollback_failure_stops_supervisor_from_launching_uncommitted_config(manager, monkeypatch):
    manager.apply_nodes([sample_node()], "q-node")
    def fail(selected):
        raise ManagedCoreError("failed")
    monkeypatch.setattr(manager, "_activate", fail)
    with pytest.raises(ManagedCoreError, match="回滚"):
        manager.apply_nodes([sample_node("new-node")], "new-node")
    assert manager._desired is False
    assert manager._process is None
    assert manager._nodes == [sample_node()]


def test_restart_is_bounded_and_stop_disables_restarts(manager, monkeypatch):
    manager.apply_nodes([sample_node()])
    attempts = []
    def crash():
        attempts.append(1)
        raise ManagedCoreError("failed")
    monkeypatch.setattr(manager, "_launch", crash)
    for _ in range(10):
        manager._next_restart = 0
        manager._restart_if_needed()
    assert len(attempts) == 3
    assert manager.status()["restart_count"] == 3
    assert not manager._desired
    manager.stop()
    manager._restart_if_needed()
    assert len(attempts) == 3


def test_child_never_inherits_application_secrets_or_ambient_proxy(monkeypatch):
    monkeypatch.setenv("LIBRARY_SECRET_KEY", "app-secret")
    monkeypatch.setenv("LIBRARY_DATABASE_URL", "mysql://password")
    monkeypatch.setenv("HTTPS_PROXY", "http://credential@proxy")
    environment = _child_environment()
    assert not set(environment) & {"LIBRARY_SECRET_KEY", "LIBRARY_DATABASE_URL", "HTTPS_PROXY"}


@pytest.mark.parametrize("occupied", ["proxy_port", "controller_port"])
def test_live_listener_prevents_launch_even_with_posix_reuseaddr(manager, monkeypatch, occupied):
    with socket.socket() as busy:
        if os.name != "nt":
            # A real server (including Go's) normally enables this option.
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        setattr(manager, occupied, busy.getsockname()[1])
        def unexpected_launch(*args, **kwargs):
            pytest.fail("Must reject an occupied port before launching a child")
        monkeypatch.setattr(subprocess, "Popen", unexpected_launch)
        with pytest.raises(ManagedCoreError, match="端口"):
            manager._launch()
        assert manager._process is None


def test_delay_quotes_name_and_does_not_accept_user_url(manager, monkeypatch):
    manager.apply_nodes([sample_node("a/b?#节点")])
    monkeypatch.setattr(manager, "_running", lambda: True)
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"delay": 42}
    monkeypatch.setattr(manager, "_request", request)
    assert manager.delay("a/b?#节点") == 42
    assert calls[0][1] == "/proxies/a%2Fb%3F%23%E8%8A%82%E7%82%B9/delay"
    assert calls[0][2]["params"]["url"] == "https://www.gstatic.com/generate_204"
    with pytest.raises(ManagedCoreError):
        manager.delay("DIRECT")


def installer_module():
    specification = importlib.util.spec_from_file_location("install_mihomo_test", PROJECT_ROOT / "scripts/install_mihomo.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_installer_checksum_before_extract_and_atomic_replacement(tmp_path, monkeypatch):
    installer = installer_module()
    good = gzip.compress(b"verified-binary")
    digest = hashlib.sha256(good).hexdigest()
    monkeypatch.setitem(installer.ARTIFACTS, "linux-amd64", ("core.gz", digest))
    archive = tmp_path / "download.gz"
    archive.write_bytes(good)
    output = tmp_path / "core"
    installer.install("linux-amd64", output, archive_path=archive)
    assert output.read_bytes() == b"verified-binary"
    # Bad bytes cannot replace an existing binary even after its manifest is lost.
    output.with_name("core.json").unlink()
    archive.write_bytes(good + b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        installer.install("linux-amd64", output, archive_path=archive)
    assert output.read_bytes() == b"verified-binary"


def test_real_installed_core_loopback_reject_select_reload_and_stop(tmp_path):
    import shutil
    binary = PROJECT_ROOT / ".runtime/mihomo" / ("mihomo.exe" if os.name == "nt" else "mihomo")
    if not binary.is_file() and shutil.which("mihomo"):
        binary = Path(shutil.which("mihomo"))
    if not binary.is_file():
        pytest.skip("Run scripts/install_mihomo.py to include real-binary smoke test")
    received = []
    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_CONNECT(self):
            received.append("CONNECT " + self.path)
            self.send_response(200)
            self.end_headers()
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"proxy")
        def log_message(self, *args):
            pass
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    manager = ManagedCore(binary, tmp_path / "real-core", unused_port(), unused_port())
    try:
        assert manager.start()["running"]
        assert "1.19.32" in manager.status()["version"]
        # No remote network: the .invalid target is either rejected or passed to
        # the test's local upstream HTTP proxy without resolving its domain.
        with httpx.Client(proxy=manager.proxy_url, trust_env=False, timeout=3) as client:
            with pytest.raises(httpx.HTTPError):
                response = client.get("http://managed-core-test.invalid/blocked")
                response.raise_for_status()
        assert received == []
        node = {"name": "q-local", "type": "http", "server": "127.0.0.1", "port": upstream.server_port}
        manager.apply_nodes([node], "q-local")
        with httpx.Client(proxy=manager.proxy_url, trust_env=False, timeout=3) as client:
            assert client.get("http://managed-core-test.invalid/selected").text == "proxy"
            # Stop with an established HTTP connection. Core closes its side
            # first; after client.close() the port can retain TCP TIME_WAIT.
            child = manager._process
            manager.stop()
            assert child.poll() is not None
        assert received == ["CONNECT managed-core-test.invalid:80", "/selected"]
        assert manager.start()["running"]
        assert manager._request("GET", "/proxies/QIYE")["now"] == "q-local"
        with httpx.Client(proxy=manager.proxy_url, trust_env=False, timeout=3) as client:
            assert client.get("http://managed-core-test.invalid/restarted").text == "proxy"
        assert received[-2:] == ["CONNECT managed-core-test.invalid:80", "/restarted"]
        previous = manager.config_path.read_bytes()
        with pytest.raises(ManagedCoreError):
            manager.apply_nodes([{**sample_node(), "cipher": "invalid-cipher"}], "q-node")
        assert manager.config_path.read_bytes() == previous
        assert manager.status()["running"] and manager.status()["selected"] == "q-local"
        manager.apply_nodes([], None)
        assert manager._request("GET", "/proxies/QIYE")["now"] == "REJECT"
        # Controller credentials stay server-side and enforce authentication.
        with httpx.Client(trust_env=False) as client:
            assert client.get(f"http://127.0.0.1:{manager.controller_port}/version").status_code == 401
        child = manager._process
    finally:
        manager.stop()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=3)
    assert child.poll() is not None and not manager.status()["running"]
    # TIME_WAIT may remain after a stopped process; verify there is no listener.
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", manager.proxy_port), timeout=0.5)
