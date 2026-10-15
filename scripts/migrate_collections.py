"""Check source collection migration; pass --apply to copy and commit paths."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.collection_migration import main

if __name__ == "__main__":
    raise SystemExit(main())
