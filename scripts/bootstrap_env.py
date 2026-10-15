"""Create private local deployment configuration without printing credentials."""
from pathlib import Path
import secrets

from configure_git import configure_git

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / ".git").exists():
    configure_git()
else:
    print("No Git checkout detected. Before adding this project to Git, run python scripts/configure_git.py.")
target = ROOT / "deploy" / ".env"
if target.exists():
    raise SystemExit("deploy/.env already exists; left unchanged.")
values = {
    "MYSQL_PASSWORD": secrets.token_hex(24),
    "MYSQL_ROOT_PASSWORD": secrets.token_hex(32),
    "LIBRARY_ADMIN_PASSWORD": secrets.token_urlsafe(24),
    "LIBRARY_SECRET_KEY": secrets.token_hex(32),
    "LIBRARY_PORT": "8800",
    "LIBRARY_DATA_PATH": "./data",
    "LIBRARY_IMPORT_PATH": "./imports",
    "LIBRARY_MODEL_PATH": "./models",
    "LIBRARY_ALLOWED_ORIGINS": "",
}
with target.open("x", encoding="utf-8", newline="\n") as stream:
    stream.write("# Private deployment configuration. Do not commit or share.\n")
    for key, value in values.items():
        stream.write(f"{key}={value}\n")
try:
    target.chmod(0o600)
except OSError:
    pass
for directory in ("data", "imports", "models"):
    (ROOT / directory).mkdir(exist_ok=True)
(ROOT / "data" / ".library-storage").write_text("qiye-library-v1\n", encoding="utf-8")
print("Created deploy/.env. Read LIBRARY_ADMIN_PASSWORD there to sign in.")
print("For NAS bind mounts, allow container UID 10001 to write the data directory.")
print("Start with: python scripts/compose.py up -d --build")
