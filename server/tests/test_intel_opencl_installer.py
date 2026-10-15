"""The build installer must verify every package before invoking APT."""
import hashlib
import importlib.util
import io
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "intel_opencl_installer", Path(__file__).resolve().parents[2] / "scripts" / "install_intel_opencl.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_download_checks_hash_before_publishing(tmp_path, monkeypatch):
    payload = b"verified official package bytes"
    calls = []

    def open_package(request, timeout):
        calls.append((request.full_url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(installer.urllib.request, "urlopen", open_package)
    digest = hashlib.sha256(payload).hexdigest()
    target = installer.download_package(tmp_path, "https://github.com/intel/project/", "driver.deb", digest)
    assert target.read_bytes() == payload
    assert calls == [("https://github.com/intel/project/driver.deb", 120)]
    assert not list(tmp_path.glob("*.part"))


def test_mismatch_never_publishes_package(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"wrong"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        installer.download_package(tmp_path, "https://github.com/intel/project/", "driver.deb", "0" * 64)
    assert list(tmp_path.iterdir()) == []


def test_oversized_download_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "MAX_PACKAGE_BYTES", 8)
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *_a, **_k: io.BytesIO(b"x" * 9))
    with pytest.raises(ValueError, match="size limit"):
        installer.download_package(tmp_path, "https://github.com/intel/project/", "driver.deb", "0" * 64)
    assert list(tmp_path.iterdir()) == []


def test_incomplete_package_set_cannot_run_apt(monkeypatch):
    monkeypatch.setattr(installer, "validate_platform", lambda: None)
    downloaded = []
    commands = []

    def download(directory, _url, filename, _sha):
        downloaded.append(filename)
        if len(downloaded) == 2:
            raise ValueError("SHA-256 mismatch")
        return Path(directory) / filename

    monkeypatch.setattr(installer, "download_package", download)
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(installer.subprocess, "check_output", lambda *args, **kwargs: commands.append(args))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        installer.install()
    assert len(downloaded) == 2
    assert commands == []


@pytest.mark.parametrize("system,machine", [("Windows", "AMD64"), ("Linux", "aarch64")])
def test_other_platforms_rejected_before_install(monkeypatch, system, machine):
    monkeypatch.setattr(installer.platform, "system", lambda: system)
    monkeypatch.setattr(installer.platform, "machine", lambda: machine)
    with pytest.raises(ValueError, match="Linux amd64"):
        installer.validate_platform()
