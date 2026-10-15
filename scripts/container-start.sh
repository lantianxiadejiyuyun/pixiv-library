#!/usr/bin/env sh
set -eu
python -m alembic -c /app/server/alembic.ini upgrade head
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
