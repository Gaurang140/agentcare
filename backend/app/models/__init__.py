"""ORM model exports.

Importing this package registers every table on Base.metadata (required for
Alembic autogenerate) and makes each model importable as `from app.models
import X`.
"""

from app.models.agent_rule import AGENT_NAMES, AgentRule
from app.models.appointment import Appointment, AppointmentSlot
from app.models.audit import AuditEvent
from app.models.catalog import Department, Doctor, RequiredDocument
from app.models.document import PatientDocument
from app.models.user import PatientProfile, User
from app.models.workflow import Escalation, Reminder, WorkflowRun

__all__ = [
    "AGENT_NAMES",
    "AgentRule",
    "Appointment",
    "AppointmentSlot",
    "AuditEvent",
    "Department",
    "Doctor",
    "RequiredDocument",
    "PatientDocument",
    "PatientProfile",
    "User",
    "Escalation",
    "Reminder",
    "WorkflowRun",
]
