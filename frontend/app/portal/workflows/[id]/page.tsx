"use client";

import { useParams, useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/status-badge";
import { WorkflowTimeline } from "@/components/workflow-timeline";
import { useWorkflowDetail } from "@/hooks/use-workflow-detail";

const TERMINAL_STATUSES = new Set(["completed", "failed", "escalated"]);

interface AppointmentSummary {
  id?: number;
  doctor?: string;
  department?: string;
  start_time?: string | null;
  end_time?: string | null;
  status?: string;
}

interface EscalationSummary {
  id?: number;
  reason?: string;
  severity?: string;
  status?: string;
}

export default function WorkflowDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const workflowId = Number(params.id);

  const { detail, events, loading, live, notFound } = useWorkflowDetail(
    Number.isFinite(workflowId) ? workflowId : null,
  );

  if (!Number.isFinite(workflowId)) {
    return <p className="text-sm text-muted-foreground">Invalid workflow id.</p>;
  }

  if (notFound) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Request not found</CardTitle>
          <CardDescription>
            This request doesn&apos;t exist or isn&apos;t yours to view.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="outline" onClick={() => router.push("/portal")}>
            Back to requests
          </Button>
        </CardContent>
      </Card>
    );
  }

  const isTerminal = detail ? TERMINAL_STATUSES.has(detail.status) : false;
  const finalResponse = (detail?.state?.final_response as string | undefined) ?? null;
  const schedulingIssue =
    (detail?.state?.scheduling_issue as string | undefined) ?? null;
  const appointment = (detail?.appointment ?? null) as AppointmentSummary | null;
  const escalation = (detail?.escalation ?? null) as EscalationSummary | null;

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Request #{workflowId}</CardTitle>
            <CardDescription>{detail?.request_text ?? (loading ? "Loading..." : "")}</CardDescription>
          </div>
          {detail ? <StatusBadge status={detail.status} /> : null}
        </CardHeader>
        <CardContent className="flex flex-col gap-1 text-xs text-muted-foreground">
          {detail ? (
            <>
              <span>Created {new Date(detail.created_at).toLocaleString()}</span>
              <span>Last updated {new Date(detail.updated_at).toLocaleString()}</span>
            </>
          ) : null}
          <span>{live ? "Live updates" : "Polling for updates every 3s"}</span>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Timeline</CardTitle>
          <CardDescription>Every step an agent has taken on this request, in order.</CardDescription>
        </CardHeader>
        <CardContent>
          <WorkflowTimeline events={events} />
        </CardContent>
      </Card>

      {detail?.status === "waiting_approval" ? (
        <Card className="border-primary/30 bg-muted/40">
          <CardHeader>
            <CardTitle className="text-base">With the practice team</CardTitle>
            <CardDescription>
              A staff member is reviewing this request. It continues on its own once they
              decide, and this page updates when it does.
            </CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      {isTerminal ? (
        <Card className="border-primary/30 bg-muted/40">
          <CardHeader>
            <CardTitle className="text-base">Outcome</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm">
            <p>{finalResponse ?? "This request has finished, but left no final message."}</p>
            {schedulingIssue ? (
              <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-amber-950">
                <p className="font-medium">Scheduling constraint</p>
                <p>{schedulingIssue}</p>
              </div>
            ) : null}
            {appointment ? (
              <div className="rounded-lg border bg-background p-3">
                <p className="text-xs font-medium text-muted-foreground">Appointment</p>
                <p>
                  {appointment.doctor ?? "—"} · {appointment.department ?? "—"}
                </p>
                <p className="text-xs text-muted-foreground">
                  {appointment.start_time
                    ? `${new Date(appointment.start_time).toLocaleString()}${
                        appointment.end_time
                          ? ` – ${new Date(appointment.end_time).toLocaleTimeString()}`
                          : ""
                      }`
                    : "—"}
                  {appointment.status ? ` · ${appointment.status}` : ""}
                </p>
              </div>
            ) : null}
            {escalation ? (
              <div className="rounded-lg border bg-background p-3">
                <p className="text-xs font-medium text-muted-foreground">Escalated to staff</p>
                <p>{escalation.reason ?? "—"}</p>
                <p className="text-xs text-muted-foreground">
                  Severity: {escalation.severity ?? "—"} · Status: {escalation.status ?? "—"}
                </p>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
