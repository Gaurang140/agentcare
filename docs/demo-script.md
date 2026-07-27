# AgentCare two-minute demo

This walkthrough uses only current UI actions. It shows a real administrative
workflow, checksum idempotency, deterministic emergency handling and a
same-thread staff resume.

## Prepare before recording

1. Start the stack with `docker compose up --build`.
2. Set a working Groq key or another tested profile for model-assisted shots.
3. Keep a small synthetic PDF named `ecg-report.pdf` ready.
4. Open two browser windows at `http://localhost:3000`.
5. Log in as the patient in one window:
   `patient@agentcare-demo.com` / `demo1234`.
6. Log in as staff in the other:
   `staff@agentcare-demo.com` / `demo1234`.
7. In the staff window, open **Audit trail** in one tab and
   **Escalations** in another.
8. Rehearse one uncertain request until it pauses at `waiting_approval`.
   Keep that escalation open for the approval shot.

Use only synthetic content in the PDF.

## Recording

### 0:00 to 0:12: frame the boundary

**Show:** The patient portal home.

**Say:** “AgentCare turns one patient request into auditable hospital
administration. It books, coordinates and follows up. Medical decisions stay
with clinicians.”

### 0:12 to 0:37: create real work

**Do:**

1. Enter `Book me a cardiology appointment next week`.
2. Attach `ecg-report.pdf` in the request form.
3. Select **Submit request**.
4. Open the new request and let the live timeline advance.

**Say:** “FastAPI stores the request first, then six model-assisted roles
coordinate through a checkpointed LangGraph. Routing resolves Cardiology, the
appointment role claims a real free slot and the document role checks the
visit requirements.”

**Point out:** The request returns before model work. Timeline entries arrive
through SSE and come from append-only audit rows.

### 0:37 to 0:55: show persisted results

**Do:** Open the booked appointment, then the patient document list.

**Say:** “The result is persisted, not generated UI text. The slot, document
record, reminders and final response come from SQL-backed tools.”

**Point out:** The doctor and time are stored facts. The uploaded document has
one record.

### 0:55 to 1:10: prove duplicate handling

**Do:**

1. Return to the patient portal home.
2. Create a new request and attach the same `ecg-report.pdf`.
3. Submit it.
4. Switch to the staff **Audit trail**.
5. Keep the newest `document.duplicate_detected` event collapsed.

**Say:** “The portal has no separate upload action. A second request submits
the same bytes, and the backend reuses the existing patient document by
checksum instead of storing a second copy.”

**Point out:** The collapsed audit action and existing document entity. The
audit metadata intentionally omits the checksum.

### 1:10 to 1:25: deterministic emergency path

**Do:** In the patient window submit:

```text
I have severe chest pain and cannot breathe.
```

**Say:** “This is outside administrative scope. Deterministic code returns
emergency guidance and creates a staff escalation before the graph starts.
There is no model call.”

**Point out:** The immediate 112 guidance and emergency status.

### 1:25 to 1:50: resume a human-controlled pause

**Do:**

1. Switch to the pre-staged uncertainty case in **Escalations**.
2. Approve it with:
   `Patient means cardiology. Book the earliest suitable slot.`
3. Switch to the patient request and watch it continue.

**Say:** “Uncertain work stops inside LangGraph `interrupt()`. This staff
decision is persisted, then `Command` resumes the original `thread_id`.
Approval continues the paused run instead of creating a new workflow.”

**Point out:** The same workflow changes from `waiting_approval` to active
steps and then a terminal result. The staff note remains staff-facing.

### 1:50 to 2:00: close on evidence

**Do:** Return to the staff audit trail.

**Say:** “Every mutation, agent exit and approval has one audit record.
AgentCare automates administration, fails to a human when uncertain and keeps
clinical judgment outside the system.”

## If a shot does not behave as rehearsed

- A normal request that escalates usually indicates an unavailable model
  profile. Show the handoff honestly or restart after fixing the profile.
- An uncertain request depends on model confidence. Pre-stage and rehearse it
  rather than improvising during the recording.
- Emergency handling does not depend on a model and remains the reliable
  fallback shot.
- A duplicate is proven by `document.duplicate_detected` in the staff audit
  view. The document list alone shows only the single stored row.

## Optional ten-second language shot

Log in as `erika@agentcare-demo.com` / `demo1234` and submit:

```text
Ich habe starke Brustschmerzen
```

The deterministic response gives German 112 guidance based on the patient's
saved language preference.
