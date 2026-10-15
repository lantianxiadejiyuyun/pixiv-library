"""Run before the development server; production uses the container entrypoint."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.dev_migrations import upgrade_development_database


if __name__ == "__main__":
    result = upgrade_development_database()
    print(f"Database schema ready: {result['revision']}")
    if result["backup"]:
        print(f"Pre-migration SQLite backup: {result['backup']}")
