"""Safely adopt known pre-Alembic development schemas before local startup."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from .config import settings


def _configuration(database_url=None):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def _schema(engine):
    inspector = inspect(engine)
    result = {}
    for table in sorted(set(inspector.get_table_names()) - {"alembic_version"}):
        columns = {column["name"]: (str(column["type"]), column["nullable"], column.get("default"))
                   for column in inspector.get_columns(table)}
        primary = tuple(inspector.get_pk_constraint(table).get("constrained_columns") or [])
        unique = sorted(tuple(value["column_names"]) for value in inspector.get_unique_constraints(table))
        foreign = sorted((tuple(value["constrained_columns"]), value["referred_table"],
                          tuple(value["referred_columns"]), value.get("options", {}).get("ondelete"))
                         for value in inspector.get_foreign_keys(table))
        indexes = sorted((tuple(value["column_names"]), bool(value["unique"])) for value in inspector.get_indexes(table))
        result[table] = (columns, primary, unique, foreign, indexes)
    return result


def _known_sqlite_schemas(config):
    """Reconstruct repository migrations in RAM; never infer versions from a name."""
    script = ScriptDirectory.from_config(config)
    if len(script.get_heads()) != 1:
        raise RuntimeError("自动开发库迁移只支持单一迁移分支，请先合并迁移分支")
    reference = create_engine("sqlite:///:memory:")
    try:
        for revision in reversed(list(script.walk_revisions())):
            with reference.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    revision.module.upgrade()
            yield revision.revision, _schema(reference)
    finally:
        reference.dispose()


def _backup_sqlite(engine):
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    original = Path(database).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = original.with_name(f"{original.stem}.pre-migrate-{stamp}.db")
    # sqlite backup includes committed WAL pages and remains consistent online.
    with engine.connect() as connection, sqlite3.connect(str(backup)) as destination:
        connection.connection.driver_connection.backup(destination)
    return backup


def upgrade_development_database(*, database_url=None):
    """Upgrade without dropping/recreating user tables; unknown schemas are refused."""
    database_url = database_url or settings.database_url
    config = _configuration(database_url)
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    engine = create_engine(database_url)
    backup = None
    adopt = None
    try:
        tables = set(inspect(engine).get_table_names())
        if "alembic_version" in tables:
            with engine.connect() as connection:
                revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            if revisions == [head]:
                return {"revision": head, "backup": None, "adopted": False}
        elif tables:
            if engine.dialect.name != "sqlite":
                raise RuntimeError("已有未记录版本的数据库不是 SQLite，拒绝自动接管；请人工核对迁移版本")
            actual = _schema(engine)
            adopt = next((revision for revision, schema in _known_sqlite_schemas(config) if schema == actual), None)
            if not adopt:
                raise RuntimeError("开发数据库结构与已知版本不一致，已停止且未修改数据库；请先备份并检查迁移")
        if tables and engine.dialect.name == "sqlite":
            backup = _backup_sqlite(engine)
    finally:
        engine.dispose()
    if adopt:
        command.stamp(config, adopt)
    command.upgrade(config, "head")
    return {"revision": head, "backup": str(backup) if backup else None, "adopted": bool(adopt)}
