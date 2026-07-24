"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Bell,
  CalendarCheck,
  ChevronDown,
  ChevronRight,
  Circle,
  FileText,
  Hourglass,
  PlayCircle,
  RotateCw,
  Route,
  ShieldAlert,
  ShieldCheck,
  Siren,
  UserCheck,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import type { WorkflowEventPayload } from "@/lib/types";

interface ActionDisplay {
  label: string;
  Icon: LucideIcon;
}

/** action strings this app ever writes for entity_type "workflow_run" -
 * see backend/app/services/workflow_service.py, agents/graph.py and each
 * agents/<name>.py::run's own write_audit call. Agent names come out of
 * the `agent.<name>.completed` pattern; the rest are matched by exact
 * string, which also wins over that pattern where the two overlap.
 * Unrecognized strings still render (the action text itself is always
 * shown), just with a plain fallback icon rather than a guess. */
const AGENT_META: Record<string, ActionDisplay> = {
  coordinator: { label: "Coordinator", Icon: Workflow },
  routing: { label: "Routing", Icon: Route },
  appointment: { label: "Appointment", Icon: CalendarCheck },
  document: { label: "Document", Icon: FileText },
  followup: { label: "Follow-up", Icon: Bell },
  safety: { label: "Safety", Icon: ShieldCheck },
  // No "escalate" entry: that node's exit row is spelled out in
  // WORKFLOW_META below, which is read first.
};

const WORKFLOW_META: Record<string, ActionDisplay> = {
  "workflow.started": { label: "Request started", Icon: PlayCircle },
  "workflow.resumed": { label: "Workflow resumed", Icon: RotateCw },
  "workflow.escalated_emergency": { label: "Emergency escalation", Icon: Siren },
  "workflow.refused_medical": { label: "Refused (medical question)", Icon: ShieldAlert },
  "workflow.waiting_approval": { label: "Waiting for staff approval", Icon: Hourglass },
  "agent.escalate.resolved": { label: "Staff decision recorded", Icon: UserCheck },
  // The escalate node's own exit row. It fits the agent pattern by name
  // only: the node hands the case to a person, so "Escalation agent
  // completed" would read as the opposite of what happened.
  "agent.escalate.completed": { label: "Handed to staff", Icon: AlertTriangle },
};

function describeAction(action: string): ActionDisplay {
  const exact = WORKFLOW_META[action];
  if (exact) return exact;
  const agentMatch = /^agent\.([a-z_]+)\.completed$/.exec(action);
  if (agentMatch) {
    const meta = AGENT_META[agentMatch[1]];
    if (meta) return { label: `${meta.label} agent completed`, Icon: meta.Icon };
  }
  return { label: action, Icon: Circle };
}

function formatTimestamp(value: string | null): string {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString();
}

function TimelineRow({ event }: { event: WorkflowEventPayload }) {
  const [expanded, setExpanded] = useState(false);
  const { label, Icon } = describeAction(event.action);
  const hasMetadata = event.metadata !== null && Object.keys(event.metadata).length > 0;

  return (
    <li className="flex items-start gap-3 rounded-lg border p-3">
      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-medium">{label}</span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {formatTimestamp(event.created_at)}
          </span>
        </div>
        <div className="font-mono text-xs text-muted-foreground">{event.action}</div>
        {hasMetadata ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="mt-1 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
            details
          </button>
        ) : null}
        {expanded && hasMetadata ? (
          <pre className="mt-1 overflow-x-auto rounded-md bg-muted p-2 text-xs">
            {JSON.stringify(event.metadata, null, 2)}
          </pre>
        ) : null}
      </div>
    </li>
  );
}

interface WorkflowTimelineProps {
  events: WorkflowEventPayload[];
  /** Shown above an empty list while the request is still fresh - "no
   * steps yet" reads better than a blank box. */
  emptyLabel?: string;
}

/** Renders the workflow's audit-event timeline: one row per step, newest
 * at the bottom, auto-scrolled into view as events arrive. Readable, not
 * fancy, per the brief - this is the demo centerpiece. */
export function WorkflowTimeline({ events, emptyLabel = "Waiting for the first step..." }: WorkflowTimelineProps) {
  const containerRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  if (events.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyLabel}</p>;
  }

  return (
    <ul ref={containerRef} className="flex max-h-96 flex-col gap-2 overflow-y-auto pr-1">
      {events.map((event) => (
        <TimelineRow key={event.id} event={event} />
      ))}
    </ul>
  );
}
