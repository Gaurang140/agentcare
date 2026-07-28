"""Run one persisted maintenance job for an external scheduler."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from app.scheduler import _run_reminder_job, _run_stalled_job


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=("reminders", "recovery"))
    job = parser.parse_args(argv).job
    if job == "reminders":
        _run_reminder_job(raise_errors=True)
    else:
        _run_stalled_job(raise_errors=True)


if __name__ == "__main__":
    main()
