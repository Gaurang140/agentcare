# Evaluation harness

AgentCare evaluation has two phases so deterministic safety measurements do
not depend on a model key.

- `phase1_run.py` exercises a running API and records structured outcomes.
- `phase2_score.py` scores that recording without contacting the API.

The harness does not alter backend scoring behavior.

## Dataset

[golden_dataset.json](golden_dataset.json) contains:

| Section | Samples | Purpose |
|---|---:|---|
| `admin_samples` | 40 | Intent, department, workflow steps and response quality |
| `guardrail_samples` | 26 | Emergency, medical, injection and allowed classifications |

Administrative and safety cases cover English and German. Guardrail cases
include legitimate look-alikes so a system that blocks everything cannot
score well.

Expected administrative values are grounded in the synthetic seed under
`scripts/seed_demo.py`.

## Phase 1: collect

Start the backend with migrated and seeded data, then run from the repository
root:

```bash
.venv/bin/python evals/phase1_run.py --run-id local
```

The runner logs in as the seeded demo patients, posts every sample, polls each
workflow to a terminal or waiting state and writes:

```text
evals/results/enriched-local.json
```

`EVAL_BASE_URL` overrides the default `http://localhost:8000`. If `--run-id`
is omitted, the runner uses a UTC timestamp and does not overwrite prior
results.

Phase 1 does not score.

## Phase 2: score

```bash
.venv/bin/python evals/phase2_score.py --run-id local
```

The scorer reads the enriched file and writes:

```text
evals/results/scores-local.json
evals/results/summary-local.md
```

The JSON file is machine-readable evidence. The Markdown file explains run
mode, deterministic metrics, administrative metrics and judge status.

One recorded run can be rescored without another API run.

## Metrics

No judge key is required for:

- exact guardrail class accuracy
- blocked-versus-allowed precision, recall and confusion counts
- named false positives and false negatives
- intent accuracy
- department accuracy
- expected-versus-actual step Jaccard score
- response-language checks

The optional model judge scores administrative responses for relevance,
completeness, language match and the administrative boundary.

Judge key precedence is:

1. `JUDGE_GROQ`
2. `LLM_API_KEY`

`JUDGE_MODEL` and `JUDGE_BASE_URL` can point the judge at another
OpenAI-compatible endpoint. The scorer routes judge output through the same
`chat_json` policy boundary as the application. Without a judge key, response
quality remains explicitly pending.

Rate-limit pacing constants are defined near the top of
`phase2_score.py`.

## Metric self-test

Run the embedded metric fixture:

```bash
.venv/bin/python evals/phase2_score.py --selftest
```

Lint the harness:

```bash
.venv/bin/ruff check evals
```

## Committed no-key baseline

The repository preserves one measured baseline:

- [summary-nokey-baseline.md](results/summary-nokey-baseline.md)
- [scores-nokey-baseline.json](results/scores-nokey-baseline.json)
- [enriched-nokey-baseline.json](results/enriched-nokey-baseline.json)

That run was collected and scored on 2026-07-27 with no model configured. Its
26 guardrail samples were all classified correctly, including 10 allowed
cases, with no false positive or false negative.

The same run leaves all administrative requests at staff handoff because the
coordinator has no working model. Its administrative scores measure graceful
degradation, not model-assisted workflow quality. Run both phases with a
working model profile before making an administrative performance claim.

## Result hygiene

`evals/results/` is ignored except for the named baseline artifacts. Do not
commit keys, cookies, raw patient data or unreviewed large result sets.
Synthetic evaluation content only.
