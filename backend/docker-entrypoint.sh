#!/bin/sh
# Container startup: migrate and seed by default, then hand off to the CMD via
# exec so it becomes PID 1 and receives signals directly. Kubernetes runs
# migrations in a separate Job and sets SKIP_STARTUP_MIGRATIONS=true on the
# long-running backend Deployment.
set -e

if [ "${SKIP_STARTUP_MIGRATIONS:-false}" != "true" ]; then
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
fi

exec "$@"
