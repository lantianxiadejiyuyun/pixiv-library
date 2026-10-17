"""Keep moved deployment files from changing the project or exposing secrets."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

PROJECT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location("deployment_test_" + name, PROJECT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_arguments_preserve_repository_relative_mounts_and_intel_overlay(tmp_path):
    compose = load_script("compose")
    command = compose.compose_command("exec", "-T", "app", "python", "-V", intel=True, root=tmp_path)
    assert command[:2] == ["docker", "compose"]
    assert command[command.index("--project-directory") + 1] == str(tmp_path.resolve())
    assert command[command.index("--env-file") + 1] == str(tmp_path / "deploy" / ".env")
    assert [command[index + 1] for index, value in enumerate(command) if value == "-f"] == [
        str(tmp_path / "deploy" / "compose.yaml"), str(tmp_path / "deploy" / "compose.intel.yaml")]
    assert command[-5:] == ["exec", "-T", "app", "python", "-V"]
    assert compose.compose_command("up", "-d", "--build", root=tmp_path)[-3:] == ["up", "-d", "--build"]


def test_backup_configuration_reads_only_moved_private_file(tmp_path, monkeypatch):
    backup = load_script("backup")
    monkeypatch.setattr(backup, "ROOT", tmp_path)
    (tmp_path / "deploy").mkdir()
    (tmp_path / ".env").write_text("LIBRARY_DATA_PATH=obsolete-root-path\n", encoding="utf-8")
    (tmp_path / "deploy" / ".env").write_text("LIBRARY_DATA_PATH=./synthetic-library\n", encoding="utf-8")
    assert backup.configuration()["LIBRARY_DATA_PATH"] == "./synthetic-library"


@pytest.mark.skipif(shutil.which("git") is None, reason="Git CLI is required for checkout bootstrap validation")
def test_fresh_checkout_protects_credentials_before_bootstrap_and_preserves_existing_env(tmp_path):
    for filename in ("scripts/configure_git.py", "scripts/bootstrap_env.py", "config/git/ignore", "config/git/attributes", "deploy/.gitignore"):
        destination = tmp_path / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / filename, destination)
    # A checkout is configured before any new private configuration is written.
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    first = subprocess.run([sys.executable, str(tmp_path / "scripts/bootstrap_env.py")], cwd=tmp_path, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    environment = (tmp_path / "deploy/.env").read_bytes()
    assert b"MYSQL_PASSWORD=" in environment
    values = dict(line.split("=", 1) for line in environment.decode().splitlines() if "=" in line and not line.startswith("#"))
    assert 8 <= len(values["LIBRARY_ADMIN_PASSWORD"]) <= 20
    assert values["LIBRARY_ADMIN_PASSWORD"] not in first.stdout + first.stderr
    assert not (tmp_path / ".env").exists()
    second = subprocess.run([sys.executable, str(tmp_path / "scripts/bootstrap_env.py")], cwd=tmp_path, capture_output=True, text=True)
    assert second.returncode != 0 and "left unchanged" in second.stderr
    assert (tmp_path / "deploy/.env").read_bytes() == environment
    for protected in ("deploy/.env", ".runtime/private-key", "data/book", "models/model", "imports/queued"):
        assert subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "--quiet", protected]).returncode == 0
    assert subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "--quiet", "deploy/.env.example"]).returncode == 1
    tracked_candidates = subprocess.run(["git", "-C", str(tmp_path), "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, check=True).stdout
    assert "deploy/.env\n" not in tracked_candidates and ".runtime/" not in tracked_candidates


def test_zip_deployment_bootstraps_without_git_and_still_preserves_existing_environment(tmp_path):
    for filename in ("scripts/configure_git.py", "scripts/bootstrap_env.py", "deploy/.gitignore"):
        destination = tmp_path / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / filename, destination)
    # No .git, Git executable or config/git files: this is an unpacked ZIP/NAS.
    no_git = {**os.environ, "PATH": ""}
    first = subprocess.run([sys.executable, str(tmp_path / "scripts/bootstrap_env.py")], cwd=tmp_path,
                           env=no_git, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "Before adding this project to Git" in first.stdout
    environment = (tmp_path / "deploy/.env").read_bytes()
    assert environment and not (tmp_path / ".env").exists()
    assert (tmp_path / "data/.library-storage").read_text() == "qiye-library-v1\n"
    second = subprocess.run([sys.executable, str(tmp_path / "scripts/bootstrap_env.py")], cwd=tmp_path,
                            env=no_git, capture_output=True, text=True)
    assert second.returncode != 0 and "left unchanged" in second.stderr
    assert (tmp_path / "deploy/.env").read_bytes() == environment


def test_root_pytest_invocation_isolates_late_conftest_and_preserves_supplied_basetemp(tmp_path):
    tests = tmp_path / "server" / "tests"
    tests.mkdir(parents=True)
    shutil.copyfile(PROJECT / "server/pytest.ini", tmp_path / "server/pytest.ini")
    shutil.copyfile(PROJECT / "server/tests/conftest.py", tests / "conftest.py")
    (tests / "test_probe.py").write_text('''import os
from sqlalchemy import create_engine
from conftest import TEST_ROOT, assert_test_database

def test_isolated(tmp_path, tmp_path_factory):
    assert os.environ["LIBRARY_ENV"] == "test"
    assert tmp_path.resolve().is_relative_to(TEST_ROOT)
    assert tmp_path_factory.getbasetemp().resolve().is_relative_to(TEST_ROOT)
    engine = create_engine(os.environ["LIBRARY_DATABASE_URL"])
    assert_test_database(engine)
    with engine.connect():
        pass
    engine.dispose()
''', encoding="utf-8")
    unsafe_temp = tmp_path / "existing-directory"
    unsafe_temp.mkdir()
    sentinel = unsafe_temp / "must-remain.txt"
    sentinel.write_text("keep", encoding="utf-8")
    environment = {**os.environ, "LIBRARY_ENV": "production",
                   "LIBRARY_DATABASE_URL": "mysql+pymysql://must-not-connect:synthetic@192.0.2.1/library"}
    # No explicit server/tests: this is the exact invocation that discovers
    # conftest after pytest's initial temporary-directory factory is created.
    result = subprocess.run([sys.executable, "-m", "pytest", "-c", "server/pytest.ini",
                             "--basetemp", str(unsafe_temp), "-q"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert sentinel.read_text(encoding="utf-8") == "keep"
