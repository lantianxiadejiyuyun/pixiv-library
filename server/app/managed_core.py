"""Own one loopback-only Mihomo child process; never fetch binaries at runtime.

Only normalized outbound nodes enter this module, never a subscription profile.
JSON is valid YAML. Generated files contain no providers, scripts or GEO rules.
Controller API: github.com/MetaCubeX/mihomo/blob/v1.19.32/hub/route/configs.go
"""
import atexit
from collections import deque
import copy
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

import httpx

VERSION = "v1.19.32"
GROUP = "QIYE"
NODE_TYPES = {"ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "socks5", "http", "anytls", "snell"}
RESERVED = {GROUP, "DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ManagedCoreError(ValueError):
    """Safe public error: never include core output or subscription credentials."""


class ManagedCoreBusy(ManagedCoreError):
    """No probe ran; callers must not overwrite a previous latency as failed."""


def _process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _child_environment():
    # Database passwords, app keys, and ambient proxy settings are not inherited.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TZ"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class ManagedCore:
    def __init__(self, binary_path=None, state_dir=None, proxy_port=None, controller_port=None):
        filename = "mihomo.exe" if os.name == "nt" else "mihomo"
        self.binary_path = Path(binary_path or os.getenv("LIBRARY_MIHOMO_PATH", str(PROJECT_ROOT / ".runtime/mihomo" / filename))).resolve()
        self.state_dir = Path(state_dir or os.getenv("LIBRARY_MIHOMO_DIR", str(PROJECT_ROOT / ".runtime/mihomo-state"))).resolve()
        self.proxy_port = int(proxy_port or os.getenv("LIBRARY_MIHOMO_PROXY_PORT", "17890"))
        self.controller_port = int(controller_port or os.getenv("LIBRARY_MIHOMO_CONTROLLER_PORT", "19090"))
        if self.proxy_port == self.controller_port or not all(1024 <= p <= 65535 for p in (self.proxy_port, self.controller_port)):
            raise ManagedCoreError("内核端口必须是两个不同的 1024–65535 端口")
        self._secret = secrets.token_urlsafe(48)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._supervisor = None
        self._process = None
        self._desired = False
        self._config = None
        self._nodes = []
        self._selected = "REJECT"
        self._error = None
        self._version = None
        self._restarts = deque()
        self._restart_count = 0
        self._next_restart = 0.0
        self._directory_ready = False
        self._delay_slots = threading.BoundedSemaphore(6)

    @property
    def proxy_url(self):
        # Always return the local endpoint even when stopped: no direct fallback.
        return f"http://127.0.0.1:{self.proxy_port}"

    @property
    def config_path(self):
        return self.state_dir / "config.json"

    def _running(self):
        return self._process is not None and self._process.poll() is None

    def status(self):
        with self._lock:
            installed = self.binary_path.is_file()
            running = self._running()
            return {"installed": installed, "running": running, "version": self._version,
                    "state": "running" if running else ("missing" if not installed else ("error" if self._error else "stopped")),
                    "error": self._error if installed else "内置代理尚未安装；运行 scripts/install_mihomo.py 或重新构建应用镜像",
                    "node_count": len(self._nodes), "selected": self._selected,
                    "proxy_url": self.proxy_url, "restart_count": self._restart_count}

    def _prepare_directory(self):
        if self._directory_ready:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        if os.name == "nt":
            # chmod cannot set Windows ACLs. Limit the directory to this account
            # and SYSTEM; files inherit these rights, including core cache files.
            result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=5, **_process_options())
            match = re.search(r"S-1-5-[0-9-]+", result.stdout)
            if result.returncode or not match:
                raise ManagedCoreError("无法获取当前账户权限，不能保存代理凭据")
            grants = [f"*{match.group(0)}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"]
            result = subprocess.run(["icacls", str(self.state_dir), "/inheritance:r", "/grant:r", *grants], capture_output=True, timeout=5, **_process_options())
            if result.returncode:
                raise ManagedCoreError("无法设置内置代理目录权限，请使用当前用户可写的本地目录")
        self._directory_ready = True

    def _build_config(self, nodes, selected):
        if not isinstance(nodes, list) or len(nodes) > 2000:
            raise ManagedCoreError("代理节点列表无效或超过 2000 个节点")
        names = set()
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") not in NODE_TYPES:
                raise ManagedCoreError("节点包含不支持的代理协议")
            name = node.get("name")
            if not isinstance(name, str) or not name or len(name) > 160 or any(ord(c) < 32 for c in name) or name in RESERVED or name in names:
                raise ManagedCoreError("节点名称无效、重复或使用了保留名称")
            if any(k in node for k in ("dialer-proxy", "interface-name", "routing-mark", "certificate", "private-key", "ca", "ca-str")):
                raise ManagedCoreError("节点包含不允许的本地文件或路由设置")
            names.add(name)
        try:
            # Detach caller-owned objects and refuse non-JSON values/NaN.
            serialized = json.dumps(nodes, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > 4 * 1024 * 1024:
                raise ValueError()
            normalized = json.loads(serialized)
        except (TypeError, ValueError, RecursionError):
            raise ManagedCoreError("节点配置过大或格式无效") from None
        selection = selected if isinstance(selected, str) and selected in names else "REJECT"
        return {
            "mixed-port": self.proxy_port, "allow-lan": False, "bind-address": "127.0.0.1",
            "mode": "rule", "log-level": "silent", "ipv6": False,
            "find-process-mode": "off", "external-controller": f"127.0.0.1:{self.controller_port}",
            "secret": self._secret, "external-controller-cors": {"allow-origins": [], "allow-private-network": False},
            "profile": {"store-selected": False, "store-fake-ip": False},
            "geo-auto-update": False, "tun": {"enable": False}, "dns": {"enable": False},
            "proxies": normalized,
            "proxy-groups": [{"name": GROUP, "type": "select", "proxies": ["REJECT"] + [n["name"] for n in normalized], "default-selected": selection}],
            "rules": ["MATCH," + GROUP],
        }, selection

    def _write_candidate(self, config):
        self._prepare_directory()
        descriptor, name = tempfile.mkstemp(prefix="candidate-", suffix=".json", dir=self.state_dir)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o600)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _validate(self, path):
        if not self.binary_path.is_file():
            raise ManagedCoreError("内置代理尚未安装，请运行 scripts/install_mihomo.py 或重新构建镜像")
        try:
            result = subprocess.run([str(self.binary_path), "-t", "-d", str(self.state_dir), "-f", str(path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                                    env=_child_environment(), **_process_options())
        except (OSError, subprocess.TimeoutExpired):
            raise ManagedCoreError("内核配置验证未完成，请检查内核安装和文件权限") from None
        if result.returncode != 0:
            raise ManagedCoreError("内核拒绝节点配置；请检查订阅中的协议参数，原配置已保留")

    def _request(self, method, path, *, body=None, params=None, timeout=5):
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{self.controller_port}", trust_env=False,
                              timeout=timeout, follow_redirects=False,
                              headers={"Authorization": "Bearer " + self._secret}) as client:
                with client.stream(method, path, json=body, params=params) as response:
                    if not 200 <= response.status_code < 300:
                        raise ManagedCoreError("内置代理控制器未完成操作")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 2 * 1024 * 1024:
                            raise ManagedCoreError("内置代理控制器响应过大")
            return json.loads(data) if data else {}
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, ManagedCoreError):
                raise
            raise ManagedCoreError("无法连接内置代理控制器，请检查内核运行状态") from None

    def _terminate(self):
        process = self._process
        self._process = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
            else:
                process.wait(timeout=1)

    def _launch(self):
        self._terminate()
        # A core can stay alive after a listener bind failure; fail before launch.
        try:
            for port in (self.proxy_port, self.controller_port):
                with socket.socket() as listener:
                    if os.name == "nt":
                        # Windows SO_REUSEADDR can steal a live listener. Keep
                        # the preflight exclusive on this platform.
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    else:
                        # Match Go's POSIX listener semantics: prior accepted
                        # connections in TIME_WAIT do not mean a live listener
                        # owns this port. Do not set SO_REUSEPORT.
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(("127.0.0.1", port))
            self._process = subprocess.Popen([str(self.binary_path), "-d", str(self.state_dir), "-f", str(self.config_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=_child_environment(), cwd=self.state_dir, **_process_options())
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and self._running():
                try:
                    version = self._request("GET", "/version", timeout=0.5)
                    self._version = str(version.get("version", ""))[:64]
                    with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=0.5):
                        pass
                    return
                except (ManagedCoreError, OSError):
                    time.sleep(0.1)
        except OSError:
            self._terminate()
            raise ManagedCoreError("内核无法启动，请检查内置端口是否被占用、二进制架构及权限") from None
        self._terminate()
        raise ManagedCoreError("内核启动超时或已退出，请检查安装和节点配置")

    def _activate(self, selected):
        if self._running():
            self._request("PUT", "/configs", params={"force": "true"}, body={"path": str(self.config_path)})
        else:
            self._launch()
        # Reload may preserve an earlier selector choice. Explicitly set it.
        self._request("PUT", "/proxies/" + GROUP, body={"name": selected})

    def apply_nodes(self, nodes, selected=None):
        with self._lock:
            config, selection = self._build_config(nodes, selected)
            candidate = self._write_candidate(config)
            try:
                self._validate(candidate)
            except Exception:
                candidate.unlink(missing_ok=True)
                raise
            previous = copy.deepcopy(self._config)
            previous_selection = self._selected
            try:
                os.replace(candidate, self.config_path)
                self._activate(selection)
            except (OSError, ManagedCoreError) as exc:
                self._error = str(exc) if isinstance(exc, ManagedCoreError) else "无法写入内核配置，请检查目录权限"
                # Restore both disk and running process. If rollback itself fails,
                # stop the child so uncommitted nodes cannot continue serving.
                if previous is not None:
                    try:
                        restored = self._write_candidate(previous)
                        os.replace(restored, self.config_path)
                        self._activate(previous_selection)
                    except (OSError, ManagedCoreError):
                        self._terminate()
                        self._desired = False
                        self._error = "配置回滚未完成，内核已停止；请检查目录权限后重新应用订阅"
                else:
                    self._terminate()
                    self.config_path.unlink(missing_ok=True)
                raise ManagedCoreError(self._error) from None
            finally:
                candidate.unlink(missing_ok=True)
            self._config = config
            self._nodes = config["proxies"]
            self._selected = selection
            self._error = None
            self._desired = True
            self._restarts.clear()
            self._next_restart = 0
            self._ensure_supervisor()
            return self.status()

    def start(self):
        with self._lock:
            if self._running():
                return self.status()
            # Database owns saved nodes. Never execute arbitrary old disk profiles.
            return self.apply_nodes(copy.deepcopy(self._nodes), self._selected)

    def _ensure_supervisor(self):
        if self._supervisor is None or not self._supervisor.is_alive():
            self._stop_event.clear()
            self._supervisor = threading.Thread(target=self._supervise, daemon=True, name="mihomo-supervisor")
            self._supervisor.start()

    def _restart_if_needed(self):
        with self._lock:
            if not self._desired or self._running() or self._config is None:
                return
            now = time.monotonic()
            while self._restarts and self._restarts[0] < now - 300:
                self._restarts.popleft()
            if len(self._restarts) >= 3:
                self._desired = False
                self._error = "内核在 5 分钟内连续退出，自动重启已暂停；修正配置后可重新启用"
                return
            if now < self._next_restart:
                return
            self._restarts.append(now)
            self._restart_count += 1
            self._next_restart = now + 2 ** len(self._restarts)
            try:
                self._launch()
                self._request("PUT", "/proxies/" + GROUP, body={"name": self._selected})
                self._error = None
            except ManagedCoreError:
                self._terminate()
                self._error = "内核意外退出，正在有限次数重试"

    def _supervise(self):
        while not self._stop_event.wait(1):
            self._restart_if_needed()

    def stop(self):
        self._stop_event.set()
        with self._lock:
            self._desired = False
            self._terminate()
            thread = self._supervisor
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self._lock:
            self._supervisor = None

    def select(self, name):
        with self._lock:
            if name != "REJECT" and name not in {node["name"] for node in self._nodes}:
                raise ManagedCoreError("节点不存在，请刷新订阅节点列表")
            # Uses the same validation/rollback path, also persists restart choice.
            return self.apply_nodes(copy.deepcopy(self._nodes), name)

    def delay(self, name, *, wait_for_slot=False, cancelled=None):
        acquired = self._delay_slots.acquire(blocking=False)
        if wait_for_slot and not acquired:
            deadline = time.monotonic() + 16
            while not acquired and time.monotonic() < deadline:
                if cancelled is not None and cancelled.is_set():
                    raise ManagedCoreBusy("测速已取消")
                acquired = self._delay_slots.acquire(timeout=0.1)
        if not acquired:
            raise ManagedCoreBusy("测速队列繁忙，本节点尚未测试，请稍后重试")
        try:
            if cancelled is not None and cancelled.is_set():
                raise ManagedCoreBusy("测速已取消")
            with self._lock:
                if name not in {node["name"] for node in self._nodes}:
                    raise ManagedCoreError("节点不存在，请刷新订阅节点列表")
                if not self._running():
                    raise ManagedCoreError("内核尚未运行，无法测速")
            # The fixed-URL controller request never changes the selector. Do
            # not hold the configuration lock for its network timeout: status,
            # selection and other bounded probes must remain responsive.
            result = self._request("GET", "/proxies/" + quote(name, safe="") + "/delay", timeout=8,
                params={"timeout": 5000, "url": "https://www.gstatic.com/generate_204", "expected": "204"})
            delay = result.get("delay")
            if isinstance(delay, bool) or not isinstance(delay, (float, int)) or not 0 <= delay <= 60000:
                raise ManagedCoreError("节点测速失败或超时")
            return int(delay)
        finally:
            self._delay_slots.release()


core = ManagedCore()
atexit.register(core.stop)
