import sqlite3
import io
import logging
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.dev_migrations import _configuration, upgrade_development_database


def test_adopts_complete_legacy_schema_with_online_backup_and_preserves_rows(tmp_path):
    database = tmp_path / "legacy.db"
    url = "sqlite:///" + database.as_posix()
    command.upgrade(_configuration(url), "97c016913066")
    engine = create_engine(url)
    with engine.begin() as connection:
        # Emulate the old Base.create_all development database in this test file only.
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text("INSERT INTO source_accounts(id,platform,label,credentials_encrypted,created_at,updated_at) VALUES ('kept','jm','keep me','encrypted-value','2026-01-01','2026-01-01')"))
    result = upgrade_development_database(database_url=url)
    assert result["adopted"] is True
    assert Path(result["backup"]).is_file()
    assert "network_settings" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT label, verified_at FROM source_accounts WHERE id='kept'")).one() == ("keep me", None)
    with sqlite3.connect(result["backup"]) as original:
        assert original.execute("SELECT label FROM source_accounts").fetchone()[0] == "keep me"
        assert "verified_at" not in [row[1] for row in original.execute("PRAGMA table_info(source_accounts)")]
    assert upgrade_development_database(database_url=url)["backup"] is None
    engine.dispose()


def test_unknown_development_schema_is_not_stamped_or_modified(tmp_path):
    database = tmp_path / "unknown.db"
    url = "sqlite:///" + database.as_posix()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE important_notes (note TEXT)")
        connection.execute("INSERT INTO important_notes VALUES ('must remain')")
    with pytest.raises(RuntimeError, match="未修改"):
        upgrade_development_database(database_url=url)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT note FROM important_notes").fetchone()[0] == "must remain"
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("important_notes",)]


def test_fresh_development_database_uses_migrations(tmp_path):
    database = tmp_path / "fresh.db"
    url = "sqlite:///" + database.as_posix()
    result = upgrade_development_database(database_url=url)
    assert result["adopted"] is False
    assert result["backup"] is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == result["revision"]
        assert "verified_at" in [row[1] for row in connection.execute("PRAGMA table_info(source_accounts)")]


def test_migrations_keep_existing_background_worker_logger_enabled(tmp_path):
    # The worker is imported before programmatic migration, as happens when
    # application modules and migration tooling share a process.
    from app import jobs
    logger = jobs.log
    assert not logger.disabled
    url = "sqlite:///" + (tmp_path / "logging.db").as_posix()
    upgrade_development_database(database_url=url)
    assert not logger.disabled
    # Use a temporary sink after Alembic's intentional console configuration;
    # unlike a logger-level override, a handler cannot enable a disabled logger.
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    logger.addHandler(handler)
    try:
        logger.error("background diagnostic remains available")
        assert "background diagnostic remains available" in sink.getvalue()
    finally:
        logger.removeHandler(handler)
        handler.close()
