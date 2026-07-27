"""Protection for reviewed evaluation artifacts committed to the repository."""

PRESERVED_RUN_IDS = frozenset({"nokey-baseline"})


class PreservedEvidenceError(ValueError):
    """Raised when a command targets a reviewed, committed run."""


def require_writable_run_id(run_id: str) -> None:
    """Reject output names reserved for committed evidence."""
    if run_id in PRESERVED_RUN_IDS:
        raise PreservedEvidenceError(
            f"{run_id!r} is preserved eval evidence; use a new scratch run id"
        )
