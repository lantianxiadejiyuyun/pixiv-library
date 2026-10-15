import csv
from types import SimpleNamespace

import pytest

from test_library import client, png
from app.ai import providers, service
from app.config import settings
from app.integrations.base import ConnectorError


@pytest.fixture
def model(monkeypatch, tmp_path):
    directory = tmp_path / "models" / "wd-tagger"
    directory.mkdir(parents=True)
    (directory / "model.onnx").write_bytes(b"model-fixture")
    with (directory / "selected_tags.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "category"])
        writer.writerows([("general", 9), ("sensitive", 9), ("questionable", 9), ("explicit", 9), ("test_tag", 0)])
    monkeypatch.setattr(settings, "model_dir", directory.parent)
    monkeypatch.setattr(service, "_cache", None)
    monkeypatch.setattr(service, "_load_error", None)
    info = lambda: {"runtime_available": True, "available_providers": [providers.CPU],
                    "runtime_version": "fixture-runtime", "runtime_error": None}
    monkeypatch.setattr(service, "runtime_info", info)
    monkeypatch.setattr(providers, "runtime_info", info)
    monkeypatch.setenv("LIBRARY_AI_PROVIDER", "intel")
    return directory


class CPUFixture:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.closed = False

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=[1, 32, 32, 3])]

    def run(self, outputs, inputs):
        import numpy as np
        assert inputs["image"].shape == (1, 32, 32, 3)
        if self.fail:
            raise RuntimeError("private-runtime-detail")
        return [np.array([[.99, .005, .004, .001, .8]], dtype=np.float32)]

    def close(self):
        self.closed = True


def test_api_distinguishes_installed_runtime_from_executed_device(client, model, monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "_ort_session", lambda *args: CPUFixture())
    before = client.get("/api/ai/status").json()
    assert before["available"] is True
    assert before["requested_provider"] == "intel"
    assert before["actual_provider"] is None and before["device"] is None
    assert before["execution_state"] == "not_loaded"
    assert before["fallback_reason"]
    image = tmp_path / "synthetic.png"
    image.write_bytes(png())
    prediction = service.infer_image(image)
    after = client.get("/api/ai/status").json()
    assert after["actual_provider"] == providers.CPU
    assert after["device"] == "CPU"
    assert after["execution_state"] == "fallback"
    assert prediction["rating"] == "general"
    assert prediction["tags"][0]["name"] == "test_tag"
    assert prediction["execution"]["actual_provider"] == providers.CPU


def test_failed_cached_session_is_recreated_for_next_attempt(model, monkeypatch, tmp_path):
    first, second = CPUFixture(fail=True), CPUFixture()
    choices = iter([first, second])
    monkeypatch.setattr(providers, "_ort_session", lambda *args: next(choices))
    image = tmp_path / "synthetic.png"
    image.write_bytes(png())
    with pytest.raises(ConnectorError, match="CPU 推理失败"):
        service.infer_image(image)
    assert service.model_status()["execution_state"] == "error"
    assert service.model_status()["actual_provider"] is None
    assert service.infer_image(image)["rating"] == "general"
    assert first.closed
    assert service.model_status()["execution_state"] == "fallback"


def test_model_or_provider_change_invalidates_reported_device(model, monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "_ort_session", lambda *args: CPUFixture())
    image = tmp_path / "synthetic.png"
    image.write_bytes(png())
    service.infer_image(image)
    assert service.model_status()["actual_provider"] == providers.CPU
    monkeypatch.setenv("LIBRARY_AI_PROVIDER", "cpu")
    status = service.model_status()
    assert status["requested_provider"] == "cpu"
    assert status["actual_provider"] is None
    assert status["execution_state"] == "not_loaded"
    assert status["fallback_reason"] is None


def test_model_initialization_error_is_visible_without_raw_runtime_details(model, monkeypatch):
    def fail(*args):
        raise RuntimeError("private-runtime-detail")
    monkeypatch.setattr(providers, "_ort_session", fail)
    with pytest.raises(ConnectorError, match="CPU 模型载入失败"):
        service._load_model()
    status = service.model_status()
    assert status["execution_state"] == "error"
    assert status["actual_provider"] is None
    assert "载入失败" in status["models"][0]["message"]
    assert "private-runtime" not in str(status)


def test_missing_model_keeps_actual_device_unset(model):
    (model / "model.onnx").unlink()
    status = service.model_status()
    assert status["available"] is False
    assert status["actual_provider"] is None
    assert status["device"] is None


def test_incompatible_model_metadata_releases_runtime_and_reports_error(model, monkeypatch):
    session = CPUFixture()
    session.get_inputs = lambda: [SimpleNamespace(name="image", shape=[1, 3, 32, 32])]
    monkeypatch.setattr(providers, "_ort_session", lambda *args: session)
    with pytest.raises(ConnectorError, match="模型输入不兼容"):
        service._load_model()
    assert session.closed
    assert service.model_status()["execution_state"] == "error"
    assert service.model_status()["actual_provider"] is None
