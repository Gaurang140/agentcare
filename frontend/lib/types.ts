/**
 * Types mirroring backend/app/schemas/*.py exactly. Keep field names and
 * optionality in sync with the Pydantic models - do not add fields the
 * backend does not return, and do not narrow `string` status fields into
 * literal unions the backend schemas themselves don't declare.
 */

// ---- schemas/auth.py ----

export interface RegisterRequest {
  name: string;
  email: string;
  password: string;
  dob?: string | null; // ISO date (YYYY-MM-DD)
  phone?: string | null;
  preferred_language?: string;
  emergency_contact?: string | null;
}

export interface LoginRequest {
  email: string;
  password: string;
}

/** Shape returned by register, login, and /me. */
export interface UserSummary {
  id: number;
  name: string;
  email: string;
  role: string; // "patient" | "staff"
}

// ---- schemas/appointment.py ----

export interface DepartmentOut {
  id: number;
  name: string;
  description?: string | null;
}

export interface SlotOut {
  slot_id: number;
  doctor_id: number;
  doctor: string;
  start_time: string;
  end_time: string;
}

export interface AppointmentOut {
  id: number;
  doctor: string;
  department: string;
  start_time: string | null;
  status: string; // "pending" | "confirmed" | "cancelled"
  reason?: string | null;
}

export interface RescheduleRequest {
  new_slot_id: number;
}

export interface ReminderOut {
  id: number;
  patient_id: number;
  appointment_id: number | null;
  reminder_type: string;
  scheduled_at: string;
  sent: boolean;
}

// ---- schemas/profile.py ----

export interface ProfileOut {
  name: string;
  email: string;
  date_of_birth: string | null;
  phone: string | null;
  preferred_language: string;
  emergency_contact: string | null;
}

export interface ProfileUpdateRequest {
  date_of_birth?: string | null;
  phone?: string | null;
  preferred_language?: "en" | "de";
  emergency_contact?: string | null;
}

// ---- schemas/document.py ----

export interface DocumentMeta {
  id: number;
  patient_id: number;
  filename: string;
  document_type: string;
  created_at: string;
}

export interface DocumentListResponse {
  documents: DocumentMeta[];
  requirements: Record<string, unknown> | null;
}

// ---- schemas/staff.py ----

export interface WorkflowRunSummary {
  id: number;
  patient_id: number;
  status: string; // "running" | "waiting_approval" | "completed" | "failed" | "escalated"
  current_step: string | null;
  request_text: string;
  created_at: string;
  updated_at: string;
}

export interface EscalationOut {
  id: number;
  workflow_run_id: number | null;
  reason: string;
  severity: string;
  status: string; // "open" | ...
  reviewed_by?: number | null;
  resolution_note?: string | null;
  created_at: string;
}

export interface ResolveEscalationRequest {
  approve: boolean;
  note?: string;
}

export interface AuditEventOut {
  id: number;
  actor_id: number | null;
  action: string;
  entity_type: string;
  entity_id: number | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

export interface DepartmentCreate {
  name: string;
  description?: string | null;
}

export interface DoctorCreate {
  department_id: number;
  name: string;
}

export interface DoctorUpdate {
  active: boolean;
}

export interface DoctorOut {
  id: number;
  department_id: number;
  name: string;
  active: boolean;
}

export interface SlotGenerateRequest {
  doctor_id: number;
  date_from: string; // ISO date
  date_to: string; // ISO date
}

export interface ReminderRunResponse {
  sent_count: number;
  reminder_ids: number[];
}

export interface AgentRuleOut {
  id: number;
  agent_name: string;
  rule_text: string;
  source: string; // "seed" | "staff"
  active: boolean;
  created_at: string;
}

export interface AgentRuleCreate {
  agent_name: string;
  rule_text: string;
}

export interface AgentRuleUpdate {
  active: boolean;
}

// ---- schemas/workflow.py ----

/** POST /requests replies immediately, before the graph has run. */
export interface CreateRequestResponse {
  workflow_id: number;
  status: string;
}

/** GET /workflows/{id}: the run plus everything it produced. */
export interface WorkflowRunDetail {
  id: number;
  status: string;
  current_step: string | null;
  request_text: string;
  state: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  appointment?: Record<string, unknown> | null;
  documents: DocumentMeta[];
  escalation?: Record<string, unknown> | null;
}

export interface ResumeResponse {
  id: number;
  status: string;
}

// ---- app/exceptions.py error envelope ----

/** Shape of every non-2xx JSON body: {"error": code, "message": message}. */
export interface ApiErrorBody {
  error: string;
  message: string;
}

// ---- SSE event payload from routes_events.py's _serialize() ----

export interface WorkflowEventPayload {
  id: number;
  action: string;
  entity_type: string;
  entity_id: number | null;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
}
