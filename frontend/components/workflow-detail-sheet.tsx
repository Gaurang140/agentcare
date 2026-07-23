"use client";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { StatusBadge } from "@/components/status-badge";
import { WorkflowTimeline } from "@/components/workflow-timeline";
import { useWorkflowDetail } from "@/hooks/use-workflow-detail";

const TERMINAL_STATUSES = new Set(["completed", "failed", "escalated"]);

interface AppointmentSummary {
  id?: number;
  doctor?: string;
  department?: string;
  start_time?: string | null;
  status?: string;
}

interface EscalationSummary {
  id?: number;
  reason?: string;
  severity?: string;
  status?: string;
}

interface WorkflowDetailSheetProps {
  /** null closes the sheet; any other value opens it and loads that run. */
  workflowId: number | null;
  /** WorkflowRunDetail (from GET /api/workflows/{id}) has no patient_id
   * field - only WorkflowRunSummary (the queue row) does - so the caller
   * passes it straight from the row it already has, rather than this sheet
   * guessing at an untyped key inside `state`. */
  patientId: number | null;
  onOpenChange: (open: boolean) => void;
}

/** Staff queue's row-click drawer: the same live SSE timeline the patient
 * portal shows (via useWorkflowDetail, extracted from that page in this
 * task), inside a Sheet instead of a full route. ensure_owner_or_staff
 * (backend/app/auth/dependencies.py) lets staff read any patient's
 * workflow, so GET /api/workflows/{id} and its /events stream both just
 * work here without a separate staff-only endpoint. */
export function WorkflowDetailSheet({
  workflowId,
  patientId,
  onOpenChange,
}: WorkflowDetailSheetProps) {
  const { detail, events, loading, live, notFound } = useWorkflowDetail(workflowId);

  const isTerminal = detail ? TERMINAL_STATUSES.has(detail.status) : false;
  const finalResponse = (detail?.state?.final_response as string | undefined) ?? null;
  const appointment = (detail?.appointment ?? null) as AppointmentSummary | null;
  const escalation = (detail?.escalation ?? null) as EscalationSummary | null;

  return (
    <Sheet open={workflowId !== null} onOpenChange={onOpenChange}>
      <SheetContent className="flex w-full flex-col gap-0 overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>{workflowId !== null ? `Request #${workflowId}` : "Request"}</SheetTitle>
          <SheetDescription>
            {detail?.request_text ?? (loading ? "Loading..." : "")}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-4 overflow-y-auto px-4 pb-4">
          {notFound ? (
            <p className="text-sm text-muted-foreground">This request could not be found.</p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                {detail ? <StatusBadge status={detail.status} /> : null}
                {patientId !== null ? <span>Patient #{patientId}</span> : null}
                {detail ? <span>Created {new Date(detail.created_at).toLocaleString()}</span> : null}
                <span>{live ? "Live updates" : "Polling every 3s"}</span>
              </div>
              <WorkflowTimeline events={events} />
              {isTerminal ? (
                <div className="rounded-lg border bg-muted/40 p-3 text-sm">
                  <p className="mb-2 text-xs font-medium text-muted-foreground">Outcome</p>
                  <p>{finalResponse ?? "This request has finished, but left no final message."}</p>
                  {appointment ? (
                    <div className="mt-3 rounded-lg border bg-background p-3">
                      <p className="text-xs font-medium text-muted-foreground">Appointment</p>
                      <p>
                        {appointment.doctor ?? "—"} · {appointment.department ?? "—"}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {appointment.start_time
                          ? new Date(appointment.start_time).toLocaleString()
                          : "—"}
                        {appointment.status ? ` · ${appointment.status}` : ""}
                      </p>
                    </div>
                  ) : null}
                  {escalation ? (
                    <div className="mt-3 rounded-lg border bg-background p-3">
                      <p className="text-xs font-medium text-muted-foreground">Escalation</p>
                      <p>{escalation.reason ?? "—"}</p>
                      <p className="text-xs text-muted-foreground">
                        Severity: {escalation.severity ?? "—"} · Status: {escalation.status ?? "—"}
                      </p>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
