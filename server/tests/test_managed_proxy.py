from copy import deepcopy
from datetime import timedelta
import json

import pytest

from test_library import client
from app import managed_proxy as service, network
from app.database import SessionLocal
from app.integrations.credentials import decrypt_credentials
from app.models import NetworkSetting
from app.proxy_subscription import SubscriptionError


class Core:
    proxy_url = "http://127.0.0.1:17890"
    def __init__(self):
        self.nodes = []
        self.selected = None
        self.fail = False
        self.running = True
    def apply_nodes(self, nodes, selected=None):
        if self.fail:
            raise RuntimeError("private core password must not escape")
        self.nodes, self.selected, self.running = deepcopy(nodes), selected, True
        return self.status()
    def status(self):
        return {"installed": True, "running": self.running, "version": "test-core", "error": None}
    def stop(self):
        self.running = False
    def delay(self, name):
        assert any(n["name"] == name for n in self.nodes)
        return 83


@pytest.fixture
def managed(client, monkeypatch):
    fake = Core()
    monkeypatch.setattr(service, "core_manager", lambda: fake)
    definitions = [{"name": "东京 01", "type": "ss", "server": "example.net", "port": 443, "cipher": "aes-128-gcm", "password": "private-node-password"},
                   {"name": "香港 02", "type": "trojan", "server": "example.org", "port": 443, "password": "private-node-password"}]
    monkeypatch.setattr(service, "fetch_subscription", lambda *a, **k: deepcopy(definitions))
    return client, fake, definitions


def add(client, **values):
    return client.post("/api/network/managed/subscriptions", json={"label": "我的订阅", "url": "https://example.net/sub?token=private-url-token", **values})


def test_import_encrypts_entire_subscription_and_does_not_enable_proxy(managed):
    client, core, _ = managed
    result = add(client)
    assert result.status_code == 200, result.text
    public = result.json()
    assert not public["enabled"]
    assert len(public["nodes"]) == 2 and public["nodes"][0]["selected"]
    assert core.selected == public["selected_node_id"]
    assert "private-url-token" not in result.text and "private-node-password" not in result.text
    assert public["subscriptions"][0]["url_host"] == "example.net"
    with SessionLocal() as db:
        value = db.get(NetworkSetting, service.KEY).value_encrypted
        assert "private" not in value
        state = decrypt_credentials(value)
        assert "private-url-token" in state["subscriptions"][0]["url"]
    assert network.httpx_proxy() is None


def test_node_selection_enable_and_no_fallback_when_core_stops(managed):
    client, core, _ = managed
    state = add(client).json()
    selected = state["nodes"][1]["id"]
    assert client.post("/api/network/managed/select", json={"node_id": selected}).json()["selected_node_id"] == selected
    assert client.post("/api/network/managed/enable", json={"enabled": True}).json()["enabled"]
    core.stop()
    assert network.httpx_proxy() == core.proxy_url
    assert network.requests_proxies()["https"] == core.proxy_url
    client.post("/api/network/managed/enable", json={"enabled": False})
    assert network.httpx_proxy() is None


def test_bad_refresh_retains_old_nodes_url_and_selected(managed, monkeypatch):
    client, core, _ = managed
    state = add(client).json()
    ident = state["subscriptions"][0]["id"]
    def reject(*args):
        raise SubscriptionError("订阅格式无效")
    monkeypatch.setattr(service, "fetch_subscription", reject)
    result = client.patch(f"/api/network/managed/subscriptions/{ident}", json={"url": "https://new.example.net/?token=new-private-token"})
    assert result.status_code == 400
    kept = client.get("/api/network/managed").json()
    assert kept["nodes"] == state["nodes"] and kept["selected_node_id"] == state["selected_node_id"]
    assert kept["subscriptions"][0]["last_error"]
    assert kept["subscriptions"][0]["url_host"] == "example.net"
    assert core.selected == state["selected_node_id"]


def test_invalid_core_config_does_not_persist_and_errors_are_redacted(managed):
    client, core, _ = managed
    state = add(client).json()
    core.fail = True
    result = add(client, url="https://second.example.net/sub", label="另一个")
    assert result.status_code == 503 and "private" not in result.text
    kept = client.get("/api/network/managed").json()
    assert kept["nodes"] == state["nodes"] and len(kept["subscriptions"]) == 1


def test_deleting_active_subscription_fails_closed_without_auto_direct(managed):
    client, core, _ = managed
    state = add(client).json()
    client.post("/api/network/managed/enable", json={"enabled": True})
    result = client.delete("/api/network/managed/subscriptions/" + state["subscriptions"][0]["id"])
    assert result.status_code == 200
    assert result.json()["enabled"] and result.json()["selected_node_id"] is None
    assert core.nodes == [] and core.selected is None
    assert network.httpx_proxy() == core.proxy_url


def test_refresh_missing_selected_node_does_not_pick_another_route(managed, monkeypatch):
    client, core, definitions = managed
    state = add(client).json()
    client.post("/api/network/managed/enable", json={"enabled": True})
    monkeypatch.setattr(service, "fetch_subscription", lambda *args: definitions[1:])
    result = client.post("/api/network/managed/subscriptions/" + state["subscriptions"][0]["id"] + "/refresh")
    assert result.json()["enabled"] and result.json()["selected_node_id"] is None
    assert core.selected is None and len(core.nodes) == 1


def test_same_display_name_in_two_subscriptions_has_independent_node_ids(managed):
    client, core, _ = managed
    first = add(client).json()
    second = add(client, url="https://second.example.net/sub", label="第二个订阅").json()
    assert len({node["id"] for node in second["nodes"]}) == 4
    assert second["selected_node_id"] == first["selected_node_id"]
    assert len(core.nodes) == 4


def test_node_ids_include_full_subscription_id_and_survive_refresh():
    definition = {"name": "same-name", "type": "ss"}
    first = service.mapped_nodes("12345678-aaaa", [definition])
    second = service.mapped_nodes("12345678-bbbb", [definition])
    assert first[0]["id"] != second[0]["id"]
    first[0]["delay"] = 42
    assert service.mapped_nodes("12345678-aaaa", [definition], first) == first
    changed = service.mapped_nodes("12345678-aaaa", [{**definition, "port": 444}], first)
    assert changed[0]["id"] == first[0]["id"] and changed[0]["delay"] is None


def test_failed_database_commit_restores_previous_core_configuration(managed, monkeypatch):
    client, core, _ = managed
    add(client)
    with SessionLocal() as db:
        previous = service.read_state(db)
        updated = deepcopy(previous)
        updated["selected_node_id"] = updated["subscriptions"][0]["nodes"][1]["id"]
        def failed_save(*args):
            raise OSError("database unavailable")
        monkeypatch.setattr(service, "save_state", failed_save)
        with pytest.raises(OSError):
            service.apply_and_save(db, previous, updated)
        assert core.selected == previous["selected_node_id"]
        assert service.read_state(db) == previous


def test_failed_database_commit_and_core_rollback_stop_proxy(managed, monkeypatch):
    client, core, _ = managed
    add(client)
    with SessionLocal() as db:
        previous = service.read_state(db)
        updated = deepcopy(previous)
        updated["selected_node_id"] = updated["subscriptions"][0]["nodes"][1]["id"]
        def failed_save(*args):
            core.fail = True
            raise OSError("database unavailable")
        monkeypatch.setattr(service, "save_state", failed_save)
        with pytest.raises(OSError):
            service.apply_and_save(db, previous, updated)
        assert not core.running
        assert service.read_state(db) == previous


def test_auto_refresh_metadata_edit_and_delay(managed, monkeypatch):
    client, _, _ = managed
    state = add(client).json()
    ident = state["subscriptions"][0]["id"]
    response = client.patch(f"/api/network/managed/subscriptions/{ident}", json={"interval_hours": 2, "label": "新名称"})
    assert response.status_code == 200 and response.json()["subscriptions"][0]["interval_hours"] == 2
    delayed = client.post("/api/network/managed/delay", json={"node_id": state["nodes"][0]["id"]})
    assert delayed.json()["delay"] == 83
    with SessionLocal() as db:
        stored = service.read_state(db)
        stored["subscriptions"][0]["next_update_at"] = service.timestamp(service.now() - timedelta(seconds=1))
        service.save_state(db, stored)
    calls = []
    previous = service.fetch_subscription
    monkeypatch.setattr(service, "fetch_subscription", lambda *args: (calls.append(args) or previous(*args)))
    service.refresh_due()
    assert len(calls) == 1
    client.patch(f"/api/network/managed/subscriptions/{ident}", json={"auto_update": False})
    service.refresh_due()
    assert len(calls) == 1


def test_account_session_validation_and_secret_error_redaction(managed):
    client, _, _ = managed
    assert client.post("/api/network/managed/enable", json={"enabled": True}).status_code == 400
    result = add(client, url="private-token-" * 400)
    assert result.status_code == 422 and "private-token" not in result.text
    client.post("/api/auth/logout")
    assert client.get("/api/network/managed").status_code == 401
    assert add(client).status_code == 401
