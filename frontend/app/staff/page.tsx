"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusBadge } from "@/components/status-badge";
import { WorkflowDetailSheet } from "@/components/workflow-detail-sheet";
import { ApiError, staffListRequests } from "@/lib/api";
import type { WorkflowRunSummary } from "@/lib/types";

/** "all" means no status filter at all (GET /api/staff/requests with no
 * `status` query param) - every other tab maps straight onto the literal
 * status values workflow_service._final_status ever writes
 * (backend/app/services/workflow_service.py). */
const TABS = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "escalated", label: "Escalated" },
  { value: "failed", label: "Failed" },
  { value: "completed", label: "Completed" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

function truncate(text: string, max = 80): string {
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

export default function StaffQueuePage() {
  const [tab, setTab] = useState<TabValue>("all");
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<WorkflowRunSummary | null>(null);

  // No synchronous setLoading(true) here: `loading` already starts true, and
  // the tab-switch handler below sets it again before changing `tab` - the
  // same "set it from the handler that causes the effect to re-run, not
  // from inside the effect" pattern app/portal/appointments/page.tsx uses.
  useEffect(() => {
    let cancelled = false;
    staffListRequests(tab === "all" ? undefined : tab)
      .then((result) => {
        if (!cancelled) setRuns(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          toast.error(err instanceof ApiError ? err.message : "Could not load the request queue");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab]);

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Request queue</CardTitle>
          <CardDescription>
            Every patient request AgentCare has received. Click a row for its live agent timeline.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Tabs
            value={tab}
            onValueChange={(v) => {
              setLoading(true);
              setTab(v as TabValue);
            }}
          >
            <TabsList>
              {TABS.map((t) => (
                <TabsTrigger key={t.value} value={t.value}>
                  {t.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>

          {loading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : runs.length === 0 ? (
            <p className="text-sm text-muted-foreground">No requests in this view.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Patient</TableHead>
                  <TableHead>Request</TableHead>
                  <TableHead>Current step</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow
                    key={run.id}
                    className="cursor-pointer"
                    role="button"
                    tabIndex={0}
                    onClick={() => setSelected(run)}
                    onKeyDown={(keyEvent) => {
                      if (keyEvent.key === "Enter" || keyEvent.key === " ") {
                        keyEvent.preventDefault();
                        setSelected(run);
                      }
                    }}
                  >
                    <TableCell>Patient #{run.patient_id}</TableCell>
                    <TableCell
                      className="max-w-sm truncate"
                      title={run.request_text}
                    >
                      {truncate(run.request_text)}
                    </TableCell>
                    <TableCell>{run.current_step ?? "—"}</TableCell>
                    <TableCell>
                      <StatusBadge status={run.status} />
                    </TableCell>
                    <TableCell>{new Date(run.created_at).toLocaleString()}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <WorkflowDetailSheet
        workflowId={selected?.id ?? null}
        patientId={selected?.patient_id ?? null}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      />
    </div>
  );
}
