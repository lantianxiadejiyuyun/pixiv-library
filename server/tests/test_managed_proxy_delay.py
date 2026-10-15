from copy import deepcopy
from threading import Event, Lock, Thread
import time

import pytest

from app import managed_proxy as service, managed_proxy_delay as batch_module
from app.database import SessionLocal
from app.managed_core import ManagedCore, ManagedCoreBusy
from test_library import client
from test_managed_proxy import managed, add


@pytest.fixture
def batching(managed, monkeypatch):
    client, core, definitions = managed
    manager = batch_module.DelayBatches()
    monkeypatch.setattr(batch_module, "batches", manager)
    original = core.delay
    monkeypatch.setattr(core, "delay", lambda name, **_kwargs: original(name))
    yield client, core, definitions, manager
    manager.stop()


def finished(client, ident, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/network/managed/delay-batch/" + ident)
        assert response.status_code == 200, response.text
        if response.json()["status"] != "running":
            return response.json()
        time.sleep(0.01)
    pytest.fail("Background delay batch did not finish")


def many_nodes(client, definitions, monkeypatch, count):
    nodes = [{**definitions[0], "name": f"test-node-{index}"} for index in range(count)]
    monkeypatch.setattr(service, "fetch_subscription", lambda *_args: deepcopy(nodes))
    return add(client).json()


def test_batch_54_nodes_is_immediate_bounded_and_never_changes_selection(batching, monkeypatch):
    client, core, definitions, _manager = batching
    state = many_nodes(client, definitions, monkeypatch, 54)
    entered, release, lock = Event(), Event(), Lock()
    active = peak = calls = 0

    def delay(_name, **_kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 6:
                entered.set()
        try:
            assert release.wait(3)
            return 42
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(core, "delay", delay)
    saves = []
    original_save = service.save_state
    monkeypatch.setattr(service, "save_state", lambda *args: (saves.append(1), original_save(*args))[-1])
    try:
        response = client.post("/api/network/managed/delay-batch", json={})
        assert response.status_code == 202
        ident = response.json()["batch_id"]
        assert entered.wait(2)
        # Both API calls complete while all six actual probes are blocked.
        running = client.get("/api/network/managed/delay-batch").json()
        assert running["completed"] == 0 and running["total"] == 54
        assert client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"] == ident
        assert client.get("/api/network/managed").json()["selected_node_id"] == state["selected_node_id"]
    finally:
        release.set()
    result = finished(client, ident)
    assert result["succeeded"] == result["completed"] == result["total"] == 54
    assert result["failed"] == 0 and peak == 6 and calls == 54
    assert len(saves) == 1  # Encrypt/save one merged state, not 54 full profiles.
    saved = client.get("/api/network/managed").json()
    assert {node["delay"] for node in saved["nodes"]} == {42}
    assert saved["selected_node_id"] == state["selected_node_id"] == core.selected
    assert saved["enabled"] == state["enabled"]


def test_failed_probe_is_local_redacted_and_clears_old_latency(batching, monkeypatch):
    client, core, _, _manager = batching
    state = add(client).json()
    first = state["nodes"][0]["id"]
    assert client.post("/api/network/managed/delay", json={"node_id": first}).json()["delay"] == 83

    def delay(name, **_kwargs):
        if name == first:
            raise TimeoutError("private-node-password https://example.net/?private-url-token")
        return 21

    monkeypatch.setattr(core, "delay", delay)
    ident = client.post("/api/network/managed/delay-batch").json()["batch_id"]
    result = finished(client, ident)
    assert result["failed"] == result["succeeded"] == 1
    assert "private" not in str(result)
    assert result["results"][0]["status"] == "failed"
    assert client.get("/api/network/managed").json()["nodes"][0]["delay"] is None


def test_inventory_updates_and_new_selection_are_not_reverted(batching, monkeypatch):
    client, core, definitions, _manager = batching
    state = add(client).json()
    entered, release = Event(), Event()

    def delay(_name, **_kwargs):
        entered.set()
        assert release.wait(3)
        return 30

    monkeypatch.setattr(core, "delay", delay)
    try:
        ident = client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"]
        assert entered.wait(2)
        other = state["nodes"][1]["id"]
        client.post("/api/network/managed/select", json={"node_id": other})
        client.post("/api/network/managed/enable", json={"enabled": True})
        # Same stable ID but new actual configuration; old measurement is stale.
        updated = [{**definitions[0], "port": 444}, definitions[1]]
        monkeypatch.setattr(service, "fetch_subscription", lambda *_args: updated)
        response = client.post("/api/network/managed/subscriptions/" + state["subscriptions"][0]["id"] + "/refresh")
        assert response.status_code == 200
    finally:
        release.set()
    result = finished(client, ident)
    assert [row["status"] for row in result["results"]] == ["skipped", "success"]
    saved = client.get("/api/network/managed").json()
    assert saved["enabled"] and saved["selected_node_id"] == other == core.selected
    assert [node["delay"] for node in saved["nodes"]] == [None, 30]


def test_deleted_subscription_cannot_be_resurrected_by_delays(batching, monkeypatch):
    client, core, _, _manager = batching
    state = add(client).json()
    entered, release = Event(), Event()

    def delay(_name, **_kwargs):
        entered.set()
        assert release.wait(3)
        return 30

    monkeypatch.setattr(core, "delay", delay)
    try:
        ident = client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"]
        assert entered.wait(2)
        assert client.delete("/api/network/managed/subscriptions/" + state["subscriptions"][0]["id"]).status_code == 200
    finally:
        release.set()
    result = finished(client, ident)
    assert all(row["status"] == "skipped" for row in result["results"])
    assert client.get("/api/network/managed").json()["nodes"] == []


def test_newer_single_probe_is_not_overwritten_by_old_batch(batching, monkeypatch):
    client, core, _, _manager = batching
    state = add(client).json()
    first, second = [node["id"] for node in state["nodes"]]
    old_finished, release = Event(), Event()

    def delay(name, **kwargs):
        if name == first and kwargs.get("wait_for_slot"):
            old_finished.set()
            return 10
        if name == second:
            assert release.wait(3)
            return 20
        return 99

    monkeypatch.setattr(core, "delay", delay)
    try:
        ident = client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"]
        assert old_finished.wait(2)
        assert client.post("/api/network/managed/delay", json={"node_id": first}).json()["delay"] == 99
    finally:
        release.set()
    result = finished(client, ident)
    assert result["results"][0]["status"] == "skipped"
    assert "较新" in result["results"][0]["message"]
    assert client.get("/api/network/managed").json()["nodes"][0]["delay"] == 99


def test_busy_probe_preserves_saved_latency(batching, monkeypatch):
    client, core, _, _manager = batching
    state = add(client).json()
    ident = state["nodes"][0]["id"]
    client.post("/api/network/managed/delay", json={"node_id": ident})
    def busy(*_args, **_kwargs):
        raise ManagedCoreBusy("capacity full")
    monkeypatch.setattr(core, "delay", busy)
    result = client.post("/api/network/managed/delay", json={"node_id": ident}).json()
    assert result["status"] == "skipped"
    assert client.get("/api/network/managed").json()["nodes"][0]["delay"] == 83


def test_save_failure_keeps_results_but_reports_failure_safely(batching, monkeypatch):
    client, _, _, _manager = batching
    add(client)
    def failed_save(*_args):
        raise RuntimeError("private-database-password")
    monkeypatch.setattr(service, "save_state", failed_save)
    ident = client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"]
    result = finished(client, ident)
    assert result["succeeded"] == 2 and "未能保存" in result["message"]
    assert "private" not in str(result)
    assert all(node["delay"] is None for node in client.get("/api/network/managed").json()["nodes"])


def test_stop_cancels_pending_probes_without_saving_or_changing_core(batching, monkeypatch):
    client, core, definitions, manager = batching
    state = many_nodes(client, definitions, monkeypatch, 54)
    entered, release = Event(), Event()
    calls = []
    def delay(name, **_kwargs):
        calls.append(name)
        entered.set()
        assert release.wait(3)
        return 42
    monkeypatch.setattr(core, "delay", delay)
    ident = client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"]
    assert entered.wait(2)
    stopping = Thread(target=manager.stop)
    stopping.start()
    try:
        result = finished(client, ident)
        assert result["status"] == "cancelled"
        assert client.post("/api/network/managed/delay-batch", json={}).status_code == 400
    finally:
        release.set()
        stopping.join(3)
    assert not stopping.is_alive() and len(calls) <= 6
    assert all(node["delay"] is None for node in client.get("/api/network/managed").json()["nodes"])
    assert core.selected == state["selected_node_id"] and core.running


def test_history_expiry_starts_after_completion_and_is_bounded(batching):
    client, _, _, manager = batching
    add(client)
    ids = []
    for _ in range(6):
        ident = client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"]
        finished(client, ident)
        ids.append(ident)
    assert len(manager._history) == batch_module.HISTORY_LIMIT
    assert client.get("/api/network/managed/delay-batch/" + ids[0]).status_code == 404
    batch = manager._history[ids[-1]]
    # A >15-minute run remains available for 15 minutes AFTER it finishes.
    batch["public"]["started_at"] = "2020-01-01T00:00:00Z"
    assert manager.get(ids[-1]) is not None
    copy = manager.get(ids[-1])
    copy["results"].clear()
    assert len(manager.get(ids[-1])["results"]) == 2
    batch["finished"] -= batch_module.HISTORY_SECONDS + 1
    assert manager.get(ids[-1]) is None


def test_validation_login_and_fixed_target_protection(batching):
    client, core, _, _manager = batching
    assert client.get("/api/network/managed/delay-batch").json() is None
    assert client.post("/api/network/managed/delay-batch", json={}).status_code == 400
    add(client)
    assert client.post("/api/network/managed/delay-batch", json={"node_ids": ["missing"]}).status_code == 400
    assert client.post("/api/network/managed/delay-batch", json={"node_ids": []}).status_code == 422
    assert client.post("/api/network/managed/delay-batch", json={"node_ids": ["x"] * 2001}).status_code == 422
    result = client.post("/api/network/managed/delay-batch", json={"url": "https://private-token.example/"})
    assert result.status_code == 422 and "private-token" not in result.text
    core.stop()
    assert client.post("/api/network/managed/delay-batch", json={}).status_code == 400
    client.post("/api/auth/logout")
    assert client.post("/api/network/managed/delay-batch", json={}).status_code == 401
    assert client.get("/api/network/managed/delay-batch").status_code == 401
    assert client.get("/api/network/managed/delay-batch/missing").status_code == 401


def test_existing_single_probe_does_not_consume_batch_as_false_failures(batching, monkeypatch, tmp_path):
    client, _, definitions, _manager = batching
    state = many_nodes(client, definitions, monkeypatch, 54)
    real = ManagedCore(tmp_path / "missing", tmp_path / "core-state", 17894, 19094)
    with SessionLocal() as db:
        real._nodes = service.core_nodes(service.read_state(db))
    monkeypatch.setattr(real, "_running", lambda: True)
    monkeypatch.setattr(service, "core_manager", lambda: real)
    release, six_started, lock = Event(), Event(), Lock()
    active = peak = calls = 0
    def request(method, path, **kwargs):
        nonlocal active, peak, calls
        assert method == "GET" and path.endswith("/delay")
        assert kwargs["timeout"] == 8 and kwargs["params"]["timeout"] == 5000
        assert kwargs["params"]["url"] == "https://www.gstatic.com/generate_204"
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 6:
                six_started.set()
        try:
            assert release.wait(3)
            return {"delay": 12}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(real, "_request", request)
    single = Thread(target=real.delay, args=(state["nodes"][0]["id"],))
    single.start()
    try:
        ident = client.post("/api/network/managed/delay-batch", json={}).json()["batch_id"]
        assert six_started.wait(2)
        # The seventh worker must WAIT for capacity, not fail the other 49 nodes.
        running = client.get("/api/network/managed/delay-batch/" + ident).json()
        assert running["completed"] == running["failed"] == 0
        assert calls == 6
        assert real.status()["running"]  # Configuration/status lock stays free.
        with pytest.raises(ManagedCoreBusy):
            real.delay(state["nodes"][0]["id"])
    finally:
        release.set()
        single.join(3)
    result = finished(client, ident)
    assert result["succeeded"] == 54 and result["failed"] == 0
    assert peak == 6 and calls == 55
