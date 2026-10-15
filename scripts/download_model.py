"""Install a pinned WD tagger from its publisher with content verification.

This is an explicit setup command. The application never downloads weights at runtime.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "SmilingWolf/wd-swinv2-tagger-v3"
REVISION = "627aef95638667ddcaa3ac8ae625e88ea5b02f51"
FILES = {
    "model.onnx": {"size": 467460978, "sha256": "e6774bff34d43bd49f75a47db4ef217dce701c9847b546523eb85ff6dbba1db1"},
    "selected_tags.csv": {"size": 308468, "git_sha1": "a2a653a5c89c8e387765eb1de9cfd2aedc92ee8d"},
}


def verify(path, expected):
    if not path.exists() or path.stat().st_size != expected["size"]:
        return False
    sha = hashlib.sha256() if "sha256" in expected else hashlib.sha1()
    if "git_sha1" in expected:
        sha.update(f"blob {expected['size']}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest() == expected.get("sha256", expected.get("git_sha1"))


def clean_chunks(destination):
    chunks = destination.parent / (destination.name + ".part.chunks")
    if chunks.is_dir() and not chunks.is_symlink():
        for path in chunks.iterdir():
            if path.name.isdigit() and path.is_file() and not path.is_symlink():
                path.unlink()
        if not any(chunks.iterdir()):
            chunks.rmdir()


def download_large(url, temporary, expected, workers):
    """Bounded range requests survive slow/stalled CDN connections; verify final hash."""
    block_size = 4 * 1024**2
    size = expected["size"]
    count = (size + block_size - 1) // block_size
    chunks = temporary.parent / (temporary.name + ".chunks")
    chunks.mkdir(exist_ok=True)
    if temporary.exists():
        with temporary.open("rb") as previous:
            for index in range(temporary.stat().st_size // block_size):
                content = previous.read(block_size)
                chunk_path = chunks / str(index)
                if not chunk_path.exists():
                    chunk_path.write_bytes(content)

    def fetch_chunk(index):
        start = index * block_size
        end = min(start + block_size, size) - 1
        path = chunks / str(index)
        if path.exists() and path.stat().st_size == end - start + 1:
            return
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": "QiyeLibrary-ModelInstaller/0.1"})
                with urllib.request.urlopen(req, timeout=45) as response:
                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise RuntimeError("CDN did not honor the bounded range request")
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise RuntimeError("Incomplete range response")
                path.write_bytes(data)
                return
            except (OSError, RuntimeError):
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        completed = 0
        for future in as_completed([pool.submit(fetch_chunk, i) for i in range(count)]):
            future.result()
            completed += 1
            if completed % 16 == 0 or completed == count:
                print(f"  {completed / count:.0%}", flush=True)
    with temporary.open("wb") as stream:
        for index in range(count):
            stream.write((chunks / str(index)).read_bytes())
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "models" / "wd-tagger")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=6)
    options = parser.parse_args()
    directory = options.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for filename, expected in FILES.items():
        destination = directory / filename
        if verify(destination, expected):
            print(f"Verified existing {filename}", flush=True)
            clean_chunks(destination)
            continue
        temporary = directory / (filename + ".part")
        url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{filename}"
        if expected["size"] > 8 * 1024**2 and options.workers > 1:
            print(f"Downloading {filename} with {options.workers} bounded connections", flush=True)
            download_large(url, temporary, expected, options.workers)
            if not verify(temporary, expected):
                raise RuntimeError(f"Checksum mismatch for {filename}; model was not activated")
            os.replace(temporary, destination)
            clean_chunks(destination)
            continue
        offset = temporary.stat().st_size if temporary.exists() else 0
        if offset > expected["size"]:
            offset = 0
        headers = {"User-Agent": "QiyeLibrary-ModelInstaller/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        print(f"Downloading {filename} ({expected['size'] / 1024**2:.1f} MiB)", flush=True)
        with urllib.request.urlopen(request, timeout=120) as response:
            if offset and response.status != 206:
                offset = 0
            if offset and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                raise RuntimeError("Unexpected resumed response; partial file left untouched")
            total = offset
            last_report = total
            with temporary.open("ab" if offset else "wb") as stream:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > expected["size"]:
                        raise RuntimeError("Downloaded content exceeds the pinned file size")
                    stream.write(chunk)
                    if total - last_report >= 64 * 1024**2:
                        print(f"  {total / expected['size']:.0%}", flush=True)
                        last_report = total
                stream.flush()
                os.fsync(stream.fileno())
        if not verify(temporary, expected):
            raise RuntimeError(f"Checksum mismatch for {filename}; model was not activated")
        os.replace(temporary, destination)
    manifest = {"repository": REPOSITORY, "revision": REVISION, "license": "Apache-2.0", "files": FILES}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Model verified and installed: {directory}", flush=True)


if __name__ == "__main__":
    main()
