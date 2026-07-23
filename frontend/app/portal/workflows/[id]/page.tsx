"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/status-badge";
import { WorkflowTimeline } from "@/components/workflow-timeline";
import { ApiError, getWorkflow } from "@/lib/api";
import type { WorkflowEventPayload, WorkflowRunDetail } from "@/lib/types";

const TERMINAL_STATUSES = new Set(["completed", "failed", "escalated"]);
const POLL_MS = 3000;

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

export default function WorkflowDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const workflowId = Number(params.id);

  const [detail, setDetail] = useState<WorkflowRunDetail | null>(null);
  const [events, setEvents] = useState<WorkflowEventPayload[]>([]);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(true);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    if (!Number.isFinite(workflowId)) return;

    let cancelled = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let source: EventSource | null = null;

    async function refreshDetail() {
      try {
        const result = await getWorkflow(workflowId);
        if (cancelled) return;
        setDetail(result);
        if (TERMINAL_STATUSES.has(result.status) && pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
          return;
        }
        toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
      }
    }

    function startPolling() {
      setLive(false);
      if (pollTimer) return;
      pollTimer = setInterval(refreshDetail, POLL_MS);
    }

    refreshDetail().finally(() => {
      if (!cancelled) setLoading(false);
    });

    source = new EventSource(`/api/workflows/${workflowId}/events`);
    source.onmessage = (evt) => {
      try {
        const payload = JSON.parse(evt.data) as WorkflowEventPayload;
        setEvents((prev) => [...prev, payload]);
      } catch {
        // malformed SSE payload - skip this one line, keep the stream open
      }
    };
    source.addEventListener("done", () => {
      refreshDetail();
      source?.close();
    });
    source.onerror = () => {
      // The backend closes the stream itself on a terminal status (via its
      // own "done" event) - a plain error here means the connection broke
      // for some other reason (network hiccup, proxy timeout), so fall back
      // to polling per the brief rather than trying to reconnect a stream
      // that may never resume where it left off.
      source?.close();
      startPolling();
    };

    return () => {
      cancelled = true;
      source?.close();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [workflowId]);

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

      {isTerminal ? (
        <Card className="border-primary/30 bg-muted/40">
          <CardHeader>
            <CardTitle className="text-base">Outcome</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm">
            <p>{finalResponse ?? "This request has finished, but left no final message."}</p>
            {appointment ? (
              <div className="rounded-lg border bg-background p-3">
                <p className="text-xs font-medium text-muted-foreground">Appointment</p>
                <p>
                  {appointment.doctor ?? "—"} · {appointment.department ?? "—"}
                </p>
                <p className="text-xs text-muted-foreground">
                  {appointment.start_time ? new Date(appointment.start_time).toLocaleString() : "—"}
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
