#!/usr/bin/env python3
"""Phase 1: run the golden dataset against a LIVE AgentCare instance.

This half needs the server, not a model. It logs in with the demo accounts,
posts every sample to POST /api/requests, polls GET /api/workflows/{id} until
the run reaches a terminal state or parks on a staff handoff, and writes an
enriched COPY of the dataset to evals/results/enriched-<run-id>.json. It
scores nothing. Scoring is phase 2, which reads that file and needs no server
at all, so a single live run can be re-scored as often as the metrics change.

What counts as truth here:

- `status`, `current_step`, `state.completed_steps`, `state.intent`,
  `state.department_name` and the escalation's `severity` are structured
  fields, so every derived value comes from them. Response text is recorded
  but never parsed to decide what happened.
- The final read is made as staff. Both roles may read the route (the portal
  and the staff sheet share it), but the patient projection masks the
  escalation reason and narrows the state to what the portal renders. Staff
  see the reason and the whole state, which is what an eval needs.
- Requests are sent as the patient they belong to: patient@ for English
  samples, erika@ for German ones, so the language of the answer is the
  system's own choice and not something the harness forced.

Guardrail classification is derived from `current_step` plus the escalation
severity, both structured:

    safety_screen    + severity emergency  -> blocked_emergency
    safety_screen    + no escalation       -> blocked_medical
    injection_screen                       -> blocked_injection
    anything else                          -> allowed

That mapping follows the three pre-graph exits in
`backend/app/services/workflow_service.py::create_run`. A request that both
screens let through enters the graph, so whatever it ends as (completed,
escalated, failed or waiting_approval) counts as "allowed": the safety layer
made the call to let it through, and how the graph then fared is the subject
of the admin metrics, not the guardrail ones. With no LLM key that is exactly
what an ordinary request does, pausing on an agent_failure handoff.

Usage:

    ../.venv/bin/python evals/phase1_run.py                 # all 66 samples
    ../.venv/bin/python evals/phase1_run.py --run-id scratch-local
    ../.venv/bin/python evals/phase1_run.py --only guardrail --limit 5

Standard library plus httpx, which is already pinned in requirements.txt.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from .evidence import PreservedEvidenceError, require_writable_run_id
except ImportError:
    from evidence import PreservedEvidenceError, require_writable_run_id

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVALS_DIR / "golden_dataset.json"
DEFAULT_OUT_DIR = EVALS_DIR / "results"

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_PASSWORD = "demo1234"

# Seeded demo accounts (backend/app/db/seed.py). The two patients differ only
# in their profile's preferred_language, which is what makes the German
# language check in phase 2 meaningful.
ACCOUNTS = {
    "en": "patient@agentcare-demo.com",
    "de": "erika@agentcare-demo.com",
    "staff": "staff@agentcare-demo.com",
}

# A run in any of these has stopped moving on its own. waiting_approval is on
# the list because the run is parked on a human decision that this harness
# deliberately does not make: approving cases would measure the staff route,
# not the agents.
TERMINAL_STATUSES = {"completed", "escalated", "failed", "waiting_approval"}

POLL_INTERVAL_S = 0.4
POLL_TIMEOUT_S = 180.0
REQUEST_TIMEOUT_S = 60.0

# Pause between samples. Zero is right for a local no-key run; raise it when a
# real key is set and the provider's rate limit is the constraint.
SAMPLE_DELAY_S = float(os.getenv("EVAL_SAMPLE_DELAY_S", "0"))


class EvalError(RuntimeError):
    """A failure that stops the whole run, e.g. the server is not reachable."""


def _login(base_url: str, email: str, password: str) -> httpx.Client:
    """One logged-in client per account. The auth cookie is httpOnly and the
    client keeps it, so every later call on this client is that user."""
    client = httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_S)
    try:
        response = client.post("/api/auth/login", json={"email": email, "password": password})
    except httpx.ConnectError as exc:
        client.close()
        raise EvalError(
            f"cannot reach {base_url}. Start the backend first: "
            "cd backend && ../.venv/bin/python -m uvicorn app.main:app --port 8000"
        ) from exc
    if response.status_code != 200:
        client.close()
        raise EvalError(f"login failed for {email}: {response.status_code} {response.text[:200]}")
    return client


def _post_request(client: httpx.Client, text: str) -> dict:
    """POST /api/requests. The route declares `text` as a Form field, so the
    body is form-encoded; the file list is optional and this harness sends
    text only (see the dataset's attachment_note)."""
    response = client.post("/api/requests", data={"text": text})
    if response.status_code != 202:
        raise EvalError(f"POST /api/requests returned {response.status_code}: {response.text[:300]}")
    return response.json()


def _poll_until_settled(client: httpx.Client, workflow_id: int) -> dict:
    """Read the run until its status stops moving, and return the last body."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    detail: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/workflows/{workflow_id}")
        if response.status_code != 200:
            raise EvalError(
                f"GET /api/workflows/{workflow_id} returned {response.status_code}: "
                f"{response.text[:200]}"
            )
        detail = response.json()
        if detail.get("status") in TERMINAL_STATUSES:
            return detail
        time.sleep(POLL_INTERVAL_S)
    return detail


def _escalation_fields(detail: dict) -> dict | None:
    escalation = detail.get("escalation")
    if not escalation:
        return None
    return {
        "id": escalation.get("id"),
        "severity": escalation.get("severity"),
        "status": escalation.get("status"),
        "reason": escalation.get("reason"),
    }


def classify_guardrail(current_step: str | None, escalation: dict | None) -> str:
    """Which safety screen handled this request, from structured fields only.

    See the module docstring for the mapping and where each branch comes from.
    The medical refusal is the one pre-graph exit that opens no escalation, so
    an absent escalation on the safety_screen step is what identifies it.
    """
    severity = (escalation or {}).get("severity")
    if current_step == "safety_screen":
        return "blocked_emergency" if severity == "emergency" else "blocked_medical"
    if current_step == "injection_screen":
        return "blocked_injection"
    return "allowed"


def _state_of(detail: dict) -> dict:
    return detail.get("state") or {}


def _run_one(
    patient: httpx.Client, staff: httpx.Client, text: str
) -> tuple[dict, dict, float]:
    """Post one request, wait for it to settle, and read it back as staff.

    Returns the patient's own last read, the staff read and the wall time. The
    patient read is kept because it is the view the portal renders, which is
    where a masking regression would show up first.
    """
    started = time.perf_counter()
    created = _post_request(patient, text)
    workflow_id = created["workflow_id"]
    patient_detail = _poll_until_settled(patient, workflow_id)
    elapsed = time.perf_counter() - started

    staff_response = staff.get(f"/api/workflows/{workflow_id}")
    if staff_response.status_code != 200:
        raise EvalError(
            f"staff read of workflow {workflow_id} returned {staff_response.status_code}"
        )
    return patient_detail, staff_response.json(), elapsed


def _enrich_admin(sample: dict, staff_detail: dict, patient_detail: dict, elapsed: float) -> None:
    state = _state_of(staff_detail)
    sample["actual_status"] = staff_detail.get("status")
    sample["actual_current_step"] = staff_detail.get("current_step")
    sample["actual_intent"] = state.get("intent")
    sample["actual_department"] = state.get("department_name")
    sample["actual_steps"] = state.get("completed_steps") or []
    sample["actual_response"] = state.get("final_response")
    sample["actual_error"] = state.get("error")
    sample["actual_escalation"] = _escalation_fields(staff_detail)
    sample["actual_workflow_id"] = staff_detail.get("id")
    sample["actual_patient_visible_response"] = _state_of(patient_detail).get("final_response")
    sample["actual_latency_s"] = round(elapsed, 3)


def _enrich_guardrail(sample: dict, staff_detail: dict, elapsed: float) -> None:
    escalation = _escalation_fields(staff_detail)
    sample["actual"] = classify_guardrail(staff_detail.get("current_step"), escalation)
    sample["actual_status"] = staff_detail.get("status")
    sample["actual_current_step"] = staff_detail.get("current_step")
    sample["actual_escalation"] = escalation
    sample["actual_response"] = _state_of(staff_detail).get("final_response")
    sample["actual_workflow_id"] = staff_detail.get("id")
    sample["actual_latency_s"] = round(elapsed, 3)


def _mark_failed(sample: dict, exc: Exception, *, guardrail: bool) -> None:
    """Record a harness-level failure on the sample instead of ending the run.

    Phase 2 counts these as misses and names them, so one flaky sample cannot
    quietly improve a score by disappearing from the denominator.
    """
    sample["actual_status"] = "eval_error"
    sample["actual_error"] = f"{type(exc).__name__}: {exc}"
    if guardrail:
        sample["actual"] = "eval_error"
    else:
        sample["actual_steps"] = []
        sample["actual_response"] = None


def _progress(index: int, total: int, label: str, sample_id: str, outcome: str) -> None:
    print(f"[{index:>2}/{total}] {label} {sample_id}: {outcome}", flush=True)


def run_phase1(dataset: dict, base_url: str, password: str, only: str, limit: int | None) -> dict:
    """Run every selected sample and return the enriched copy of the dataset."""
    enriched = copy.deepcopy(dataset)
    admin = enriched["admin_samples"] if only in ("all", "admin") else []
    guardrail = enriched["guardrail_samples"] if only in ("all", "guardrail") else []
    if limit is not None:
        admin = admin[:limit]
        guardrail = guardrail[:limit]

    clients = {key: _login(base_url, email, password) for key, email in ACCOUNTS.items()}
    staff = clients["staff"]
    started_at = datetime.now(timezone.utc)

    try:
        total = len(admin) + len(guardrail)
        position = 0

        for sample in admin:
            position += 1
            try:
                patient_detail, staff_detail, elapsed = _run_one(
                    clients[sample["language"]], staff, sample["request"]
                )
                _enrich_admin(sample, staff_detail, patient_detail, elapsed)
                _progress(
                    position,
                    total,
                    "admin",
                    sample["id"],
                    f"{sample['actual_status']} steps={sample['actual_steps']}",
                )
            except (EvalError, httpx.HTTPError) as exc:
                _mark_failed(sample, exc, guardrail=False)
                _progress(position, total, "admin", sample["id"], f"eval_error: {exc}")
            time.sleep(SAMPLE_DELAY_S)

        for sample in guardrail:
            position += 1
            try:
                _, staff_detail, elapsed = _run_one(
                    clients[sample["language"]], staff, sample["input"]
                )
                _enrich_guardrail(sample, staff_detail, elapsed)
                _progress(
                    position,
                    total,
                    "guard",
                    sample["id"],
                    f"expected={sample['expected']} actual={sample['actual']}",
                )
            except (EvalError, httpx.HTTPError) as exc:
                _mark_failed(sample, exc, guardrail=True)
                _progress(position, total, "guard", sample["id"], f"eval_error: {exc}")
            time.sleep(SAMPLE_DELAY_S)
    finally:
        for client in clients.values():
            client.close()

    finished_at = datetime.now(timezone.utc)
    enriched["run"] = {
        "phase": 1,
        "base_url": base_url,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s": round((finished_at - started_at).total_seconds(), 1),
        "admin_samples_run": len(admin),
        "guardrail_samples_run": len(guardrail),
        "accounts": ACCOUNTS,
    }
    enriched["admin_samples"] = admin
    enriched["guardrail_samples"] = guardrail
    return enriched


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentCare eval phase 1: live run")
    parser.add_argument(
        "--base-url",
        default=os.getenv("EVAL_BASE_URL", DEFAULT_BASE_URL),
        help="base URL of the running backend (env EVAL_BASE_URL)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="name for this run's output file; defaults to a UTC timestamp",
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--only", choices=("all", "admin", "guardrail"), default="all")
    parser.add_argument("--limit", type=int, default=None, help="first N of each section")
    parser.add_argument(
        "--password",
        default=os.getenv("EVAL_PASSWORD", DEFAULT_PASSWORD),
        help="demo account password (env EVAL_PASSWORD)",
    )
    args = parser.parse_args(argv)

    run_id = args.run_id or _default_run_id()
    try:
        require_writable_run_id(run_id)
    except PreservedEvidenceError as exc:
        print(f"phase 1 stopped: {exc}", file=sys.stderr)
        return 2

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))

    try:
        enriched = run_phase1(dataset, args.base_url, args.password, args.only, args.limit)
    except EvalError as exc:
        print(f"phase 1 stopped: {exc}", file=sys.stderr)
        return 2

    enriched["run"]["run_id"] = run_id
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"enriched-{run_id}.json"
    out_path.write_text(json.dumps(enriched, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nwrote {out_path}")
    print(f"score it with: ../.venv/bin/python evals/phase2_score.py --input {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
