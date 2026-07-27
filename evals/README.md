# Evaluation harness

Two phases. Phase 1 needs the running system and no API key. Phase 2 needs
only the file phase 1 wrote. The split is what lets the safety numbers exist
without a model provider.

`phase1_run.py` logs in with the seeded demo accounts, posts all 66 golden
samples to the live HTTP API, polls each workflow until it settles and writes
an enriched copy of the dataset to `results/enriched-<run-id>.json`. It scores
nothing.

`phase2_score.py` reads that file and writes `results/scores-<run-id>.json`
plus a readable `results/summary-<run-id>.md`. One live run can be re-scored
as often as the metrics change, and re-scoring never touches the server.

## The dataset

`golden_dataset.json` has two sections.

- `admin_samples` (40, half English half German): ordinary administrative
  requests with the intent, department and workflow steps each one should
  produce. Every expected value is grounded in the seeded demo data
  (`scripts/seed_demo.py`), so a keyed run can actually satisfy them.
- `guardrail_samples` (26, English and German): 16 adversarial inputs across
  the three safety screens, and 10 legitimate requests that resemble them.
  The look-alikes carry the false-positive measurement. Without them a guard
  that blocks every request would score perfectly.

## The three commands

```bash
# 1. seed the demo data (from the repo root)
.venv/bin/python scripts/seed_demo.py

# 2. phase 1, against a running backend on port 8000
.venv/bin/python evals/phase1_run.py --run-id my-run

# 3. phase 2, no server needed
.venv/bin/python evals/phase2_score.py --run-id my-run
```

Start the backend first if it is not up:

```bash
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/python -m uvicorn app.main:app --port 8000
```

`EVAL_BASE_URL` overrides the target (default `http://localhost:8000`).
`--run-id` is optional and defaults to a UTC timestamp, so a re-run never
overwrites an earlier one by accident.

## Which metrics need a key

No key at all:

| Metric | What it reads |
|---|---|
| Guardrail confusion matrix, per class and blocked against allowed | run status, current step and escalation severity |
| False positives and false negatives, named | the same fields, per sample |
| Intent correctness | `state.intent` |
| Expected against actual steps, as a Jaccard score | `state.completed_steps` |
| Department accuracy | `state.department_name` |
| Response language | the response text (the one metric with no structured field to read) |

Key gated, `JUDGE_GROQ` first and `LLM_API_KEY` as the fallback:

| Metric | Notes |
|---|---|
| LLM-judge rubric over admin responses, 1 to 5 on relevance, completeness, administrative boundary and language match | pydantic validated through `backend/app/agents/llm.py::chat_json`, the single LLM entry point in this repo |

Two keys rather than one so an eval run never spends the demo key's own rate
limit. `JUDGE_MODEL` and `JUDGE_BASE_URL` point the judge at any
OpenAI-compatible endpoint. With no key set the judge section prints as
pending and reports no numbers, rather than dropping out of the summary.

Pacing constants for a rate-limited provider (`JUDGE_BATCH_SIZE`,
`JUDGE_BATCH_COOLDOWN_S`, `JUDGE_SAMPLE_DELAY_S`) sit at the top of
`phase2_score.py`.

## The metric gate

The scoring math lives outside the backend package, so it carries its own
test:

```bash
.venv/bin/python evals/phase2_score.py --selftest
```

That runs 58 assertions over an embedded fixture, covering the
confusion-matrix counts, the binary collapse, the Jaccard scores, the language
detection and the summary rendering. Lint with `.venv/bin/ruff check evals`.

## The committed baseline

`results/` is otherwise ignored, with one exception: the `nokey-baseline` run
is committed as the measured number this harness produces. It was taken with
`LLM_API_KEY` empty, which is the state a judge sees on a fresh clone.

| Result | Value |
|---|---|
| Guardrail classification, all four classes | 26 of 26, accuracy 1.0 |
| Blocked against allowed | precision 1.0, recall 1.0, 0 false positives, 0 false negatives |
| Legitimate look-alikes correctly allowed | 5 of 5 |
| Response language on the localized safety templates | 11 of 11, German 5 of 5 |

The administrative metrics in that same run are near zero, and that is the
expected reading. With no model configured every administrative request stops
at the coordinator and parks on an `agent_failure` handoff to staff. That is
the documented degradation path, so the summary labels the run as degraded
instead of reporting those numbers as agent performance. Re-run both phases
with a key set and they become a measurement of the agents.
