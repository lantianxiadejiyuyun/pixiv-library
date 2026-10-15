"""Regression coverage for production-shell test execution and reset safeguards."""
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app.config import settings
from app.database import engine
from conftest import TEST_ROOT, assert_test_database


def test_application_configuration_is_isolated_before_collection():
    assert settings.environment == "test"
    assert_test_database(engine)
    for directory in (settings.storage_root, settings.import_root, settings.model_dir):
        assert directory.resolve().is_relative_to(TEST_ROOT)
    assert settings.admin_password == "local-library"


def test_pytest_temporary_databases_stay_within_the_protected_root(tmp_path, tmp_path_factory):
    assert tmp_path.resolve().is_relative_to(TEST_ROOT)
    assert tmp_path_factory.getbasetemp().resolve().is_relative_to(TEST_ROOT)
    temporary_engine = create_engine("sqlite:///" + (tmp_path / "isolated.db").as_posix())
    assert_test_database(temporary_engine)
    with temporary_engine.connect():
        pass
    temporary_engine.dispose()


def test_mysql_is_blocked_before_driver_connect(monkeypatch):
    forbidden = create_engine("mysql+pymysql://production:secret@127.0.0.1:3306/production")
    connect = Mock(side_effect=AssertionError("Database driver must never be called"))
    monkeypatch.setattr(forbidden.dialect.dbapi, "connect", connect)
    with pytest.raises(RuntimeError, match="non-SQLite"):
        forbidden.connect()
    connect.assert_not_called()
    with pytest.raises(AssertionError, match="non-SQLite"):
        assert_test_database(forbidden)
    forbidden.dispose()


def test_external_sqlite_is_blocked_before_creating_file(monkeypatch):
    forbidden = create_engine("sqlite:///outside-test-root.db")
    connect = Mock(side_effect=AssertionError("Database driver must never be called"))
    monkeypatch.setattr(forbidden.dialect.dbapi, "connect", connect)
    with pytest.raises(RuntimeError, match="outside this test run"):
        forbidden.connect()
    connect.assert_not_called()
    with pytest.raises(AssertionError, match="outside this test run"):
        assert_test_database(forbidden)
    forbidden.dispose()
