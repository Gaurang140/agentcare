# AgentCare demo script (2 minutes)

A shot-by-shot script for the demo video. Two browser windows side by side: one
logged in as the patient (`patient@agentcare-demo.com`), one as staff
(`staff@agentcare-demo.com`), both password `demo1234`. Have a small PDF ready
to upload (an ECG report stand-in). Start the stack with
`docker compose up --build` and confirm Grafana is up at http://localhost:3001.

Total runtime 2:00. Each shot lists what to click, one line to say and what the
judge should notice.

---

**0:00 - 0:15 | Log in**

- Click: open http://localhost:3000, log in as the patient.
- Say: "AgentCare is agentic hospital administration. A patient asks in plain
  language, and a set of agents does the real booking work."
- Notice: a clean patient portal with a single request box. No forms to fill in.

**0:15 - 0:35 | Submit a request with a file**

- Click: type `Book me a cardiology appointment next week`, attach the ECG PDF,
  submit.
- Say: "One sentence and a document. That is the whole input."
- Notice: the request returns instantly with a workflow id, before any model
  call. The UI moves straight to a live timeline.

**0:35 - 0:50 | Watch the agent timeline**

- Click: nothing, let the Server-Sent Events timeline stream.
- Say: "Six agents coordinate. Routing picks Cardiology, the appointment agent
  claims a free slot, the document agent checks what the visit needs."
- Notice: each step appears as it happens, every one an audit row, not a
  hardcoded script.

**0:50 - 1:05 | Confirmation and appointment**

- Click: open the appointment the run just booked.
- Say: "A real slot is booked and confirmed, and it flags the ECG report and
  blood test the department requires."
- Notice: the booking is a database row with a real date and doctor, and
  reminders are already scheduled.

**1:05 - 1:15 | Duplicate upload caught in the audit trail**

- Click: upload the same ECG PDF a second time, then open the audit trail for
  the run.
- Say: "Upload the same file again and the document agent notices."
- Notice: the audit trail records the duplicate rather than storing it blindly.
  The record is the point, not the guess.

**1:15 - 1:30 | Emergency request escalates**

- Click: in the patient window, submit `I have severe chest pain and can't
  breathe`.
- Say: "Some requests are not administrative. This one never reaches a model."
- Notice: an instant response to call 112 and zero model calls, while an
  emergency escalation drops into the staff queue in the other window.

**1:30 - 1:45 | Staff approves with a note**

- Click: switch to the staff window, open the escalation queue, resolve the
  emergency with a short note.
- Say: "A human owns every escalation and closes it with a note."
- Notice: the resolution and the reviewer are written back to the record. The
  boundary between software and clinician is explicit.

**1:45 - 2:00 | Audit trail and a Grafana glance**

- Click: open the staff audit view, then flip to Grafana at
  http://localhost:3001.
- Say: "Every mutation and every agent step is one append-only audit row, and
  the run is observable live in Grafana."
- Notice: a complete trace from request through booking to escalation, plus
  request rate and latency on the dashboard.

**2:00 | Close**

- Say: "AgentCare books, coordinates and follows up, and it knows exactly where
  its job ends. Administration only. Medical decisions stay with clinicians."

---

## Optional B-roll: kill and resume

If there is room for a bonus shot, submit a booking request, run
`docker compose restart backend` mid-run, then call the resume endpoint. The run
picks up from its last checkpoint with no duplicate work. Steps are in the
README under "Kill-and-resume demo".
