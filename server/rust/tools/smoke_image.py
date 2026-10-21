"""Validate a built native image using only a new disposable local library.

Never loads deploy/.env, production volumes, accounts or an existing database.
"""
import argparse
import http.cookiejar
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="qiye-library:rust-validation")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    temporary = root / ".runtime" / "rust-validation"
    temporary.mkdir(parents=True, exist_ok=True)
    name = "qiye-rust-image-smoke-" + uuid.uuid4().hex[:12]

    def docker(*command, check=True):
        result = subprocess.run(["docker", *command], capture_output=True, text=True, timeout=90)
        if check and result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        return result

    with tempfile.TemporaryDirectory(prefix="image-", dir=temporary) as target:
        target = Path(target).resolve()
        assert target.parent == temporary.resolve() and target.name.startswith("image-")
        storage = target / "media"
        storage.mkdir()
        marker = storage / ".library-storage"
        marker.write_text("qiye-library-v1\n", encoding="utf-8")
        (target / "library.db").touch()
        environment = {
            "LIBRARY_DATABASE_URL": "sqlite:///fixture/library.db",
            "LIBRARY_STORAGE_ROOT": "/fixture/media",
            "LIBRARY_IMPORT_ROOT": "/fixture/import",
            "LIBRARY_ADMIN_PASSWORD": "image-test-only",
            "LIBRARY_SECRET_KEY": "isolated-native-image-validation-secret",
            "LIBRARY_RUST_BIND": "0.0.0.0:8000",
            "LIBRARY_STORAGE_REQUIRE_MARKER": "true",
        }
        env = [value for key, value in environment.items() for value in ("-e", key + "=" + value)]
        mount = ["--mount", f"type=bind,source={target},target=/fixture"]
        docker("run", "--rm", *mount, *env, "-e", "LIBRARY_RUST_READ_ONLY=false", args.image, "migrate")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({}))

        def request(path, body=None, method=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(origin + path, data=data, method=method,
                                         headers={} if data is None else {"Content-Type": "application/json"})
            try:
                with opener.open(req, timeout=10) as response:
                    return response.status, response.headers, response.read()
            except urllib.error.HTTPError as response:
                return response.code, response.headers, response.read()

        def wait(path, condition):
            for _ in range(100):
                try:
                    status, headers, raw = request(path)
                    if status == 200 and condition(json.loads(raw)):
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(.2)
            raise RuntimeError("Native image did not reach the expected state: " + path)

        def launch(writable):
            docker("run", "-d", "--init", "--name", name, "--label", "qiye.purpose=rust-validation",
                   "--cpus", "2", "--memory", "1g", "-p", f"127.0.0.1:{port}:8000", *mount, *env,
                   "-e", f"LIBRARY_RUST_READ_ONLY={str(not writable).lower()}",
                   "-e", f"LIBRARY_RUST_WORKERS_ENABLED={str(writable).lower()}", args.image)
            wait("/api/health", lambda v: v["status"] == "ok")
            assert request("/api/auth/login", {"password": "image-test-only"})[0] == 200

        try:
            launch(False)
            status, _, raw = request("/api/rust/migration")
            migration = json.loads(raw)
            assert status == 200 and migration["legacy_route_count"] == 152 and migration["remaining"] == []
            assert migration["read_only"] and migration["workers_migrated"]
            assert request("/")[0] == 200
            status, _, raw = request("/api/ai/nodes/deployment")
            assert status == 200
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                assert "deploy/compose.ai-node.yaml" in archive.namelist()
                assert "README.md" in archive.namelist()
                assert all(not name.startswith("/") and ".." not in Path(name).parts
                           and not name.endswith("/.env") for name in archive.namelist())
            assert request("/api/queue/settings", {"paused": True}, "PUT")[0] == 409
            docker("stop", "--time", "15", name)
            docker("rm", name)
            launch(True)
            wait("/api/queue/status", lambda v: len(v["runtime"]["workers"]) == 8
                 and all(w["running"] for w in v["runtime"]["workers"]))
            wait("/api/server/performance", lambda v: v["status"] == "ready")
            assert request("/api/network/speed/download?bytes=1024")[0] == 200
            marker.unlink()
            assert request("/api/health")[0] == 503
            assert request("/api/items")[0] == 200
            marker.write_text("qiye-library-v1\n", encoding="utf-8")
            assert request("/api/health")[0] == 200
            docker("stop", "--time", "15", name)
            print("Native Docker image passed: initialization, Web, AI bundle, read-only mode, eight workers, telemetry, speed, NAS degradation and shutdown.")
        finally:
            docker("rm", "--force", name, check=False)


if __name__ == "__main__":
    main()
