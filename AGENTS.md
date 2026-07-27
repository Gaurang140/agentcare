# AGENTS.md: rules for AI coding agents working in this repo

AgentCare is an agentic hospital-administration system: FastAPI + LangGraph backend
(six coordinated agents), Next.js 16 frontend and persistent SQL. It has a
reviewed GCP deployment path but has not been deployed to a live GCP project.
It handles administration only: registration, department routing, appointment
booking, document coordination, reminders and follow-up. It never diagnoses,
prescribes or doses. Keep that boundary in every line you write, including
prompts, tests, seeds and docs.

## Repo map

```
backend/app/
  config.py logging_setup.py exceptions.py   # settings, structlog (PII-redacting), AppError hierarchy
  models/    # SQLAlchemy 2.0 (Mapped/mapped_column); patient_id always = users.id
  db/        # session, seed; alembic/ at backend/ root
  auth/      # pwdlib Argon2id, PyJWT HS256 cookies, require_role, ensure_owner_or_staff
  safety/    # deterministic guardrails (pre-LLM screen + output sanitizer)
  tools/     # ~10 real DB tools; every mutation writes an AuditEvent
  agents/    # llm.py (only LLM entry point), state.py, prompts.py, six agent nodes
  services/  # workflow_service (start/resume), storage adapter (local | gcs)
  api/       # routers; RBAC enforced here, never in the frontend
frontend/    # Next.js 16 App Router, Tailwind v4, shadcn; proxy.ts (not middleware.ts)
infra/       # terraform (OpenTofu-compatible HCL, GCP resources) + k8s/
             #   (kustomize base + gcp overlay)
scripts/     # seed_demo.py and helpers
docs/        # architecture, security, demo and GCP deployment guides
```

## Hard rules

1. **No hardcoded agent output.** Anything shown as an agent result comes from an
   LLM call or is composed from persisted rows. Tools always read/write real data.
2. **Administrative language only.** No diagnosis/prescription/dosage wording
   anywhere. The deterministic guardrails in `backend/app/safety/` are the first
   gate; do not weaken them.
3. **Keep the chat-model boundary in `agents/llm.py`.** Agent schemas go through
   `invoke_structured`; the optional prompt-injection classifier goes through
   `classify_injection`. Both build models with LangChain's `init_chat_model`
   factory from profiles in `backend/llm.yaml` (env vars win; see
   `agents/model_config.py`). Never regex-parse JSON out of prose, construct a
   chat model or call a chat-provider SDK elsewhere. Google Model Armor is a
   separate optional safety provider under `backend/app/safety/model_armor.py`,
   not a chat model.
4. **LangGraph 1.2.9 specifics:** checkpointer `from_conn_string` is a context
   manager held open in the FastAPI lifespan (ExitStack); the graph is built and
   compiled once. Any state key written by more than one node uses an
   `Annotated[list, operator.add]`-style reducer. Crash-resume is
   `graph.invoke(None, config)` with the same `thread_id`.
5. **RBAC is backend-only truth.** `require_role(...)` on staff routes,
   `ensure_owner_or_staff(...)` on every patient-data query. Frontend guards are
   UX, never security.
6. **Audit everything.** Mutations and agent node exits write `AuditEvent`; the
   SSE timeline and staff audit view read from it.
7. **Secrets and data:** config via environment only (`.env` local, gitignored;
   `.env.example` documents keys). Never commit `.env`, `*.db`, `uploads/`, real
   PII, or credentials. Seed data stays obviously synthetic.
8. **Dependencies are pinned** in the root `requirements.txt` (also the file CI
   scanners read: it must keep naming `openai` and `langgraph`). Verify current
   APIs against official docs before using anything you are unsure about; several
   APIs changed recently (passlib is dead → pwdlib; Next.js middleware.ts →
   proxy.ts; Groq model lineup).

## Commands

```bash
# backend (venv at .venv, Python 3.12)
.venv/bin/pip install -r requirements.txt
(cd backend && ../.venv/bin/alembic upgrade head)   # migrate (sqlite default)
.venv/bin/python scripts/seed_demo.py               # from repo root, idempotent
(cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload)
PYTHONPATH=backend .venv/bin/python -m pytest -q backend evals/test_evidence_safety.py
.venv/bin/python evals/phase2_score.py --selftest
.venv/bin/ruff check backend evals                  # must stay clean
.venv/bin/python -m compileall backend evals -q     # must stay clean
# frontend
npm --prefix frontend ci
npm --prefix frontend audit --omit=dev --audit-level=high
npm --prefix frontend run lint
npm --prefix frontend run build
# infrastructure
tofu -chdir=infra/terraform fmt -check -recursive
tofu -chdir=infra/terraform init -backend=false -input=false
tofu -chdir=infra/terraform validate
kubectl kustomize infra/k8s/overlays/gcp
# full stack
docker compose up --build
```

## Testing rules

- TDD for behavior changes: failing test first, then implement, then green.
- LLM-dependent tests inject a fake client via
  `app.agents.llm.set_llm_client_for_tests(...)`: no network, no keys in tests.
- Run the focused test while iterating; run the FULL suite + ruff + compileall
  before every commit.

## Commit conventions

Short, human, imperative, lower-case subjects ("add booking tool", "fix ci").
No trailers, no emoji. One logical change per commit. Never push from an agent
session; pushing is a human decision.

## Docs voice

No "leverage", "robust", "ecosystem"; no em-dashes; no serial comma. Plain,
experienced engineering voice. Keep one canonical architecture diagram in the
judge-facing Markdown documentation and use restrained Mermaid node styling.
