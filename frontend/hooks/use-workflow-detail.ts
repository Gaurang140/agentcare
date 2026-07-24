"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { ApiError, getWorkflow } from "@/lib/api";
import type { WorkflowEventPayload, WorkflowRunDetail } from "@/lib/types";

const TERMINAL_STATUSES = new Set(["completed", "failed", "escalated"]);
const POLL_MS = 3000;

// Audit actions that change the run's status without ending it, so the
// backend's own "done" event (terminal statuses only) never fires for them:
// the escalate node parking the run at `waiting_approval`, and the staff
// decision that picks it back up. Both arrive as ordinary timeline events,
// and without a re-read here the page would keep showing the status it had
// before the pause for as long as staff take to decide.
const STATUS_CHANGING_ACTIONS = new Set([
  "workflow.waiting_approval",
  "agent.escalate.resolved",
]);

interface UseWorkflowDetailResult {
  detail: WorkflowRunDetail | null;
  events: WorkflowEventPayload[];
  loading: boolean;
  /** true while the SSE connection is the live source; false once a
   * connection error has fallen back to 3s polling. */
  live: boolean;
  notFound: boolean;
}

/** Extracted from the patient workflow detail page (Task 14) so the staff
 * queue's row-click drawer (Task 15) can show the exact same live timeline
 * without duplicating the SSE/polling wiring. `workflowId` of `null` skips
 * the fetch entirely - used when nothing is selected yet (e.g. the staff
 * sheet before a row is clicked). */
export function useWorkflowDetail(workflowId: number | null): UseWorkflowDetailResult {
  const [detail, setDetail] = useState<WorkflowRunDetail | null>(null);
  const [events, setEvents] = useState<WorkflowEventPayload[]>([]);
  const [loading, setLoading] = useState(workflowId !== null);
  const [live, setLive] = useState(true);
  const [notFound, setNotFound] = useState(false);

  // Which workflowId the state above currently belongs to. Comparing and
  // resetting here - during render, guarded so it only fires on an actual
  // change - is React's documented "adjust state when a prop changes"
  // pattern (not a useEffect): it clears a previous run's stale detail/
  // events before paint when the staff drawer jumps straight from one
  // workflow to another without unmounting, and keeps this reset outside
  // react-hooks/set-state-in-effect's reach since there is no effect here.
  const [trackedId, setTrackedId] = useState(workflowId);
  if (workflowId !== trackedId) {
    setTrackedId(workflowId);
    setDetail(null);
    setEvents([]);
    setLoading(workflowId !== null);
    setLive(true);
    setNotFound(false);
  }

  useEffect(() => {
    if (workflowId === null || !Number.isFinite(workflowId)) {
      return;
    }

    let cancelled = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let source: EventSource | null = null;

    async function refreshDetail() {
      try {
        const result = await getWorkflow(workflowId as number);
        if (cancelled) return;
        setDetail(result);
        setLoading(false);
        if (TERMINAL_STATUSES.has(result.status) && pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      } catch (err) {
        if (cancelled) return;
        setLoading(false);
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

    // Fired and not awaited: everything inside runs after the first
    // `await`, so nothing here executes synchronously within this effect.
    refreshDetail();

    source = new EventSource(`/api/workflows/${workflowId}/events`);
    source.onmessage = (evt) => {
      try {
        const payload = JSON.parse(evt.data) as WorkflowEventPayload;
        setEvents((prev) => [...prev, payload]);
        if (STATUS_CHANGING_ACTIONS.has(payload.action)) {
          refreshDetail();
        }
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

  return { detail, events, loading, live, notFound };
}
