# Documentation index

Reading order for a first visit:

1. [../README.md](../README.md) - what it is, how a request runs, setup, demos
2. [architecture.md](architecture.md) - request path, workflow lifecycle, data model, observability ([architecture.mmd](architecture.mmd) is the diagram source of truth)
3. [security.md](security.md) - trust boundary and the safety layers, one by one
4. [decisions.md](decisions.md) - 15 ADRs; the README's stack table is the skim layer, this is the proof layer
5. [demo-script.md](demo-script.md) - shot-by-shot 2-minute demo
6. [deployment-gcp.md](deployment-gcp.md) - the 12-step GCP walkthrough
7. [runbook.md](runbook.md) - every command explained, local through GCP

## What is verified vs. planned

| Claim | Status |
|---|---|
| Local run (compose and no-Docker), seeded demo, both portals | Verified - tests + fresh-clone walkthrough |
| 416-test suite, ruff, frontend lint and production build | Verified - run on every change |
| Deterministic safety gates, EN + DE, incl. homoglyph folding | Verified - test suite + committed eval baseline (26/26 guardrail samples, precision/recall 1.0) |
| Crash-resume and interrupt/approve on persisted checkpoints | Verified - fake-LLM end-to-end tests + curl demo in the README |
| Groq via the OpenAI-compatible endpoint (`llm.yaml` `groq`/`local` profiles) | Verified offline with SDK-shaped fakes; live run gated on a key |
| LLM-judge eval half, keyed EN/DE eval run | Planned - needs `LLM_API_KEY` |
| Other model providers (e.g. Gemini via `langchain-google-genai`) | Possible, not verified - commented profile in `backend/llm.yaml`, needs its package + a live smoke test |
| GCP: Terraform, GKE overlay, Model Armor template (multi-language on) | Committed and validated (`tofu validate`, `kubeconform`), **not deployed** - no GCP project exists yet |
| Model Armor as live guardrail provider | Not deployed - adapter + template ready; separate input/output templates recorded as follow-up |
| Cloud Scheduler/Tasks replacing in-process APScheduler | Follow-up - single replica is pinned and documented until then |

Update this table when any status changes; it is the single place a reader
should trust over any older sentence elsewhere in these documents.
