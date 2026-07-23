"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { WorkflowDetailSheet } from "@/components/workflow-detail-sheet";
import { useCurrentUser } from "@/hooks/use-current-user";
import { ApiError, staffListEscalations, staffResolveEscalation } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { EscalationOut } from "@/lib/types";

type FilterValue = "open" | "resolved";

/** severity values agents/prompts.py and app/safety/* ever write
 * (emergency|safety|uncertainty|agent_failure) - anything unrecognized
 * falls back to a plain neutral card rather than guessing, same policy as
 * status-badge.tsx. */
const SEVERITY_STYLE: Record<string, string> = {
  emergency: "border-red-500/40 bg-red-500/5 dark:bg-red-500/10",
  safety: "border-orange-500/40 bg-orange-500/5 dark:bg-orange-500/10",
  uncertainty: "border-amber-500/40 bg-amber-500/5 dark:bg-amber-500/10",
  agent_failure: "border-slate-500/40 bg-slate-500/5 dark:bg-slate-500/10",
};

const SEVERITY_BADGE: Record<string, string> = {
  emergency: "bg-red-500/15 text-red-700 dark:text-red-400",
  safety: "bg-orange-500/15 text-orange-700 dark:text-orange-400",
  uncertainty: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  agent_failure: "bg-slate-500/15 text-slate-700 dark:text-slate-400",
};

interface ResolveTarget {
  escalation: EscalationOut;
  approve: boolean;
}

export default function StaffEscalationsPage() {
  const { user } = useCurrentUser();
  const [filter, setFilter] = useState<FilterValue>("open");
  const [escalations, setEscalations] = useState<EscalationOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [resolveTarget, setResolveTarget] = useState<ResolveTarget | null>(null);
  const [note, setNote] = useState("");
  const [resolving, setResolving] = useState(false);
  const [openWorkflowId, setOpenWorkflowId] = useState<number | null>(null);

  // load() itself never flips loading back to true - the filter-tab handler
  // below does that before switching filter, and the post-resolve call to
  // load() (in handleConfirmResolve) is a deliberately silent background
  // refresh, matching the pattern used across the staff/portal pages.
  function load() {
    const request =
      filter === "open"
        ? staffListEscalations("open")
        : Promise.all([staffListEscalations("approved"), staffListEscalations("rejected")]).then(
            ([approved, rejected]) =>
              [...approved, ...rejected].sort(
                (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
              ),
          );

    request
      .then(setEscalations)
      .catch((err: unknown) => {
        toast.error(err instanceof ApiError ? err.message : "Could not load escalations");
      })
      .finally(() => setLoading(false));
  }

  useEffect(load, [filter]);

  function openResolveDialog(escalation: EscalationOut, approve: boolean) {
    setNote("");
    setResolveTarget({ escalation, approve });
  }

  async function handleConfirmResolve() {
    if (!resolveTarget || !note.trim()) return;
    setResolving(true);
    try {
      await staffResolveEscalation(resolveTarget.escalation.id, resolveTarget.approve, note.trim());
      toast.success(resolveTarget.approve ? "Escalation approved" : "Escalation rejected");
      setResolveTarget(null);
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setResolving(false);
    }
  }

  function reviewerLabel(escalation: EscalationOut): string {
    if (escalation.reviewed_by == null) return "—";
    if (user && escalation.reviewed_by === user.id) return user.name;
    return `Staff #${escalation.reviewed_by}`;
  }

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Escalation inbox</CardTitle>
          <CardDescription>
            Cases an agent handed to a human. Every approval or rejection is recorded with a note.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Tabs
            value={filter}
            onValueChange={(v) => {
              setLoading(true);
              setFilter(v as FilterValue);
            }}
          >
            <TabsList>
              <TabsTrigger value="open">Open</TabsTrigger>
              <TabsTrigger value="resolved">Resolved</TabsTrigger>
            </TabsList>
          </Tabs>

          {loading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : escalations.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {filter === "open" ? "No open escalations." : "Nothing resolved yet."}
            </p>
          ) : (
            <div className="flex flex-col gap-3">
              {escalations.map((esc) => (
                <div
                  key={esc.id}
                  className={cn(
                    "rounded-lg border p-4",
                    SEVERITY_STYLE[esc.severity] ?? "border-border",
                  )}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex flex-col gap-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge
                          variant="outline"
                          className={cn(
                            "border-transparent capitalize",
                            SEVERITY_BADGE[esc.severity] ?? "bg-secondary text-secondary-foreground",
                          )}
                        >
                          {esc.severity.replace(/_/g, " ")}
                        </Badge>
                        {esc.workflow_run_id !== null ? (
                          <button
                            type="button"
                            className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
                            onClick={() => setOpenWorkflowId(esc.workflow_run_id)}
                          >
                            Request #{esc.workflow_run_id}
                          </button>
                        ) : null}
                      </div>
                      <p className="text-sm">{esc.reason}</p>
                      <p className="text-xs text-muted-foreground">
                        Created {new Date(esc.created_at).toLocaleString()}
                      </p>
                    </div>
                    {esc.status === "open" ? (
                      <div className="flex shrink-0 gap-2">
                        <Button size="sm" onClick={() => openResolveDialog(esc, true)}>
                          Approve
                        </Button>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => openResolveDialog(esc, false)}
                        >
                          Reject
                        </Button>
                      </div>
                    ) : (
                      <Badge variant="outline" className="capitalize">
                        {esc.status}
                      </Badge>
                    )}
                  </div>
                  {esc.status !== "open" ? (
                    <div className="mt-3 rounded-md border bg-background p-3 text-sm">
                      <p className="text-xs font-medium text-muted-foreground">
                        Reviewed by {reviewerLabel(esc)} · {esc.status}
                      </p>
                      <p className="mt-1">{esc.resolution_note || "No note left."}</p>
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={resolveTarget !== null}
        onOpenChange={(open) => !open && setResolveTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {resolveTarget?.approve ? "Approve" : "Reject"} this escalation
            </DialogTitle>
            <DialogDescription>
              {resolveTarget?.escalation.reason}. A note is required either way.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="resolution-note">Note</Label>
            <Textarea
              id="resolution-note"
              placeholder="What did you check, and why this decision?"
              required
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={3}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResolveTarget(null)} disabled={resolving}>
              Cancel
            </Button>
            <Button
              onClick={handleConfirmResolve}
              disabled={resolving || !note.trim()}
              variant={resolveTarget?.approve ? "default" : "destructive"}
            >
              {resolving
                ? "Saving..."
                : resolveTarget?.approve
                  ? "Confirm approval"
                  : "Confirm rejection"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <WorkflowDetailSheet
        workflowId={openWorkflowId}
        patientId={null}
        onOpenChange={(open) => {
          if (!open) setOpenWorkflowId(null);
        }}
      />
    </div>
  );
}
