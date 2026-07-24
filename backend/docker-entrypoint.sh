#!/bin/sh
# Container startup: migrate, seed (idempotent), then hand off to the CMD
# (uvicorn) via exec so it becomes PID 1 and receives signals directly.
set -e

alembic upgrade head

python -c "
from app.db.seed import seed
from app.db.session import SessionLocal
import app.models  # noqa: F401  (registers every table on Base.metadata)

db = SessionLocal()
try:
    counts = seed(db)
    print('seed:', counts)
finally:
    db.close()
"

exec "$@"
