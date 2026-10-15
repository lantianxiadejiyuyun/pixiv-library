#!/usr/bin/env python3
"""Install the pinned official Mihomo core. Never called by app startup.

Archive digests are pinned from GitHub's official release asset metadata:
https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/v1.19.32
Release/source (GPL-3.0): https://github.com/MetaCubeX/mihomo/tree/v1.19.32
The amd64 v1 builds also run on older x86-64 NAS/PC CPUs.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import tempfile
import urllib.request
import zipfile

VERSION = "v1.19.32"
RELEASE = f"https://github.com/MetaCubeX/mihomo/releases/download/{VERSION}/"
ARTIFACTS = {
    "linux-amd64": ("mihomo-linux-amd64-v1-v1.19.32.gz", "306f81e723e60ce6b828899a6fe83e1d00e9ecefb2dc8d4d849312a5bc00efdc"),
    "linux-arm64": ("mihomo-linux-arm64-v1.19.32.gz", "9dd862e28b46ff7d775f169cceebc28deccaa0a9e804237d421cd2571e0caba0"),
    "windows-amd64": ("mihomo-windows-amd64-v1-v1.19.32.zip", "00549b347d8e124600e46cac67d59293358454f465e9d86d2511567da56b7029"),
}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_BINARY = 160 * 1024 * 1024


def current_platform():
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    target = f"{system}-{arch}"
    if target not in ARTIFACTS:
        raise ValueError("Bundled Mihomo supports Windows amd64 and Linux amd64/arm64 only")
    return target


def unpack_verified(archive, filename, expected_digest):
    if len(archive) > MAX_ARCHIVE or hashlib.sha256(archive).hexdigest() != expected_digest:
        raise ValueError("Official Mihomo archive SHA-256 mismatch; nothing was installed")
    if filename.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(archive)) as source:
            binary = source.read(MAX_BINARY + 1)
    else:
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            files = [info for info in source.infolist() if not info.is_dir()]
            # No extractall: even a traversal entry cannot write outside our target.
            if len(files) != 1 or not files[0].filename.endswith(".exe") or files[0].file_size > MAX_BINARY:
                raise ValueError("Unexpected official Mihomo archive layout")
            with source.open(files[0]) as member:
                binary = member.read(MAX_BINARY + 1)
    if not binary or len(binary) > MAX_BINARY:
        raise ValueError("Invalid Mihomo binary size")
    return binary


def install(target, destination, *, archive_path=None):
    filename, digest = ARTIFACTS[target]
    destination = Path(destination).resolve()
    manifest_path = destination.with_name(destination.name + ".json")
    if destination.is_file() and manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (manifest.get("archive_sha256") == digest and manifest.get("target") == target
                    and manifest.get("binary_sha256") == hashlib.sha256(destination.read_bytes()).hexdigest()):
                return destination
        except (OSError, ValueError):
            pass
    if archive_path:
        with Path(archive_path).open("rb") as stream:
            archive = stream.read(MAX_ARCHIVE + 1)
    else:
        request = urllib.request.Request(RELEASE + filename, headers={"User-Agent": "Qiye-Library-Mihomo-Installer"})
        with urllib.request.urlopen(request, timeout=90) as stream:
            archive = stream.read(MAX_ARCHIVE + 1)
    binary = unpack_verified(archive, filename, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".mihomo-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(binary)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    manifest_path.write_text(json.dumps({"version": VERSION, "target": target, "url": RELEASE + filename,
        "archive_sha256": digest, "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "license": "GPL-3.0", "source": "https://github.com/MetaCubeX/mihomo/tree/" + VERSION}, indent=2), encoding="utf-8")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=ARTIFACTS, dest="target")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--archive", type=Path, help="Use a previously downloaded official archive (same pinned SHA-256 verification)")
    args = parser.parse_args()
    target = args.target or current_platform()
    destination = args.output or Path(__file__).resolve().parents[1] / ".runtime" / "mihomo" / ("mihomo.exe" if target.startswith("windows") else "mihomo")
    try:
        installed = install(target, destination, archive_path=args.archive)
    except Exception as exc:
        # Installer can use shell proxies, which may contain authentication.
        raise SystemExit(f"Mihomo installation failed ({type(exc).__name__}); verify network/archive and retry") from None
    print(f"Installed verified Mihomo {VERSION}: {installed}")


if __name__ == "__main__":
    main()
