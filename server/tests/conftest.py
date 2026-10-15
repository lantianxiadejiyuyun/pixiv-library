"""Isolate tests before importing any application module or opening a database."""
import os
from pathlib import Path
import sys
import tempfile

import pytest


if "app.config" in sys.modules or "app.database" in sys.modules:
    raise RuntimeError("Test isolation must initialize before application configuration")

TEST_ROOT = Path(tempfile.mkdtemp(prefix="qiye-library-tests-")).resolve()
for name in list(os.environ):
    if name.startswith("LIBRARY_"):
        del os.environ[name]
os.environ.update({
    "LIBRARY_DATABASE_URL": f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}",
    "LIBRARY_STORAGE_ROOT": str(TEST_ROOT / "storage"),
    "LIBRARY_IMPORT_ROOT": str(TEST_ROOT / "import"),
    "LIBRARY_MODEL_DIR": str(TEST_ROOT / "models"),
    "LIBRARY_ADMIN_PASSWORD": "local-library",
    "LIBRARY_SECRET_KEY": "isolated-test-secret-never-used-outside-tests",
    "LIBRARY_WORKER_ENABLED": "false",
    "LIBRARY_MANAGED_PROXY_RUNTIME": "false",
    "LIBRARY_ENV": "test",
    "LIBRARY_SECURE_COOKIE": "false",
    "LIBRARY_STORAGE_REQUIRE_MARKER": "false",
    "LIBRARY_ALLOWED_ORIGINS": "",
})

from sqlalchemy import event
from sqlalchemy.engine import Dialect


def assert_test_database(engine):
    """Explicit destructive-operation guard, independent of app configuration."""
    assert engine.dialect.name == "sqlite", "Tests must never use a non-SQLite database"
    database = engine.url.database
    assert database and database != ":memory:", "Reset fixture requires its isolated database file"
    assert Path(database).resolve().is_relative_to(TEST_ROOT), "Database lies outside this test run"


@event.listens_for(Dialect, "do_connect")
def reject_external_database_connections(dialect, _connection_record, args, _kwargs):
    # do_connect runs BEFORE DBAPI.connect, even for engines accidentally created
    # with a production URL. Reference migration schemas may use in-memory SQLite.
    if dialect.name != "sqlite":
        raise RuntimeError("Test database guard: non-SQLite connections are forbidden")
    database = args[0] if args else None
    if database == ":memory:":
        return
    if not database or not Path(database).resolve().is_relative_to(TEST_ROOT):
        raise RuntimeError("Test database guard: database path is outside this test run")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # tmp_path also remains under the protected root, irrespective of shell env
    # or a caller-provided --basetemp pointing at an existing directory.
    config.option.basetemp = str(TEST_ROOT / "pytest")
    # A run started above server/ can discover this conftest only during
    # collection, after pytest has already constructed its TempPathFactory.
    # Updating the option alone does not update that factory. Recreate it from
    # the protected option before any test fixture runs; never allow an
    # inherited --basetemp to delete or place test databases outside TEST_ROOT.
    if hasattr(config, "_tmp_path_factory"):
        config._tmp_path_factory = pytest.TempPathFactory.from_config(config, _ispytest=True)
