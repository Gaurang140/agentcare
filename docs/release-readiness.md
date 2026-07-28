# Release readiness design

This document defines the final AgentCare release gate for the Build Challenge.
It is intentionally narrower than a product roadmap: every item below either
closes a verified judging risk, fixes a demonstrated workflow defect, or makes
the deployed evidence match the repository.

## Invariants

- AgentCare remains an administrative system. Diagnosis, treatment, dosage,
  and emergency requests are blocked or escalated deterministically.
- LangGraph remains the workflow orchestrator, SQL remains the system of
  record, and every workflow action remains attributable through persisted
  state and audit events.
- Production uses Vertex AI through LangChain's Google provider. Calls have a
  shared in-process request limiter, a finite timeout, and at most two retries.
  Local development may still select a Groq profile explicitly.
- Model Armor augments the local deterministic gates; it does not replace
  healthcare scope enforcement, input validation, or PII minimization.
- No public staff password, usable secret, raw request text in audit metadata,
  or silent production SQLite fallback is allowed.

## Request and appointment flow

1. The API accepts a caller-generated idempotency key. Replaying the same key
   for the same patient returns the original workflow rather than creating a
   second run or uploading documents twice.
2. A deterministic parser converts supported English and German expressions
   such as “next week” and “nächste Woche” into a concrete date window.
3. SQL fetches only future slots inside that window. Vertex chooses among those
   valid candidates; it cannot choose an out-of-window slot.
4. If no candidate exists, the workflow reports that constraint instead of
   silently booking the earliest unrelated slot.
5. Before booking, AgentCare checks the patient's active appointments in the
   requested window. A repeated request returns the existing appointment unless
   the patient explicitly asks for an additional appointment.
6. Database overlap constraints remain the final concurrency guard. We will
   not add a blanket one-active-appointment-per-department constraint because
   legitimate future follow-ups can share a department without overlapping.
7. The patient UI separates active and historical appointments, shows start
   and end time, creation time, full reason, and originating request, and lets
   the patient choose a rescheduling date range.

## Response and reminder integrity

- Appointment facts are rendered from committed SQL rows. The LLM may assess
  safety and presentation but may not rewrite authoritative doctor, date, or
  time values.
- Reminder summaries include only reminders tied to the current workflow's
  appointments.
- Reminder jobs never create already-expired pre-appointment reminders.
- Documentation says reminders are scheduled and processed; it does not claim
  email or SMS delivery that the application does not implement.

## Production and delivery

- CI tests migrations against PostgreSQL 17, matching Cloud SQL.
- The Kubernetes migration Job invokes `alembic upgrade head` explicitly and
  never relies on an image entrypoint side effect or automatic demo seeding.
- Production startup fails on a non-PostgreSQL database URL.
- Demo staff access is provisioned from a Kubernetes Secret or an operator
  command and is never displayed in the UI or README.
- Background jobs run in dedicated Kubernetes CronJobs. This permits two
  rolling backend replicas and two frontend replicas, startup/readiness probes,
  disruption budgets, and no planned single-pod release outage.
- Application pushes build immutable images and roll the existing Deployments.
  Terraform changes infrastructure only when the infrastructure workflow is
  deliberately run. A normal source push does not recreate GKE or Cloud SQL.
- The existing GCP stack is updated in place. Destruction is reserved for an
  unrecoverable Terraform-state mismatch; none is currently evidenced.

## Evidence gate

Release is complete only when all local tests, lint, frontend build, Terraform
validation, and manifest validation pass, followed by CI and live checks for:

- PostgreSQL migration and health;
- one Vertex administrative workflow with correct relative-date behavior;
- deterministic medical/emergency refusal;
- injection refusal;
- synthetic multipart document upload and classification;
- human approval and same-thread resume;
- request replay idempotency and patient scheduling-conflict handling;
- no public staff credential and no sensitive audit metadata.

Anything not verified in the deployed environment is labelled optional,
planned, or unverified in the public documentation.
