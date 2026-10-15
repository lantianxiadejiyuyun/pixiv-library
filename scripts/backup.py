"""Back up the Compose MySQL database and immutable library files, or verify a backup.

No files are deleted. Keep deploy/.env (especially LIBRARY_SECRET_KEY) separately.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compose import compose_command


def configuration():
    config = {}
    if (ROOT / "deploy" / ".env").exists():
        for line in (ROOT / "deploy" / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    return config


def verify(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for entry in manifest["files"]:
            with archive.open(entry["name"]) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != entry["sha256"] or archive.getinfo(entry["name"]).file_size != entry["size"]:
                raise RuntimeError(f"Integrity check failed: {entry['name']}")
    print(f"Verified {len(manifest['files'])} files in {archive_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        verify(args.verify)
        return
    if not args.output:
        parser.error("--output is required when creating a backup")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("Output already exists; left unchanged.")
    storage = (args.storage or Path(configuration().get("LIBRARY_DATA_PATH", "./data")))
    storage = (ROOT / storage).resolve() if not storage.is_absolute() else storage.resolve()
    if not (storage / ".library-storage").is_file():
        raise SystemExit("Storage marker missing. Check --storage or the NAS mount.")
    if output.is_relative_to(storage):
        raise SystemExit("Backup destination must be outside the library storage directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    if partial.exists():
        raise SystemExit("A partial backup already exists; choose a new output name.")
    with tempfile.TemporaryDirectory(prefix="qiye-backup-") as temporary:
        dump = Path(temporary) / "database.sql"
        # Dump first: immutable committed blobs are never removed by this version,
        # so subsequent file copying includes every referenced committed asset.
        command = compose_command("exec", "-T", "db", "sh", "-c",
                   'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u library --single-transaction --no-tablespaces --skip-add-drop-table --skip-comments library', root=ROOT)
        with dump.open("wb") as stream:
            subprocess.run(command, cwd=ROOT, stdout=stream, check=True)
        manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "files": []}
        sources = [(dump, "database.sql"), (storage / ".library-storage", "storage/.library-storage")]
        # Include retained pre-collection blobs and every source's independent
        # namespace. A migration never removes originals referenced by this dump.
        blob_paths = list((storage / "blobs").rglob("*")) + list((storage / "collections").rglob("*"))
        for path in sorted(blob_paths):
            if path.is_file() and not path.name.startswith("."):
                if not path.resolve().is_relative_to(storage):
                    raise RuntimeError("Storage contains a symlink outside its root")
                sources.append((path, "storage/" + path.relative_to(storage).as_posix()))
        with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path, name in sources:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                archive.write(path, name)
                manifest["files"].append({"name": name, "size": path.stat().st_size, "sha256": digest})
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        verify(partial)
        partial.rename(output)
    print(f"Saved backup: {output}")
    print("Keep deploy/.env separately: its encryption key is needed to recover saved source accounts.")


if __name__ == "__main__":
    main()
