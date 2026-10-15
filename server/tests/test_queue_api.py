"""Public queue controls must validate bounds and never expose job credentials."""
import pytest

from test_library import client
from app import jobs
from app.database import SessionLocal


def test_random_queue_settings_validate_partial_updates_atomically(client):
    original = client.get("/api/queue/settings").json()
    assert original == {"paused": False, "rate_limit_enabled": True, "min_interval_seconds": 2.0, "max_interval_seconds": 5.0, "concurrency": 1}
    changed = client.put("/api/queue/settings", json={"paused": True, "min_interval_seconds": 3.5, "max_interval_seconds": 7})
    assert changed.status_code == 200
    assert changed.json()["paused"] is True
    # A rejected update must not persist its valid sibling field either.
    rejected = client.put("/api/queue/settings", json={"paused": False, "min_interval_seconds": 8})
    assert rejected.status_code in (400, 422)
    assert client.get("/api/queue/settings").json() == changed.json()
    assert client.put("/api/queue/settings", json={"request_interval_seconds": 4}).json() == {
        "paused": True, "rate_limit_enabled": True, "min_interval_seconds": 4.0, "max_interval_seconds": 4.0, "concurrency": 1}


@pytest.mark.parametrize("payload", [
    {"min_interval_seconds": -1}, {"max_interval_seconds": 61},
    {"min_interval_seconds": 5, "max_interval_seconds": 2},
    {"request_interval_seconds": 2, "min_interval_seconds": 1},
    {"unknown_setting": 1}, {"max_interval_seconds": "NaN"},
    {"rate_limit_enabled": "false"}, {"rate_limit_enabled": 0},
    {"concurrency": 0}, {"concurrency": 9}, {"concurrency": 1.5},
    {"concurrency": "2"}, {"concurrency": True},
])
def test_bad_queue_settings_do_not_create_invalid_state(client, payload):
    before = client.get("/api/queue/settings").json()
    assert client.put("/api/queue/settings", json=payload).status_code in (400, 422)
    assert client.get("/api/queue/settings").json() == before


def test_queue_status_and_individual_job_are_authenticated_and_redacted(client):
    with SessionLocal() as db:
        job = jobs.enqueue(db, "import", {"platform": "pixiv", "remote_id": "private-id", "account_id": "private-account",
                                           "refresh_token": "secret-never-public", "download": True})
        identifier = job.id
        job.status = "running"
        db.commit()
        jobs.enqueue(db, "ai", {"item_id": "local-asset"})
    status = client.get("/api/queue/status")
    assert status.status_code == 200
    assert status.json()["counts"] == {"queued": 0, "running": 1, "failed": 0}
    detail = client.get(f"/api/jobs/{identifier}")
    assert detail.status_code == 200 and detail.json()["id"] == identifier
    assert status.json()["active"] == detail.json()
    for response in (status, detail):
        assert "payload" not in response.text
        assert all(secret not in response.text for secret in ("secret-never-public", "private-account", "private-id"))
    assert client.get("/api/jobs/not-found").status_code == 404
    client.post("/api/auth/logout")
    for path in ("/queue/settings", "/queue/status", f"/jobs/{identifier}"):
        assert client.get("/api" + path).status_code == 401
    assert client.put("/api/queue/settings", json={"paused": True}).status_code == 401


def test_pacing_toggle_preserves_interval_and_pause_and_rejected_update_is_atomic(client):
    original = client.put('/api/queue/settings', json={'paused': True, 'min_interval_seconds': 3, 'max_interval_seconds': 7}).json()
    disabled = client.put('/api/queue/settings', json={'rate_limit_enabled': False}).json()
    assert disabled == {**original, 'rate_limit_enabled': False}
    assert client.get('/api/queue/settings').json() == disabled
    assert client.put('/api/queue/settings', json={'rate_limit_enabled': True, 'min_interval_seconds': 10}).status_code == 400
    assert client.get('/api/queue/settings').json() == disabled
    assert client.put('/api/queue/settings', json={'rate_limit_enabled': True}).json() == original


def test_concurrency_setting_is_persistent_partial_and_atomic(client):
    before = client.put('/api/queue/settings', json={'paused': True, 'rate_limit_enabled': False}).json()
    response = client.put('/api/queue/settings', json={'concurrency': 8})
    assert response.status_code == 200
    assert response.json() == {**before, 'concurrency': 8}
    assert client.get('/api/queue/settings').json() == response.json()
    assert client.put('/api/queue/settings', json={'concurrency': 2, 'min_interval_seconds': 10}).status_code == 400
    assert client.get('/api/queue/settings').json() == response.json()
