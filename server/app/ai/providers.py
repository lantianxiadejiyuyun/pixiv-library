"""Local ONNX providers with explicit, observable CPU recovery.

OpenVINO uses a child process: a native graphics-driver crash cannot kill the
web server. No model download, network request, or untrusted code is involved.
Provider options: https://onnxruntime.ai/docs/reference/execution-providers/OpenVINO-ExecutionProvider.html
"""
from __future__ import annotations

import importlib
import multiprocessing
from pathlib import Path
import sys
from threading import Lock
from types import SimpleNamespace

CPU = "CPUExecutionProvider"
CUDA = "CUDAExecutionProvider"
INTEL = "OpenVINOExecutionProvider"
MODES = {"auto", "cpu", "cuda", "intel"}


class AIRuntimeError(ValueError):
    pass


def runtime_info():
    try:
        importlib.import_module("numpy")
        ort = importlib.import_module("onnxruntime")
        ort.disable_telemetry_events()
        return {"runtime_available": True, "available_providers": list(ort.get_available_providers()),
                "runtime_version": ort.__version__, "runtime_error": None}
    except Exception:
        return {"runtime_available": False, "available_providers": [], "runtime_version": None,
                "runtime_error": "本地推理运行时未安装或无法载入；安装匹配平台的 AI 依赖后重启"}


def _options(ort, *, intel=False):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.log_severity_level = 4
    if intel:
        # Registered providers alone do not prove model execution uses a GPU.
        # Refuse an unsupported graph instead of silently running all of it on
        # CPU while reporting Intel acceleration; our explicit fallback handles it.
        options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    return options


def _ort_session(model, provider):
    import onnxruntime as ort
    ort.disable_telemetry_events()
    providers = [provider, CPU] if provider == CUDA else [CPU]
    session = ort.InferenceSession(str(model), sess_options=_options(ort), providers=providers, enable_fallback=False)
    session.disable_fallback()
    if provider not in session.get_providers():
        raise AIRuntimeError("所选设备未能初始化")
    return session


def _openvino_worker(connection, model):
    """Spawn target imports only runtime packages, never application DB modules."""
    phase = "初始化或模型编译"
    try:
        # The pinned OpenVINO Python package sends an import-usage event through
        # this optional module by default. Its own ImportError handler selects
        # telemetry_stub when absent. Block it only in our dedicated child; do
        # not change a user's global telemetry preferences or write consent files.
        sys.modules["openvino_telemetry"] = None
        # Query hardware inside the child, since driver enumeration itself can
        # crash natively. Some OpenCL stacks expose NVIDIA as "GPU" too.
        import openvino
        core = openvino.Core()
        intel_devices = [(device, core.get_property(device, "FULL_DEVICE_NAME"))
                         for device in core.available_devices if device.startswith("GPU")]
        intel_devices = [(device, name) for device, name in intel_devices if "intel" in name.lower()]
        if not intel_devices:
            raise AIRuntimeError("没有可用的 Intel GPU")
        device, device_name = intel_devices[0]
        import onnxruntime as ort
        ort.disable_telemetry_events()
        session = ort.InferenceSession(str(model), sess_options=_options(ort, intel=True),
            providers=[(INTEL, {"device_type": device, "load_config": '{"GPU":{"EXECUTION_MODE_HINT":"ACCURACY"}}'})],
            enable_fallback=False)
        session.disable_fallback()
        if INTEL not in session.get_providers():
            raise AIRuntimeError("Intel GPU 未能初始化")
        inputs = [{"name": value.name, "shape": value.shape, "type": value.type} for value in session.get_inputs()]
        connection.send(("ready", {"inputs": inputs, "device": device_name}))
        phase = "推理"
        while True:
            command, outputs, inputs = connection.recv()
            if command != "run":
                break
            connection.send(("result", session.run(outputs, inputs)))
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            # Do not expose model paths, driver logs, or raw native exceptions.
            reason = str(exc) if isinstance(exc, AIRuntimeError) else f"Intel GPU {phase}失败"
            connection.send(("error", reason))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class _OpenVINOSession:
    def __init__(self, model):
        self._process = None
        self._connection = None
        self._lock = Lock()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._process = context.Process(target=_openvino_worker, args=(child, str(model)), daemon=True,
                                        name="intel-ai-runtime")
        try:
            self._process.start()
            child.close()
            kind, value = self._receive()
            if kind != "ready":
                raise AIRuntimeError(value or "Intel GPU 初始化或模型编译失败")
            self._inputs = [SimpleNamespace(**item) for item in value["inputs"]]
            self.device = value["device"]
        except BaseException:
            child.close()
            self.close()
            raise

    def _receive(self):
        if not self._connection.poll(120):
            raise AIRuntimeError("Intel GPU 初始化或推理超时")
        try:
            return self._connection.recv()
        except (EOFError, OSError):
            raise AIRuntimeError("Intel GPU 运行进程意外退出") from None

    def get_inputs(self):
        return self._inputs

    def get_providers(self):
        return [INTEL]

    def run(self, outputs, inputs):
        with self._lock:
            try:
                self._connection.send(("run", outputs, inputs))
                kind, value = self._receive()
                if kind != "result":
                    raise AIRuntimeError("Intel GPU 推理失败")
                return value
            except (EOFError, BrokenPipeError, OSError):
                raise AIRuntimeError("Intel GPU 运行进程意外退出") from None

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process is not None and self._process.pid:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=3)
        self._process = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class InferenceSession:
    """Small session facade, retaining the actual execution and fallback state."""
    def __init__(self, model: Path, requested: str):
        self.model = model
        self.requested = requested
        self._lock = Lock()
        self._session = None
        self._provider = None
        self._successful = False
        self._reason = None
        self._error = None
        self._snapshot = {}
        if requested not in MODES:
            raise AIRuntimeError("LIBRARY_AI_PROVIDER 应为 auto、cpu、intel 或 cuda")
        info = runtime_info()
        if not info["runtime_available"]:
            raise AIRuntimeError(info["runtime_error"])
        providers = info["available_providers"]
        if requested == "cuda" and CUDA not in providers:
            raise AIRuntimeError("CUDA 不可用；安装匹配的 CUDA 运行时或设置 LIBRARY_AI_PROVIDER=cpu")
        candidates = [CPU]
        if requested == "cuda":
            candidates = [CUDA]
        elif requested == "intel":
            if INTEL in providers:
                candidates.insert(0, INTEL)
            else:
                self._reason = "当前运行时不含 OpenVINO Intel GPU 支持，已回退 CPU"
        elif requested == "auto":
            candidates = [value for value in (CUDA, INTEL) if value in providers] + [CPU]
        for provider in candidates:
            try:
                self._session = _OpenVINOSession(model) if provider == INTEL else _ort_session(model, provider)
                self._provider = provider
                self._publish()
                return
            except Exception as exc:
                label = "Intel GPU" if provider == INTEL else "CUDA"
                if provider == CPU or requested == "cuda":
                    self._error = "CPU 模型载入失败，请检查模型与推理依赖" if provider == CPU else "CUDA 初始化失败；可切换 auto 或 cpu"
                    raise AIRuntimeError(self._error) from None
                reason = str(exc) if isinstance(exc, AIRuntimeError) else f"{label} 不可用或模型编译失败"
                self._reason = f"{reason}，已回退后备设备"
        raise AIRuntimeError("没有可用的本地推理设备")

    def _publish(self):
        self._snapshot = {"requested_provider": self.requested,
            "actual_provider": self._provider if self._successful else None,
            "device": (getattr(self._session, "device", "Intel GPU") if self._provider == INTEL else
                       {CPU: "CPU", CUDA: "NVIDIA CUDA"}.get(self._provider)) if self._successful else None,
            "fallback_reason": self._reason,
            "execution_state": "error" if self._error else ("not_loaded" if not self._successful else ("fallback" if self._reason else "ready"))}

    def status(self):
        # Publishing replaces the whole immutable snapshot, so status requests do
        # not block behind an in-progress GPU compile/inference.
        return dict(self._snapshot)

    def get_inputs(self):
        return self._session.get_inputs()

    def run(self, outputs, inputs):
        with self._lock:
            try:
                result = self._session.run(outputs, inputs)
            except Exception:
                if self.requested in {"cpu", "cuda"} or self._provider == CPU:
                    self._successful = False
                    self._error = "本地推理失败，请检查模型、内存和运行时；CUDA 模式不会自动切换设备" if self.requested == "cuda" else "CPU 推理失败，请检查模型、内存和运行时"
                    self._publish()
                    raise AIRuntimeError(self._error) from None
                label = "Intel GPU" if self._provider == INTEL else "CUDA"
                self._successful = False
                self._reason = f"{label} 推理失败或运行进程退出，已回退 CPU"
                if hasattr(self._session, "close"):
                    self._session.close()
                self._session = None
                try:
                    self._session = _ort_session(self.model, CPU)
                    self._provider = CPU
                    result = self._session.run(outputs, inputs)
                except Exception:
                    self._error = "加速设备和 CPU 推理均失败，请检查模型、内存和运行时"
                    self._publish()
                    raise AIRuntimeError(self._error) from None
            self._successful = True
            self._error = None
            self._publish()
            return result

    def close(self):
        if self._session is not None and hasattr(self._session, "close"):
            self._session.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
