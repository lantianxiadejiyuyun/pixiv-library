"""Portable logical backups of this application's MySQL tables.

No database client binary, Docker socket, credentials file or media access is
required. Run schema migrations separately from a backup, as with mysqldump's
single-transaction mode. Restore the result into an empty MySQL database.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import logging
import math
import os
from pathlib import Path
import tempfile
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pymysql.cursors import SSCursor
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from .auth import require_auth
from .database import Base, engine
from . import models as _models  # Register every application table for standalone use.


router = APIRouter(dependencies=[Depends(require_auth)])
logger = logging.getLogger(__name__)
_export_lock = Lock()
INSERT_BATCH_ROWS = 200
INSERT_BATCH_BYTES = 1024 * 1024
MAX_EXPORT_BYTES = 1024 * 1024 * 1024
MAX_EXPORT_SECONDS = 600


class ExportUnavailable(Exception):
    """An incomplete or unsupported schema must not produce a partial backup."""


class _BoundedWriter:
    def __init__(self, stream):
        self.stream = stream
        self.size = 0
        self.deadline = monotonic() + MAX_EXPORT_SECONDS

    def check(self):
        if monotonic() > self.deadline:
            raise ExportUnavailable()

    def write(self, value):
        self.check()
        self.size += len(value.encode("utf-8"))
        if self.size > MAX_EXPORT_BYTES:
            raise ExportUnavailable()
        self.stream.write(value)


def sql_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ExportUnavailable()
    return "`" + value.replace("`", "``") + "`"


def sql_literal(value) -> str:
    """Encode driver values without depending on the source/target SQL mode."""
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "CONVERT(X'" + value.encode("utf-8").hex() + "' USING utf8mb4)"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        if not math.isfinite(value):
            raise ExportUnavailable()
        return repr(value) if isinstance(value, float) else format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ExportUnavailable()
        return "'" + value.isoformat(sep=" ", timespec="microseconds") + "'"
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ExportUnavailable()
        return "'" + value.isoformat(timespec="microseconds") + "'"
    if isinstance(value, date):
        return "'" + value.isoformat() + "'"
    if isinstance(value, timedelta):
        # PyMySQL returns MySQL TIME columns as timedeltas, including >24 hours.
        micros = (value.days * 86400 + value.seconds) * 1000000 + value.microseconds
        sign = "-" if micros < 0 else ""
        seconds, fraction = divmod(abs(micros), 1000000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"'{sign}{hours:02}:{minutes:02}:{seconds:02}.{fraction:06}'"
    raise ExportUnavailable()


_HEADER = """-- Qiye library database backup. Restore into an EMPTY MySQL database.
-- Includes application tables and encrypted account/settings values.
-- Excludes media, models, .env and encryption keys. Keep the original key separately.
-- Do not run schema migrations during export. SQL is not a media backup.
SET @QIYE_OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT;
SET @QIYE_OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS;
SET @QIYE_OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION;
SET @QIYE_OLD_TIME_ZONE=@@TIME_ZONE;
SET @QIYE_OLD_SQL_MODE=@@SQL_MODE;
SET @QIYE_OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS;
SET NAMES utf8mb4;
SET TIME_ZONE='+00:00';
SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';
SET FOREIGN_KEY_CHECKS=0;
"""
_FOOTER = """SET FOREIGN_KEY_CHECKS=@QIYE_OLD_FOREIGN_KEY_CHECKS;
SET SQL_MODE=@QIYE_OLD_SQL_MODE;
SET TIME_ZONE=@QIYE_OLD_TIME_ZONE;
SET CHARACTER_SET_CLIENT=@QIYE_OLD_CHARACTER_SET_CLIENT;
SET CHARACTER_SET_RESULTS=@QIYE_OLD_CHARACTER_SET_RESULTS;
SET COLLATION_CONNECTION=@QIYE_OLD_COLLATION_CONNECTION;
-- Backup complete.
"""


def _write_rows(stream, connection, table: str, columns: list[str]) -> None:
    names = ",".join(sql_identifier(column) for column in columns)
    prefix = f"INSERT INTO {sql_identifier(table)} ({names}) VALUES\n"
    batch = []
    batch_bytes = len(prefix.encode("utf-8"))

    def flush():
        nonlocal batch_bytes
        if batch:
            stream.write(prefix + ",\n".join(batch) + ";\n")
            batch.clear()
            batch_bytes = len(prefix.encode("utf-8"))

    cursor = connection.cursor(SSCursor)
    try:
        cursor.execute(f"SELECT {names} FROM {sql_identifier(table)}")
        for values in cursor:
            stream.check()
            row = "(" + ",".join(sql_literal(value) for value in values) + ")"
            size = len(row.encode("utf-8")) + 2
            if batch and (len(batch) >= INSERT_BATCH_ROWS or batch_bytes + size > INSERT_BATCH_BYTES):
                flush()
            batch.append(row)
            batch_bytes += size
            # One large value may exceed the batching target; do not accumulate
            # additional rows beside it. Memory is bounded by one database row.
            if batch_bytes >= INSERT_BATCH_BYTES:
                flush()
        flush()
    except BaseException:
        # SSCursor.close() drains every unread row. Abort the dedicated socket
        # first instead of continuing a failed/oversized backup in the background.
        # PyMySQL's result destructor also drains unless unbuffered_active is off.
        result = getattr(cursor, "_result", None)
        if result is not None:
            result.unbuffered_active = False
            result.connection = None
        cursor.connection = None
        if connection.open:
            connection.close()
        raise
    finally:
        cursor.close()


def write_mysql_dump(stream, database_engine=engine) -> None:
    """Write a complete snapshot or raise, leaving cleanup to the caller.

    A private NullPool engine keeps bounded network timeouts and session settings
    separate from the application's pool. Discard the entire file on failure.
    """
    if database_engine.dialect.name != "mysql" or database_engine.dialect.driver != "pymysql":
        raise ExportUnavailable()
    stream = _BoundedWriter(stream)
    tables = sorted(set(Base.metadata.tables) | {"alembic_version"})
    private_engine = create_engine(database_engine.url, poolclass=NullPool,
                                   connect_args={"connect_timeout": 10, "read_timeout": 60, "write_timeout": 60})
    pooled = None
    connection = None
    try:
        pooled = private_engine.raw_connection()
        connection = pooled.driver_connection
        pooled.detach()
        connection.set_character_set("utf8mb4")
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION time_zone='+00:00'")
            cursor.execute("SET SESSION sql_mode='NO_AUTO_VALUE_ON_ZERO'")
            cursor.execute("SET SESSION sql_quote_show_create=1")
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            cursor.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
            available = dict(cursor.fetchall())
            if any((available.get(table) or "").upper() != "INNODB" for table in tables):
                raise ExportUnavailable()
            definitions = []
            for table in tables:
                stream.check()
                # The transaction retains these metadata locks until rollback,
                # protecting each captured table definition while rows stream.
                cursor.execute(f"SELECT * FROM {sql_identifier(table)} LIMIT 0")
                columns = [entry[0] for entry in cursor.description]
                if table in Base.metadata.tables:
                    generated = {column.name for column in Base.metadata.tables[table].columns if column.computed is not None}
                    columns = [name for name in columns if name not in generated]
                cursor.execute(f"SHOW CREATE TABLE {sql_identifier(table)}")
                row = cursor.fetchone()
                if not row or row[0] != table or not row[1].lstrip().upper().startswith("CREATE TABLE "):
                    raise ExportUnavailable()
                definitions.append((table, columns, row[1]))
        stream.write(_HEADER)
        for _table, _columns, definition in definitions:
            stream.write(definition + ";\n\n")
        stream.write("START TRANSACTION;\n")
        for table, columns, _definition in definitions:
            _write_rows(stream, connection, table, columns)
        stream.write("COMMIT;\n")
        stream.write(_FOOTER)
    finally:
        # Close the detached physical connection without issuing ROLLBACK, which
        # could drain an unread result. Closing rolls back the read-only snapshot.
        try:
            if connection is not None and connection.open:
                connection.close()
        finally:
            if pooled is not None:
                pooled.invalidate()
                pooled.close()
            private_engine.dispose()


class ExportFileResponse(FileResponse):
    """Own the private temporary file and lock even when the client disconnects."""
    def __init__(self, path: str, *, filename: str, release):
        super().__init__(path, filename=filename, media_type="application/sql; charset=utf-8",
                         headers={"Cache-Control": "no-store"})
        self._release = release
        self._cleaned = False

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            Path(self.path).unlink(missing_ok=True)
        finally:
            self._release()

    async def __call__(self, scope, receive, send):
        try:
            # A pathsend server can return before it finishes opening/sending
            # the file. Use ordinary FileResponse streaming for strict cleanup.
            scope = {**scope, "extensions": {key: value for key, value in scope.get("extensions", {}).items()
                                              if key != "http.response.pathsend"}}
            await super().__call__(scope, receive, send)
        finally:
            self.cleanup()


@router.post("/database/export")
def export_database():
    if engine.dialect.name != "mysql" or engine.dialect.driver != "pymysql":
        raise HTTPException(501, "数据库导出仅支持 MySQL；SQLite 开发模式暂不支持")
    if not _export_lock.acquire(blocking=False):
        raise HTTPException(409, "已有数据库导出正在进行，请稍后重试")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="qiye-database-", suffix=".sql")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            write_mysql_dump(stream)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return ExportFileResponse(temporary, filename=f"qiye-database-{timestamp}.sql", release=_export_lock.release)
    except BaseException as exc:
        try:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        finally:
            _export_lock.release()
        if not isinstance(exc, Exception):
            raise
        logger.warning("Database export failed type=%s", type(exc).__name__)
        raise HTTPException(503, "数据库导出失败，请检查数据库连接、表结构和临时磁盘空间后重试") from None
