#!/bin/sh
# Container startup: migrate by default, optionally seed explicitly for local
# demos, then hand off to the CMD via exec so it becomes PID 1 and receives
# signals directly. Kubernetes runs migrations in a separate Job and sets
# SKIP_STARTUP_MIGRATIONS=true on the long-running backend Deployment.
set -e

if [ "${SKIP_STARTUP_MIGRATIONS:-false}" != "true" ]; then
    alembic upgrade head
fi

if [ "${SEED_DEMO_DATA:-false}" = "true" ]; then
    python -m app.db.seed
fi

exec "$@"
