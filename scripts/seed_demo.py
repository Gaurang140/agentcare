#!/usr/bin/env python3
"""Thin CLI wrapper around app.db.seed: create tables if needed, seed, print
counts.

Run from the repo root:
    .venv/bin/python scripts/seed_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# backend/ holds the `app` package; this script lives in scripts/ at the
# repo root, so put backend/ on sys.path before importing anything under
# `app` - this makes the documented `.venv/bin/python scripts/seed_demo.py`
# invocation (run from the repo root) work regardless of cwd otherwise.
_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# The default DATABASE_URL is a cwd-relative sqlite path. Alembic and uvicorn
# are documented to run from backend/, so chdir there before the engine is
# created - every entry point then agrees on backend/agentcare.db.
import os  # noqa: E402

os.chdir(_BACKEND_DIR)

from app.db.base import Base  # noqa: E402
from app.db.seed import seed  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402

# Importing app.models registers every table on Base.metadata so
# create_all below sees the full schema.
import app.models  # noqa: E402,F401


def main() -> None:
    # create_all only creates tables that don't already exist, so this is
    # safe to run against a fresh sqlite file or an already-migrated db.
    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        counts = seed(db)
    finally:
        db.close()

    for name, count in counts.items():
        print(f"{name}: {count}")


if __name__ == "__main__":
    main()
