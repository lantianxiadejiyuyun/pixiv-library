"""Configure checkout-local ignore/attribute files; never print private settings."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def configure_git(root=ROOT):
    root = Path(root).resolve()
    files = {"core.excludesFile": root / "config" / "git" / "ignore",
             "core.attributesFile": root / "config" / "git" / "attributes"}
    if not all(path.is_file() for path in files.values()):
        raise RuntimeError("Missing config/git/ignore or config/git/attributes")
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("Run git init or clone this project, then run scripts/configure_git.py before creating private configuration")
    if Path(result.stdout.strip()).resolve() != root:
        raise RuntimeError("Refusing to configure a parent repository; initialize this project as its own Git checkout")
    for key, path in files.items():
        subprocess.run(["git", "-C", str(root), "config", "--local", key, str(path)], check=True)
    # This also protects runtime credentials/build products after a fresh clone.
    for protected in ("deploy/.env", ".runtime/private-probe", "data/private-probe", "models/private-probe", "imports/private-probe"):
        result = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--no-index", protected])
        if result.returncode != 0:
            raise RuntimeError("Private deployment/runtime files are not ignored; no credentials were generated")


if __name__ == "__main__":
    configure_git()
    print("Configured checkout-local Git ignore and attributes.")
