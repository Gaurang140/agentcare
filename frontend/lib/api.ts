/**
 * Typed fetch client for the AgentCare API. Every call goes through the
 * Next.js rewrite in next.config.ts (/api/* -> backend), same origin, so
 * the httpOnly access_token cookie set by the backend just works with
 * `credentials: "include"` - no token handling in the browser at all.
 */

import type {
  AgentRuleCreate,
  AgentRuleOut,
  ApiErrorBody,
  AppointmentOut,
  AuditEventOut,
  CreateRequestResponse,
  DepartmentCreate,
  DepartmentOut,
  DocumentListResponse,
  DoctorCreate,
  DoctorOut,
  EscalationOut,
  LoginRequest,
  ProfileOut,
  ProfileUpdateRequest,
  RegisterRequest,
  ReminderOut,
  RescheduleRequest,
  ResolveEscalationRequest,
  SlotGenerateRequest,
  SlotOut,
  UserSummary,
  WorkflowRunDetail,
  WorkflowRunSummary,
} from "./types";

/** Thrown for any non-2xx response. Carries the backend's {error, message}
 * envelope (app/exceptions.py::register_exception_handlers) so callers can
 * branch on `.status` or show `.message` directly in a toast. */
export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.message = message;
  }
}

const DEFAULT_ERROR_MESSAGE = "Something went wrong. Please try again.";

async function parseErrorBody(res: Response): Promise<ApiErrorBody> {
  try {
    const body = await res.json();
    if (body && typeof body === "object" && "message" in body) {
      return {
        error: typeof body.error === "string" ? body.error : "unknown_error",
        message: typeof body.message === "string" ? body.message : DEFAULT_ERROR_MESSAGE,
      };
    }
  } catch {
    // response wasn't JSON (e.g. a proxy/network-level error page) - fall
    // through to the generic message below.
  }
  return { error: "unknown_error", message: DEFAULT_ERROR_MESSAGE };
}

/** Core wrapper: same-origin credentials, JSON in, JSON out, normalized
 * errors. Pass a FormData body (e.g. submitRequest) and the browser sets
 * its own multipart Content-Type - never override it manually. */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  const headers: HeadersInit = isFormData
    ? { ...init.headers }
    : { "Content-Type": "application/json", ...init.headers };

  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers,
  });

  if (!res.ok) {
    const { error, message } = await parseErrorBody(res);
    throw new ApiError(res.status, error, message);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return (await res.json()) as T;
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      usp.set(key, String(value));
    }
  }
  const qs = usp.toString();
  return qs ? `?${qs}` : "";
}

// ---- auth (routes_auth.py) ----

export function register(payload: RegisterRequest): Promise<UserSummary> {
  return apiFetch("/api/auth/register", { method: "POST", body: JSON.stringify(payload) });
}

export function login(payload: LoginRequest): Promise<UserSummary> {
  return apiFetch("/api/auth/login", { method: "POST", body: JSON.stringify(payload) });
}

export function logout(): Promise<{ status: string }> {
  return apiFetch("/api/auth/logout", { method: "POST" });
}

export function me(): Promise<UserSummary> {
  return apiFetch("/api/auth/me");
}

// ---- requests / workflows (routes_workflows.py) ----

/** POST /api/requests is multipart: a `text` field plus zero or more
 * `files`. Build the FormData in the caller so the page controls exactly
 * which fields/files go in. */
export function submitRequest(
  formData: FormData,
  idempotencyKey: string,
): Promise<CreateRequestResponse> {
  return apiFetch("/api/requests", {
    method: "POST",
    body: formData,
    headers: { "Idempotency-Key": idempotencyKey },
  });
}

/** Ownership-filtered: always the caller's own runs, most recent first. */
export function listWorkflows(): Promise<WorkflowRunSummary[]> {
  return apiFetch("/api/workflows");
}

export function getWorkflow(id: number): Promise<WorkflowRunDetail> {
  return apiFetch(`/api/workflows/${id}`);
}

// ---- patient self-service (routes_patient.py) ----

export function listAppointments(): Promise<AppointmentOut[]> {
  return apiFetch("/api/appointments");
}

export function rescheduleAppointment(
  appointmentId: number,
  payload: RescheduleRequest,
): Promise<AppointmentOut> {
  return apiFetch(`/api/appointments/${appointmentId}/reschedule`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function cancelAppointment(appointmentId: number): Promise<AppointmentOut> {
  return apiFetch(`/api/appointments/${appointmentId}/cancel`, { method: "POST" });
}

export function listDepartments(): Promise<DepartmentOut[]> {
  return apiFetch("/api/departments");
}

export function listSlots(
  departmentId: number,
  params: { date_from?: string; date_to?: string; limit?: number } = {},
): Promise<SlotOut[]> {
  return apiFetch(`/api/departments/${departmentId}/slots${query(params)}`);
}

export function listReminders(): Promise<ReminderOut[]> {
  return apiFetch("/api/reminders");
}

export function getProfile(): Promise<ProfileOut> {
  return apiFetch("/api/profile");
}

export function updateProfile(payload: ProfileUpdateRequest): Promise<ProfileOut> {
  return apiFetch("/api/profile", { method: "PATCH", body: JSON.stringify(payload) });
}

// ---- documents (routes_documents.py) ----

export function listDocuments(
  params: { patient_id?: number; department_id?: number } = {},
): Promise<DocumentListResponse> {
  return apiFetch(`/api/documents${query(params)}`);
}

// ---- staff (routes_staff.py) ----

export function staffListRequests(status?: string): Promise<WorkflowRunSummary[]> {
  return apiFetch(`/api/staff/requests${query({ status })}`);
}

export function staffListEscalations(status = "open"): Promise<EscalationOut[]> {
  return apiFetch(`/api/staff/escalations${query({ status })}`);
}

export function staffResolveEscalation(
  escalationId: number,
  approve: boolean,
  note = "",
): Promise<EscalationOut> {
  const payload: ResolveEscalationRequest = { approve, note };
  return apiFetch(`/api/staff/escalations/${escalationId}/resolve`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** entity_type/limit/offset all optional query params on GET /api/staff/audit;
 * `page` here is a 0-based UI convenience converted to offset = page * pageSize. */
export function staffListAudit(
  page = 0,
  pageSize = 50,
  entityType?: string,
): Promise<AuditEventOut[]> {
  return apiFetch(
    `/api/staff/audit${query({ entity_type: entityType, limit: pageSize, offset: page * pageSize })}`,
  );
}

export function staffCreateDepartment(payload: DepartmentCreate): Promise<DepartmentOut> {
  return apiFetch("/api/staff/departments", { method: "POST", body: JSON.stringify(payload) });
}

/** Fetch doctor choices for the staff catalog and slot-generation form. */
export function staffListDoctors(departmentId?: number): Promise<DoctorOut[]> {
  return apiFetch(`/api/staff/doctors${query({ department_id: departmentId })}`);
}

export function staffCreateDoctor(payload: DoctorCreate): Promise<DoctorOut> {
  return apiFetch("/api/staff/doctors", { method: "POST", body: JSON.stringify(payload) });
}

/** The backend has no department-level toggle (Department has no `active`
 * column - see backend/app/models/catalog.py); the real minimal-admin
 * capability is toggling a doctor's active flag via PATCH
 * /api/staff/doctors/{id} (app/tools/department_tools.py::set_doctor_active).
 * Named for what it actually does rather than inventing a department field. */
export function staffSetDoctorActive(doctorId: number, active: boolean): Promise<DoctorOut> {
  return apiFetch(`/api/staff/doctors/${doctorId}`, {
    method: "PATCH",
    body: JSON.stringify({ active }),
  });
}

export function staffGenerateSlots(payload: SlotGenerateRequest): Promise<SlotOut[]> {
  return apiFetch("/api/staff/slots/generate", { method: "POST", body: JSON.stringify(payload) });
}

// ---- agent rules (procedural memory, routes_staff.py) ----

export function staffListAgentRules(agentName?: string): Promise<AgentRuleOut[]> {
  return apiFetch(`/api/staff/agent-rules${query({ agent_name: agentName })}`);
}

export function staffCreateAgentRule(payload: AgentRuleCreate): Promise<AgentRuleOut> {
  return apiFetch("/api/staff/agent-rules", { method: "POST", body: JSON.stringify(payload) });
}

export function staffSetAgentRuleActive(ruleId: number, active: boolean): Promise<AgentRuleOut> {
  return apiFetch(`/api/staff/agent-rules/${ruleId}`, {
    method: "PATCH",
    body: JSON.stringify({ active }),
  });
}
