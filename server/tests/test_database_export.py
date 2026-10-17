import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pymysql.cursors import SSCursor

from test_library import client
from app import database_export as export


@pytest.fixture
def mysql_mode(monkeypatch):
    # Never construct a real MySQL connection: conftest additionally rejects one.
    value = SimpleNamespace(dialect=SimpleNamespace(name="mysql", driver="pymysql"), url="mysql+pymysql://synthetic")
    monkeypatch.setattr(export, "engine", value)
    return value


def test_export_auth_origin_and_sqlite_support(client):
    assert client.post("/api/database/export").status_code == 501
    assert client.post("/api/database/export", headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/database/export").status_code == 401


def test_success_download_headers_and_cleanup(client, mysql_mode, monkeypatch):
    paths = []
    def dump(stream):
        paths.append(Path(stream.name) if isinstance(stream.name, str) else None)
        stream.write("-- synthetic database\n")
    monkeypatch.setattr(export, "write_mysql_dump", dump)
    original = export.tempfile.mkstemp
    captured = []
    def temporary(*args, **kwargs):
        fd, path = original(*args, **kwargs)
        captured.append(Path(path))
        return fd, path
    monkeypatch.setattr(export.tempfile, "mkstemp", temporary)
    response = client.post("/api/database/export")
    assert response.status_code == 200
    assert response.content == b"-- synthetic database\n"
    assert response.headers["content-type"] == "application/sql; charset=utf-8"
    assert response.headers["content-disposition"].startswith('attachment; filename="qiye-database-')
    assert response.headers["cache-control"] == "no-store"
    assert len(captured) == 1 and not captured[0].exists()
    assert not export._export_lock.locked()


def test_single_export_lock_until_download_cleanup(mysql_mode, monkeypatch):
    monkeypatch.setattr(export, "write_mysql_dump", lambda stream: stream.write("synthetic"))
    response = export.export_database()
    path = Path(response.path)
    try:
        assert path.exists()
        assert export._export_lock.locked()
        with pytest.raises(HTTPException) as error:
            export.export_database()
        assert error.value.status_code == 409
    finally:
        response.cleanup()
    response.cleanup()  # Idempotent if finally is reached a second time.
    assert not path.exists() and not export._export_lock.locked()


def test_failed_generation_is_removed_and_error_is_sanitized(client, mysql_mode, monkeypatch, caplog):
    created = []
    original = export.tempfile.mkstemp
    def temporary(*args, **kwargs):
        fd, path = original(*args, **kwargs)
        created.append(Path(path))
        return fd, path
    monkeypatch.setattr(export.tempfile, "mkstemp", temporary)
    def fail(stream):
        stream.write("partial synthetic secret")
        raise ValueError("mysql://private-user:secret-token@database/private INSERT raw data")
    monkeypatch.setattr(export, "write_mysql_dump", fail)
    response = client.post("/api/database/export")
    assert response.status_code == 503
    assert "secret-token" not in response.text + caplog.text
    assert "raw data" not in response.text + caplog.text
    assert "ValueError" in caplog.text
    assert len(created) == 1 and not created[0].exists()
    assert not export._export_lock.locked()


@pytest.mark.parametrize("failure", [OSError("synthetic disconnect"), asyncio.CancelledError()])
def test_failed_send_and_cancellation_always_cleanup(tmp_path, failure):
    path = tmp_path / "private.sql"
    path.write_text("-- data", encoding="utf-8")
    released = []
    response = export.ExportFileResponse(str(path), filename="backup.sql", release=lambda: released.append(True))
    scope = {"type": "http", "method": "POST", "headers": [], "asgi": {"spec_version": "2.4"},
             "extensions": {"http.response.pathsend": {}}}
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        assert message["type"] != "http.response.pathsend"
        raise failure
    with pytest.raises(type(failure)):
        asyncio.run(response(scope, receive, send))
    assert released == [True] and not path.exists()


def test_range_early_return_still_cleans_file(tmp_path):
    path = tmp_path / "private.sql"
    path.write_text("-- data", encoding="utf-8")
    released = []
    messages = []
    response = export.ExportFileResponse(str(path), filename="backup.sql", release=lambda: released.append(True))
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        messages.append(message)
    asyncio.run(response({"type": "http", "method": "POST", "headers": [(b"range", b"bytes=1000000-")],
                         "asgi": {"spec_version": "2.4"}}, receive, send))
    assert messages[0]["status"] == 416
    assert released == [True] and not path.exists()


@pytest.mark.parametrize(("value", "expected"), [
    (None, "NULL"), (True, "1"), (False, "0"), (2**60, str(2**60)),
    (1.25, "1.25"), (Decimal("1234.000001"), "1234.000001"),
    (b"\x00\xff", "X'00ff'"),
    (date(2026, 10, 14), "'2026-10-14'"),
    (datetime(2026, 10, 14, 2, 3, 4, 5), "'2026-10-14 02:03:04.000005'"),
    (time(2, 3, 4, 5), "'02:03:04.000005'"),
    (timedelta(hours=-26, microseconds=-1), "'-26:00:00.000001'"),
])
def test_explicit_literal_types(value, expected):
    assert export.sql_literal(value) == expected


def test_unicode_sql_and_control_text_are_hex_encoded():
    value = "中文😀 'quoted' \\\n\r\0\x1a'); DROP TABLE items; --"
    rendered = export.sql_literal(value)
    assert rendered == "CONVERT(X'" + value.encode("utf-8").hex() + "' USING utf8mb4)"
    assert value not in rendered and "DROP TABLE" not in rendered
    assert export.sql_identifier("odd`name") == "`odd``name`"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN"), {}, object()])
def test_unsupported_or_nonfinite_values_fail_closed(value):
    with pytest.raises(export.ExportUnavailable):
        export.sql_literal(value)


class FakeCursor:
    def __init__(self, connection, streaming=False):
        self.connection = connection
        self.streaming = streaming
        self._result = SimpleNamespace(unbuffered_active=streaming, connection=connection)
        self.description = [("id",)]
        self.data = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def execute(self, command):
        self.connection.commands.append(command)
        if "information_schema.TABLES" in command:
            self.data = list(self.connection.tables.items())
        elif command.startswith("SHOW CREATE TABLE"):
            table = command.split("`")[1]
            self.row = (table, f"CREATE TABLE `{table}` (`id` text) ENGINE=InnoDB")
        elif command.startswith("SELECT `id`"):
            self.data = list(self.connection.values)

    def fetchall(self):
        assert not self.streaming, "Streaming result must never buffer all rows"
        return self.data

    def fetchone(self):
        return self.row

    def __iter__(self):
        yield from self.data
        self._result.unbuffered_active = False

    def close(self):
        if self.connection is not None and self.streaming:
            assert not self._result.unbuffered_active, "Failure cleanup must not drain the result"
        self.connection = None


class FakeNative:
    def __init__(self):
        self.commands = []
        self.tables = {"items": "InnoDB", "alembic_version": "InnoDB"}
        self.values = [("中文\0\\'😀",), (None,)]
        self.open = True
        self.streaming_cursors = []

    def set_character_set(self, charset):
        assert charset == "utf8mb4"

    def cursor(self, kind=None):
        result = FakeCursor(self, streaming=kind is SSCursor)
        if kind is SSCursor:
            self.streaming_cursors.append(result)
        return result

    def close(self):
        self.open = False


@pytest.fixture
def native_dump(monkeypatch, mysql_mode):
    native = FakeNative()
    state = {"invalidated": False, "disposed": False}
    def invalidate():
        state["invalidated"] = True
    def dispose():
        state["disposed"] = True
    pooled = SimpleNamespace(driver_connection=native, invalidate=invalidate, close=lambda: None)
    def detach():
        # SQLAlchemy no longer exposes driver_connection after detaching.
        pooled.driver_connection = None
    pooled.detach = detach
    def private_engine(url, **kwargs):
        assert kwargs["connect_args"]["read_timeout"] == 60
        pooled.driver_connection = native
        return SimpleNamespace(raw_connection=lambda: pooled, dispose=dispose)
    monkeypatch.setattr(export, "create_engine", private_engine)
    monkeypatch.setattr(export, "Base", SimpleNamespace(metadata=SimpleNamespace(tables={"items": SimpleNamespace(columns=[SimpleNamespace(name='id', computed=None)])})))
    return native, state, mysql_mode


def test_dump_uses_snapshot_show_create_and_unbuffered_rows(native_dump):
    native, state, engine = native_dump
    output = io.StringIO()
    export.write_mysql_dump(output, engine)
    sql = output.getvalue()
    assert "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ" in native.commands
    assert "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" in native.commands
    assert native.commands.index("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY") < native.commands.index("SELECT * FROM `items` LIMIT 0")
    assert "CREATE TABLE `items`" in sql and "CREATE TABLE `alembic_version`" in sql
    assert "INSERT INTO `items` (`id`)" in sql
    assert export.sql_literal(native.values[0][0]) in sql
    assert len(native.streaming_cursors) == 2
    assert not native.open and state == {"invalidated": True, "disposed": True}
    assert not any(line.startswith(("DROP ", "USE ", "CREATE DATABASE")) for line in sql.splitlines())


def test_generated_columns_keep_definition_but_are_not_inserted(native_dump):
    native, _, engine = native_dump
    export.Base.metadata.tables['items'].columns.append(SimpleNamespace(name='queue_lane', computed=object()))
    original = native.cursor
    def cursor(kind=None):
        result = original(kind)
        execute = result.execute
        def run(sql):
            execute(sql)
            if sql == 'SELECT * FROM `items` LIMIT 0':
                result.description = [('id',), ('queue_lane',)]
            if sql == 'SHOW CREATE TABLE `items`':
                result.row = ('items', "CREATE TABLE `items` (`id` text, `queue_lane` varchar(24) GENERATED ALWAYS AS ('details') VIRTUAL) ENGINE=InnoDB")
        result.execute = run
        return result
    native.cursor = cursor
    output = io.StringIO()
    export.write_mysql_dump(output, engine)
    assert 'GENERATED ALWAYS' in output.getvalue()
    assert 'INSERT INTO `items` (`id`)' in output.getvalue()
    assert 'INSERT INTO `items` (`id`,`queue_lane`)' not in output.getvalue()


@pytest.mark.parametrize("kind", ["missing", "nontransactional", "view"])
def test_missing_and_noninnodb_tables_refuse_partial_export(native_dump, kind):
    native, state, engine = native_dump
    if kind == "missing":
        del native.tables["items"]
    else:
        native.tables["items"] = "MyISAM" if kind == "nontransactional" else None
    output = io.StringIO()
    with pytest.raises(export.ExportUnavailable):
        export.write_mysql_dump(output, engine)
    assert output.getvalue() == ""
    assert not native.open and state["invalidated"]


def test_bounded_batches_and_failed_write_abort_without_drain(native_dump, monkeypatch):
    native, _, engine = native_dump
    native.values = [(str(i),) for i in range(7)]
    monkeypatch.setattr(export, "INSERT_BATCH_ROWS", 2)
    output = io.StringIO()
    export.write_mysql_dump(output, engine)
    assert output.getvalue().count("INSERT INTO `items`") == 4

    native.open = True
    native.values = [("x" * 6000,)] * 10
    monkeypatch.setattr(export, "MAX_EXPORT_BYTES", 20000)
    monkeypatch.setattr(export, "INSERT_BATCH_BYTES", 10000)
    with pytest.raises(export.ExportUnavailable):
        export.write_mysql_dump(io.StringIO(), engine)
    assert not native.open
    assert native.streaming_cursors[-1].connection is None
    assert native.streaming_cursors[-1]._result.unbuffered_active is False


def test_export_time_limit(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(export, "monotonic", lambda: clock[0])
    stream = export._BoundedWriter(io.StringIO())
    clock[0] += export.MAX_EXPORT_SECONDS + 1
    with pytest.raises(export.ExportUnavailable):
        stream.write("data")
