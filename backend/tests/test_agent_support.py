from app.agents.support import record_agent_exit, redact_request_for_agent
from app.models import AuditEvent


def test_redact_request_for_agent_redacts_and_audits_counts(db, seeded):
    state = {
        "workflow_id": 41,
        "patient_id": 1,
        "request_text": "email jane.doe@example.com about my appointment",
    }

    redacted = redact_request_for_agent(db, state, "routing")

    assert redacted == "email [REDACTED_EMAIL] about my appointment"
    row = db.query(AuditEvent).filter_by(
        action="safety.pii_redacted",
        entity_type="workflow_run",
        entity_id=41,
    ).one()
    assert row.metadata_json == {"node": "routing", "counts": {"email": 1}}


def test_record_agent_exit_commits_named_audit_event(db, seeded):
    record_agent_exit(db, "coordinator", 42, {"next_step": "finalize"})

    row = db.query(AuditEvent).filter_by(
        action="agent.coordinator.completed",
        entity_type="workflow_run",
        entity_id=42,
    ).one()
    assert row.metadata_json == {"next_step": "finalize"}
