import json
import logging
from pathlib import Path

import httpx
import pytest
from sqlalchemy.exc import StatementError

from app import jobs
from app.database import SessionLocal
from app.integrations.base import ConnectorError
from app.models import Job, Subscription
from test_library import client


SECRET = "private-diagnostic-refresh-token"
PRIVATE_URL = "https://private.example/api?token=" + SECRET
SQL = "INSERT INTO secrets(token) VALUES (:private_parameter)"


def pending_job(kind="import", payload=None):
    # These tests simulate an active worker, not a previous TestClient's shutdown.
    jobs._stop.clear()
    with SessionLocal() as db:
        job = jobs.enqueue(db, kind, payload or {"platform": "pixiv", "remote_id": "123", "token": SECRET})
        job.status, job.worker_id = "running", jobs._worker_id
        db.commit()
        return job.id


def diagnostic_records(caplog):
    return [record for record in caplog.records if record.name == jobs.log.name]


@pytest.mark.parametrize("error", [
    TypeError("'NoneType' is not callable " + SECRET),
    httpx.HTTPStatusError("HTTP failure " + PRIVATE_URL,
                         request=httpx.Request("GET", PRIVATE_URL), response=httpx.Response(403)),
    StatementError("database failure " + PRIVATE_URL, SQL, {"private_parameter": SECRET}, RuntimeError(SECRET)),
])
def test_unknown_error_logs_locations_without_exception_data(client, monkeypatch, caplog, error):
    caplog.set_level(logging.ERROR, logger=jobs.log.name)

    def sdk_failure():
        credential_in_local_variable = SECRET
        try:
            raise ValueError("private chained cause " + credential_in_local_variable)
        except ValueError as cause:
            raise error from cause

    monkeypatch.setattr(jobs, "_import", lambda *_args: sdk_failure())
    ident = pending_job()
    jobs.process_job(ident)
    with SessionLocal() as db:
        job = db.get(Job, ident)
        assert job.status == "failed"
        assert "内部处理错误" in job.error and type(error).__name__ in job.error
        assert "检查平台网络" not in job.error
        saved_error = job.error
    records = diagnostic_records(caplog)
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert ident in message and "kind=import" in message and type(error).__name__ in message
    frames = json.loads(message.split("frames=", 1)[1])
    assert frames[-1]["file"] == "test_job_diagnostics.py"
    assert frames[-1]["function"] == "sdk_failure" and frames[-1]["line"] > 0
    assert all(set(frame) == {"file", "function", "line"} for frame in frames)
    assert record.exc_info is None and record.stack_info is None
    for forbidden in (SECRET, PRIVATE_URL, SQL, "private_parameter", "private chained cause", "raise error from cause",
                      str(Path(__file__).resolve().parent), "credential_in_local_variable"):
        assert forbidden not in message and forbidden not in saved_error
        assert forbidden not in client.get("/api/jobs").text


def test_diagnostics_do_not_call_exception_str_or_repr_and_limit_frames(client, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=jobs.log.name)

    class SensitiveError(Exception):
        def __str__(self):
            pytest.fail("Unknown exception strings must never be formatted")
        def __repr__(self):
            pytest.fail("Unknown exception repr must never be formatted")

    def recurse(depth):
        if depth:
            recurse(depth - 1)
        else:
            raise SensitiveError(SECRET)

    monkeypatch.setattr(jobs, "_import", lambda *_args: recurse(30))
    ident = pending_job()
    jobs.process_job(ident)
    message = diagnostic_records(caplog)[0].getMessage()
    frames = json.loads(message.split("frames=", 1)[1])
    assert len(frames) == jobs.DIAGNOSTIC_FRAME_LIMIT
    assert frames[-1]["function"] == "recurse"
    assert SECRET not in message
    with SessionLocal() as db:
        assert "SensitiveError" in db.get(Job, ident).error


def test_subscription_error_uses_same_safe_internal_diagnostic(client, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=jobs.log.name)
    with SessionLocal() as db:
        subscription = Subscription(platform="pixiv", target_type="author", target="123", label="test")
        db.add(subscription)
        db.commit()
        subscription_id = subscription.id
    def failure(*_args):
        raise RuntimeError(SQL + SECRET)
    monkeypatch.setattr(jobs, "_sync", failure)
    ident = pending_job("source_sync", {"subscription_id": subscription_id, "platform": "pixiv", "url": PRIVATE_URL})
    jobs.process_job(ident)
    with SessionLocal() as db:
        message = db.get(Subscription, subscription_id).last_error
        assert message == db.get(Job, ident).error
        assert "RuntimeError" in message and SECRET not in message and SQL not in message
    assert "kind=source_sync" in diagnostic_records(caplog)[0].getMessage()


def test_expected_connector_error_keeps_its_safe_message(client, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=jobs.log.name)
    def failure(*_args):
        raise ConnectorError("平台登录已过期，请重新登录")
    monkeypatch.setattr(jobs, "_import", failure)
    ident = pending_job()
    jobs.process_job(ident)
    with SessionLocal() as db:
        assert db.get(Job, ident).error == "平台登录已过期，请重新登录"
    assert diagnostic_records(caplog) == []


def test_generated_traceback_names_cannot_echo_urls_or_job_payload(caplog):
    caplog.set_level(logging.ERROR, logger=jobs.log.name)
    try:
        exec(compile("raise TypeError('private source code token')", PRIVATE_URL, "exec"))
    except TypeError as error:
        jobs._record_internal_failure(PRIVATE_URL, SECRET, error)
    message = diagnostic_records(caplog)[0].getMessage()
    assert "job_id=unknown kind=unknown" in message
    frames = json.loads(message.split("frames=", 1)[1])
    assert frames[-1]["file"] == "<generated>"
    assert PRIVATE_URL not in message and SECRET not in message and "private source code token" not in message
