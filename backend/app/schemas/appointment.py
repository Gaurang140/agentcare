"""Request/response schemas for departments, slots, appointments and reminders."""

from datetime import datetime

from pydantic import BaseModel


class DepartmentOut(BaseModel):
    id: int
    name: str
    description: str | None = None


class SlotOut(BaseModel):
    slot_id: int
    doctor_id: int
    doctor: str
    start_time: str
    end_time: str


class AppointmentOut(BaseModel):
    id: int
    doctor: str
    department: str
    start_time: str | None
    status: str
    reason: str | None = None


class RescheduleRequest(BaseModel):
    new_slot_id: int


class ReminderOut(BaseModel):
    id: int
    patient_id: int
    appointment_id: int | None
    reminder_type: str
    scheduled_at: datetime
    sent: bool
