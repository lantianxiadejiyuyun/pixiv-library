#!/usr/bin/env python3
"""Install pinned Intel OpenCL userspace drivers in the opt-in amd64 image.

Official Ubuntu 24.04 packages also load on our Debian 13 image. No Ubuntu APT
repository is added: Debian resolves system dependencies from its own sources.
The release's matching IGC, ocloc and GMM versions must be updated together.
Package SHA-256 values are published at:
https://github.com/intel/compute-runtime/releases/tag/25.35.35096.9
https://github.com/intel/intel-graphics-compiler/releases/tag/v2.18.5

Only userspace drivers are installed. A compatible host kernel driver and a
passed-through, accessible /dev/dri render device are still required for GPU.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import urllib.request


VERSION = "25.35.35096.9"
IGC_VERSION = "2.18.5+19820"
GMM_VERSION = "22.8.1"
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
COMPUTE_RELEASE = f"https://github.com/intel/compute-runtime/releases/download/{VERSION}/"
IGC_RELEASE = "https://github.com/intel/intel-graphics-compiler/releases/download/v2.18.5/"
PACKAGES = (
    (IGC_RELEASE, f"intel-igc-core-2_{IGC_VERSION}_amd64.deb", "7c340f45c4f294eca3a9780a1b3ece6d4b699fdc51e7dc9ff8c5d6323c5d8d64"),
    (IGC_RELEASE, f"intel-igc-opencl-2_{IGC_VERSION}_amd64.deb", "bcb1435486175673f14b180efb8fc39ff31b76c625e09b1253a07a705efcf9c3"),
    (COMPUTE_RELEASE, f"intel-ocloc_{VERSION}-0_amd64.deb", "023ccf8fd8912958bb3f00977056c2fa8ddbcef8ecb4bbc72bddf9ecb0eec6fb"),
    (COMPUTE_RELEASE, f"intel-opencl-icd_{VERSION}-0_amd64.deb", "89f252553c937b2456ee9919858ff2ab65dc6b0dcbd219c73953548465adf325"),
    (COMPUTE_RELEASE, f"libigdgmm12_{GMM_VERSION}_amd64.deb", "810114ef05f4d44c5c86fbc2f0b34688102f41596a40772a1ff7a48f5df69ecd"),
)
MANIFEST_PATH = Path("/usr/local/share/qiye-library/intel-opencl.json")


def download_package(directory, release_url, filename, expected_sha256):
    """Stream one fixed official asset, publishing it only after SHA verification."""
    directory = Path(directory)
    target = directory / filename
    request = urllib.request.Request(release_url + filename, headers={
        "User-Agent": "Qiye-Library-Intel-OpenCL-Installer",
    })
    digest = hashlib.sha256()
    size = 0
    # This private directory exists only for this installation. Files are never
    # installed if even one of the five official package hashes does not match.
    partial = target.with_suffix(".part")
    try:
        with urllib.request.urlopen(request, timeout=120) as source, partial.open("wb") as output:
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > MAX_PACKAGE_BYTES:
                    raise ValueError("Intel package exceeds the download size limit")
                digest.update(block)
                output.write(block)
        if not size or digest.hexdigest() != expected_sha256:
            raise ValueError("Official Intel package SHA-256 mismatch; installation aborted")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def validate_platform():
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("Intel OpenCL image requires Linux amd64")
    if os.geteuid() != 0:
        raise ValueError("Intel driver installation runs as root during image build only")


def verify_loader():
    """Check actual shared-library loading without requiring build-time GPU access."""
    icd_files = list(Path("/etc/OpenCL/vendors").glob("*intel*.icd"))
    if len(icd_files) != 1:
        raise ValueError("Intel OpenCL ICD registration is missing or ambiguous")
    library = icd_files[0].read_text(encoding="utf-8").strip()
    # Explicit RTLD_NOW detects missing linked dependencies even without a GPU.
    driver = ctypes.CDLL(library, mode=os.RTLD_NOW)
    loader = ctypes.CDLL("libOpenCL.so.1", mode=os.RTLD_NOW)
    loader.clGetPlatformIDs.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    loader.clGetPlatformIDs.restype = ctypes.c_int
    count = ctypes.c_uint()
    status = loader.clGetPlatformIDs(0, None, ctypes.byref(count))
    if status not in (0, -1001):  # CL_PLATFORM_NOT_FOUND_KHR is expected without /dev/dri.
        raise ValueError("OpenCL loader returned an unexpected platform enumeration error")
    # Keep both CDLL objects alive until enumeration has finished.
    return {"driver_library": driver._name, "icd": str(icd_files[0]),
            "platform_count_at_build": count.value, "loader_status": status}


def install():
    validate_platform()
    with tempfile.TemporaryDirectory(prefix="qiye-intel-opencl-") as temporary:
        packages = [download_package(temporary, *package) for package in PACKAGES]
        for package in packages:
            arch = subprocess.check_output(["dpkg-deb", "-f", str(package), "Architecture"], text=True).strip()
            if arch != "amd64":
                raise ValueError("Unexpected Intel package architecture")
        subprocess.run(["apt-get", "update"], check=True)
        subprocess.run(["apt-get", "install", "-y", "--no-install-recommends",
                        "ocl-icd-libopencl1", "libgomp1", *map(str, packages)],
                       check=True, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
    subprocess.run(["ldconfig"], check=True)
    loader = verify_loader()
    manifest = {"compute_runtime": VERSION, "igc": IGC_VERSION, "gmm": GMM_VERSION,
                "architecture": "amd64", "validation": loader,
                "packages": [{"url": url + filename, "sha256": digest} for url, filename, digest in PACKAGES]}
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Intel OpenCL {VERSION}: shared libraries and ICD loader verified; "
          f"visible platforms={loader['platform_count_at_build']}. GPU inference needs a render device.", flush=True)


if __name__ == "__main__":
    try:
        install()
    except Exception as error:
        # Do not echo proxy URLs, environment credentials or URL-bearing errors.
        raise SystemExit(f"Intel OpenCL installation failed ({type(error).__name__}); "
                         "check official release access, package hashes and APT dependencies") from None
