import asyncio
from contextlib import ExitStack
import zlib

from fastapi import HTTPException
import pytest
from starlette.requests import ClientDisconnect, Request

from app import device_auth, network_speed
from test_devices import bearer, bind
from test_library import client


@pytest.fixture(autouse=True)
def empty_speed_slots():
    assert not network_speed._active
    yield
    assert not network_speed._active


def request_for(*, device=None, headers=(), receive=None):
    request = Request({"type": "http", "method": "POST", "path": "/api/network/speed/upload",
                       "headers": [(name.encode(), value.encode()) for name, value in headers]}, receive=receive)
    if device is not None:
        request.state.device_id = device
    return request


def test_speed_requires_authentication(client):
    client.cookies.clear()
    assert client.get("/api/network/speed/ping").status_code == 401
    assert client.get("/api/network/speed/download?bytes=1").status_code == 401
    assert client.post("/api/network/speed/upload", content=b"x",
                       headers={"Content-Type": "application/octet-stream"}).status_code == 401


def test_paired_device_can_measure_without_admin_cookie_or_cache(client):
    device = bind(client)
    client.cookies.clear()
    headers = bearer(device)
    ping = client.get("/api/network/speed/ping?nonce=unique", headers=headers)
    assert ping.status_code == 200
    assert ping.json() == {"ok": True, "max_bytes": 32 * 1024 * 1024,
                           "default_bytes": 8 * 1024 * 1024, "timeout_seconds": 30}
    size = 2 * network_speed.CHUNK_BYTES + 11
    first = client.get(f"/api/network/speed/download?bytes={size}&nonce=unique", headers=headers)
    second = client.get(f"/api/network/speed/download?bytes={size}&nonce=another", headers=headers)
    assert first.status_code == second.status_code == 200
    assert len(first.content) == len(second.content) == size
    assert first.content != second.content
    assert len(zlib.compress(first.content)) >= size * .99
    assert first.headers["content-type"] == "application/octet-stream"
    assert first.headers["content-length"] == str(size)
    assert first.headers["content-encoding"] == "identity"
    assert first.headers["x-accel-buffering"] == "no"
    uploaded = client.post("/api/network/speed/upload", content=first.content,
                           headers={**headers, "Content-Type": "application/octet-stream"})
    assert uploaded.status_code == 200
    assert uploaded.json()["received_bytes"] == size
    assert uploaded.json()["elapsed_ms"] >= 0
    for response in (ping, first, second, uploaded):
        assert response.headers["cache-control"] == "no-store, no-transform"
        assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/api/network/speed/ping"), ("GET", "/api/network/speed/download"),
    ("POST", "/api/network/speed/upload"),
])
def test_device_speed_grants_are_exact(method, path):
    assert device_auth.permitted(method, path)
    assert not device_auth.permitted(method, path + "/extra")
    for wrong_method in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"} - {method}:
        assert not device_auth.permitted(wrong_method, path)


@pytest.mark.parametrize("size", ["0", "-1", "33554433", "1.5", "invalid"])
def test_download_size_is_bounded(client, size):
    response = client.get("/api/network/speed/download", params={"bytes": size})
    assert response.status_code == 422


def test_download_defaults_to_eight_mib(client):
    response = client.get("/api/network/speed/download")
    assert response.status_code == 200
    assert len(response.content) == network_speed.DEFAULT_BYTES


@pytest.mark.parametrize(("headers", "status"), [
    ({"Content-Type": "application/json"}, 415),
    ({"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"}, 415),
    ({"Content-Type": "application/octet-stream", "Content-Length": "33554433"}, 413),
    ({"Content-Type": "application/octet-stream", "Content-Length": "-1"}, 400),
    ({"Content-Type": "application/octet-stream", "Content-Length": "bad"}, 400),
    ({"Content-Type": "application/octet-stream", "Content-Length": "2"}, 400),
])
def test_upload_rejects_invalid_headers_and_truncated_body(client, headers, status):
    response = client.post("/api/network/speed/upload", content=b"x", headers=headers)
    assert response.status_code == status


def test_chunked_upload_enforces_actual_body_size(client, monkeypatch):
    monkeypatch.setattr(network_speed, "MAX_BYTES", 100)
    accepted = client.post("/api/network/speed/upload", content=iter([b"a" * 40, b"b" * 60]),
                           headers={"Content-Type": "application/octet-stream"})
    assert accepted.status_code == 200
    assert accepted.json()["received_bytes"] == 100
    rejected = client.post("/api/network/speed/upload", content=iter([b"a" * 40, b"b" * 61]),
                           headers={"Content-Type": "application/octet-stream"})
    assert rejected.status_code == 413


def test_device_and_global_slots_are_bounded_and_reusable():
    requests = [request_for(device=f"device-{number}") for number in range(network_speed.MAX_TRANSFERS + 1)]
    with ExitStack() as stack:
        for request in requests[:-1]:
            stack.enter_context(network_speed._transfer_slot(request))
        for blocked in (requests[0], requests[-1]):
            with pytest.raises(HTTPException) as caught:
                with network_speed._transfer_slot(blocked):
                    pytest.fail("excess transfer admitted")
            assert caught.value.status_code == 429
            assert caught.value.headers == {"Retry-After": "5"}
    with network_speed._transfer_slot(requests[0]):
        assert len(network_speed._active) == 1


def test_busy_response_reaches_authenticated_client(client):
    with network_speed._transfer_slot(request_for()):
        response = client.get("/api/network/speed/download?bytes=1")
        assert response.status_code == 429
        assert response.headers["retry-after"] == "5"
        upload = client.post("/api/network/speed/upload", content=b"x",
                             headers={"Content-Type": "application/octet-stream"})
        assert upload.status_code == 429
        assert client.get("/api/network/speed/ping").status_code == 200


@pytest.mark.parametrize("failure", ["header", "body", "cancel", "timeout"])
def test_download_slot_released_when_sending_fails(monkeypatch, failure):
    async def exercise():
        response = network_speed._SpeedDownload(request_for(device="phone"), network_speed.CHUNK_BYTES * 3)
        chunks = 0

        async def send(message):
            nonlocal chunks
            if message["type"] == "http.response.start":
                if failure == "header":
                    raise OSError("disconnected before headers")
                return
            chunks += 1
            assert len(message.get("body", b"")) <= network_speed.CHUNK_BYTES
            if failure == "body":
                raise OSError("socket closed")
            if failure == "cancel":
                raise asyncio.CancelledError()
            await asyncio.sleep(10)

        async def receive():
            raise AssertionError("ASGI 2.4 does not need disconnect polling")

        error = asyncio.CancelledError if failure == "cancel" else TimeoutError if failure == "timeout" else ClientDisconnect
        with pytest.raises(error):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert chunks <= 1
        assert not network_speed._active

    monkeypatch.setattr(network_speed, "TRANSFER_SECONDS", .01)
    asyncio.run(exercise())


def test_download_slot_released_for_legacy_asgi_disconnect():
    async def exercise():
        response = network_speed._SpeedDownload(request_for(device="phone"), network_speed.MAX_BYTES)
        disconnected = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                disconnected.set()
                await asyncio.sleep(10)

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send), timeout=1)
        assert not network_speed._active

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["disconnect", "timeout", "cancel"])
def test_upload_slot_released_when_reading_fails(monkeypatch, failure):
    async def exercise():
        async def receive():
            if failure == "disconnect":
                return {"type": "http.disconnect"}
            if failure == "cancel":
                raise asyncio.CancelledError()
            await asyncio.sleep(10)

        request = request_for(device="phone", receive=receive,
                              headers=[("content-type", "application/octet-stream")])
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else HTTPException) as caught:
            await network_speed.upload(request)
        if failure != "cancel":
            assert caught.value.status_code == (400 if failure == "disconnect" else 408)
        assert not network_speed._active

    monkeypatch.setattr(network_speed, "TRANSFER_SECONDS", .01)
    asyncio.run(exercise())
