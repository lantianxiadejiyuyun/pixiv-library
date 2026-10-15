#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
if [ "${1:-}" = "--install" ]; then
  .venv/bin/python -m pip install -r server/requirements.txt
  .venv/bin/python scripts/install_mihomo.py
  (cd web && npm ci && npm run build)
fi
mkdir -p .runtime data imports models
export LIBRARY_DATABASE_URL="${LIBRARY_DATABASE_URL:-sqlite:///$(pwd)/.runtime/library.db}"
export LIBRARY_STORAGE_ROOT="${LIBRARY_STORAGE_ROOT:-$(pwd)/data}"
export LIBRARY_IMPORT_ROOT="${LIBRARY_IMPORT_ROOT:-$(pwd)/imports}"
export LIBRARY_MODEL_DIR="${LIBRARY_MODEL_DIR:-$(pwd)/models}"
export LIBRARY_MIHOMO_PATH="${LIBRARY_MIHOMO_PATH:-$(pwd)/.runtime/mihomo/mihomo}"
export LIBRARY_MIHOMO_DIR="${LIBRARY_MIHOMO_DIR:-$(pwd)/.runtime/mihomo-state}"
.venv/bin/python scripts/migrate_dev.py
echo 'Local development: http://127.0.0.1:8000 (SQLite).'
echo 'If LIBRARY_ADMIN_PASSWORD is unset, local development password is local-library.'
exec .venv/bin/python -m uvicorn app.main:app --app-dir server --host 127.0.0.1 --port 8000
