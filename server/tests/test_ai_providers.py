"""Provider recovery tests; subprocess targets import no application database."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from app.ai import providers as p


class FakeSession:
    device = "Intel(R) UHD Graphics"

    def __init__(self, *, fail=False):
        self.fail = fail
        self.closed = False
        self.calls = 0

    def get_inputs(self):
        return [SimpleNamespace(name="image", shape=[1, 448, 448, 3], type="tensor(float)")]

    def run(self, outputs, inputs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private-driver-path-do-not-expose")
        return [[1.0]]

    def close(self):
        self.closed = True


@pytest.fixture
def runtime(monkeypatch):
    def configure(available):
        monkeypatch.setattr(p, "runtime_info", lambda: {
            "runtime_available": True, "available_providers": available,
            "runtime_version": "test", "runtime_error": None})
    configure([p.CPU])
    return configure


def test_cpu_status_reports_actual_device_only_after_inference(runtime, monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(p, "_ort_session", lambda model, provider: session)
    facade = p.InferenceSession(Path("model.onnx"), "cpu")
    assert facade.status() == {"requested_provider": "cpu", "actual_provider": None,
        "device": None, "fallback_reason": None, "execution_state": "not_loaded"}
    assert facade.run(None, {"image": []}) == [[1.0]]
    assert facade.status()["actual_provider"] == p.CPU
    assert facade.status()["device"] == "CPU"
    assert facade.status()["execution_state"] == "ready"


def test_intel_package_missing_uses_cpu_with_visible_reason(runtime, monkeypatch):
    monkeypatch.setattr(p, "_ort_session", lambda *args: FakeSession())
    facade = p.InferenceSession(Path("model.onnx"), "intel")
    assert "OpenVINO" in facade.status()["fallback_reason"]
    assert facade.status()["actual_provider"] is None
    facade.run(None, {})
    assert facade.status()["actual_provider"] == p.CPU
    assert facade.status()["execution_state"] == "fallback"


@pytest.mark.parametrize("mode", ["auto", "intel"])
def test_intel_compile_failure_falls_back_and_redacts_native_errors(mode, runtime, monkeypatch):
    runtime([p.INTEL, p.CPU])
    def unavailable(model):
        raise RuntimeError("private-driver-path-do-not-expose")
    monkeypatch.setattr(p, "_OpenVINOSession", unavailable)
    monkeypatch.setattr(p, "_ort_session", lambda *args: FakeSession())
    facade = p.InferenceSession(Path("model.onnx"), mode)
    facade.run(None, {})
    status = facade.status()
    assert status["actual_provider"] == p.CPU
    assert "编译失败" in status["fallback_reason"]
    assert "private-driver" not in str(status)


@pytest.mark.parametrize("mode", ["auto", "intel"])
def test_intel_runtime_failure_retries_same_input_on_cpu_once(mode, runtime, monkeypatch):
    runtime([p.INTEL, p.CPU])
    accelerated, cpu = FakeSession(fail=True), FakeSession()
    monkeypatch.setattr(p, "_OpenVINOSession", lambda model: accelerated)
    created = []
    def create(model, provider):
        created.append(provider)
        return cpu
    monkeypatch.setattr(p, "_ort_session", create)
    facade = p.InferenceSession(Path("model.onnx"), mode)
    assert facade.status()["actual_provider"] is None
    assert facade.run(None, {"image": [3]}) == [[1.0]]
    assert facade.run(None, {"image": [4]}) == [[1.0]]
    assert created == [p.CPU]
    assert accelerated.closed and accelerated.calls == 1 and cpu.calls == 2
    assert facade.status()["actual_provider"] == p.CPU
    assert "推理失败" in facade.status()["fallback_reason"]


def test_intel_success_reports_confirmed_hardware_name(runtime, monkeypatch):
    runtime([p.INTEL, p.CPU])
    monkeypatch.setattr(p, "_OpenVINOSession", lambda model: FakeSession())
    facade = p.InferenceSession(Path("model.onnx"), "intel")
    facade.run(None, {})
    assert facade.status()["actual_provider"] == p.INTEL
    assert facade.status()["device"] == FakeSession.device
    assert facade.status()["execution_state"] == "ready"


def test_auto_tries_cuda_then_intel_before_cpu(runtime, monkeypatch):
    runtime([p.CUDA, p.INTEL, p.CPU])
    attempted = []
    def ort_session(model, provider):
        attempted.append(provider)
        raise RuntimeError("CUDA missing library")
    def intel_session(model):
        attempted.append(p.INTEL)
        return FakeSession()
    monkeypatch.setattr(p, "_ort_session", ort_session)
    monkeypatch.setattr(p, "_OpenVINOSession", intel_session)
    facade = p.InferenceSession(Path("model.onnx"), "auto")
    facade.run(None, {})
    assert attempted == [p.CUDA, p.INTEL]
    assert facade.status()["actual_provider"] == p.INTEL
    assert "CUDA" in facade.status()["fallback_reason"]


@pytest.mark.parametrize("failure", ["missing", "initialize", "run"])
def test_explicit_cuda_stays_strict(failure, runtime, monkeypatch):
    runtime([p.CPU] if failure == "missing" else [p.CUDA, p.CPU])
    attempted = []
    def create(model, provider):
        attempted.append(provider)
        if failure == "initialize":
            raise RuntimeError("private-driver-path")
        return FakeSession(fail=True)
    monkeypatch.setattr(p, "_ort_session", create)
    with pytest.raises(p.AIRuntimeError, match="CUDA") as exc:
        facade = p.InferenceSession(Path("model.onnx"), "cuda")
        facade.run(None, {})
    assert p.CPU not in attempted
    assert "private-driver" not in str(exc.value)
    if failure == "run":
        assert facade.status()["execution_state"] == "error"
        assert facade.status()["actual_provider"] is None


def test_cpu_failure_after_gpu_failure_is_actionable(runtime, monkeypatch):
    runtime([p.INTEL, p.CPU])
    monkeypatch.setattr(p, "_OpenVINOSession", lambda model: FakeSession(fail=True))
    monkeypatch.setattr(p, "_ort_session", lambda *args: FakeSession(fail=True))
    facade = p.InferenceSession(Path("model.onnx"), "intel")
    with pytest.raises(p.AIRuntimeError, match="均失败"):
        facade.run(None, {})
    assert facade.status()["execution_state"] == "error"
    assert facade.status()["actual_provider"] is None


@pytest.mark.parametrize("devices", [
    {"CPU": "Intel CPU", "GPU": "NVIDIA GeForce"},
    {"CPU": "Intel CPU"},
])
def test_openvino_does_not_label_other_vendors_or_cpu_as_intel_gpu(devices, monkeypatch):
    monkeypatch.setitem(sys.modules, "openvino_telemetry", SimpleNamespace())
    core = SimpleNamespace(available_devices=list(devices), get_property=lambda device, prop: devices[device])
    monkeypatch.setitem(sys.modules, "openvino", SimpleNamespace(Core=lambda: core))
    messages = []
    connection = SimpleNamespace(send=messages.append, close=lambda: None)
    p._openvino_worker(connection, "not-opened.onnx")
    assert messages == [("error", "没有可用的 Intel GPU")]


def test_openvino_uses_correct_gpu_and_disables_silent_cpu_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "openvino_telemetry", SimpleNamespace())
    devices = {"CPU": "Intel CPU", "GPU.0": "NVIDIA GeForce", "GPU.1": "Intel(R) UHD Graphics"}
    core = SimpleNamespace(available_devices=list(devices), get_property=lambda device, prop: devices[device])
    monkeypatch.setitem(sys.modules, "openvino", SimpleNamespace(Core=lambda: core))
    options = SimpleNamespace(add_session_config_entry=lambda name, value: entries.update({name: value}))
    entries, parameters, messages = {}, {}, []
    fake = FakeSession()
    fake.disable_fallback = lambda: None
    fake.get_providers = lambda: [p.INTEL]
    def create(model, **kwargs):
        parameters.update(kwargs)
        return fake
    ort = SimpleNamespace(SessionOptions=lambda: options, disable_telemetry_events=lambda: None, InferenceSession=create)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    connection = SimpleNamespace(send=messages.append, close=lambda: None, recv=lambda: ("stop", None, None))
    p._openvino_worker(connection, "model.onnx")
    assert sys.modules["openvino_telemetry"] is None
    assert messages[0][0] == "ready"
    assert messages[0][1]["device"] == devices["GPU.1"]
    assert parameters["providers"][0][1]["device_type"] == "GPU.1"
    assert parameters["enable_fallback"] is False
    assert entries["session.disable_cpu_ep_fallback"] == "1"
    assert all(provider != p.CPU for provider in parameters["providers"])


def _exit_worker(connection, model):
    # Simulates a native driver access violation without importing GPU drivers.
    os._exit(17)


def test_native_driver_process_exit_is_contained(monkeypatch):
    monkeypatch.setattr(p, "_openvino_worker", _exit_worker)
    with pytest.raises(p.AIRuntimeError, match="意外退出"):
        p._OpenVINOSession(Path("unused.onnx"))


def test_runtime_import_failure_is_safe_and_actionable(monkeypatch):
    def unavailable(name):
        raise OSError("private-runtime-dll-path")
    monkeypatch.setattr(p.importlib, "import_module", unavailable)
    result = p.runtime_info()
    assert result["runtime_available"] is False
    assert result["available_providers"] == []
    assert "private-runtime" not in str(result)
