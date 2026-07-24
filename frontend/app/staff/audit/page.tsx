"use client";

import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError, staffListAudit } from "@/lib/api";
import type { AuditEventOut } from "@/lib/types";

const PAGE_SIZE = 25;

export default function StaffAuditPage() {
  const [events, setEvents] = useState<AuditEventOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [entityTypeInput, setEntityTypeInput] = useState("");
  const [entityTypeFilter, setEntityTypeFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  // No synchronous setLoading(true) here - each handler that changes page
  // or entityTypeFilter below sets it first, matching the pattern used
  // throughout the staff/portal pages (see app/staff/page.tsx's comment).
  useEffect(() => {
    let cancelled = false;
    staffListAudit(page, PAGE_SIZE, entityTypeFilter || undefined)
      .then((result) => {
        if (!cancelled) setEvents(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          toast.error(err instanceof ApiError ? err.message : "Could not load the audit trail");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [page, entityTypeFilter]);

  function handleFilterSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setPage(0);
    setEntityTypeFilter(entityTypeInput.trim());
  }

  function toggleExpanded(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const hasNextPage = events.length === PAGE_SIZE;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Audit trail</CardTitle>
        <CardDescription>Every mutation and agent step, most recent first.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form onSubmit={handleFilterSubmit} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="entity-type-filter">Filter by entity type</Label>
            <Input
              id="entity-type-filter"
              placeholder="e.g. workflow_run, escalation, doctor"
              value={entityTypeInput}
              onChange={(e) => setEntityTypeInput(e.target.value)}
              className="w-64"
            />
          </div>
          <Button type="submit" variant="outline" size="sm">
            Apply
          </Button>
          {entityTypeFilter ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setLoading(true);
                setEntityTypeInput("");
                setEntityTypeFilter("");
                setPage(0);
              }}
            >
              Clear
            </Button>
          ) : null}
        </form>

        {loading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : events.length === 0 ? (
          <p className="text-sm text-muted-foreground">No audit events in this view.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>Actor</TableHead>
                <TableHead>Action</TableHead>
                <TableHead>Entity</TableHead>
                <TableHead>Timestamp</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((event) => {
                const hasMetadata = event.metadata !== null && Object.keys(event.metadata).length > 0;
                const isExpanded = expanded.has(event.id);
                return (
                  <Fragment key={event.id}>
                    <TableRow
                      className={hasMetadata ? "cursor-pointer" : undefined}
                      role={hasMetadata ? "button" : undefined}
                      tabIndex={hasMetadata ? 0 : undefined}
                      onClick={() => hasMetadata && toggleExpanded(event.id)}
                      onKeyDown={(keyEvent) => {
                        if (!hasMetadata) return;
                        if (keyEvent.key === "Enter" || keyEvent.key === " ") {
                          keyEvent.preventDefault();
                          toggleExpanded(event.id);
                        }
                      }}
                    >
                      <TableCell>
                        {hasMetadata ? (
                          isExpanded ? (
                            <ChevronDown className="size-4 text-muted-foreground" />
                          ) : (
                            <ChevronRight className="size-4 text-muted-foreground" />
                          )
                        ) : null}
                      </TableCell>
                      <TableCell>
                        {event.actor_id !== null ? `User #${event.actor_id}` : "System"}
                      </TableCell>
                      <TableCell className="font-mono text-xs">{event.action}</TableCell>
                      <TableCell>
                        {event.entity_type}
                        {event.entity_id !== null ? ` #${event.entity_id}` : ""}
                      </TableCell>
                      <TableCell>{new Date(event.created_at).toLocaleString()}</TableCell>
                    </TableRow>
                    {isExpanded && hasMetadata ? (
                      <TableRow>
                        <TableCell />
                        <TableCell colSpan={4}>
                          <pre className="overflow-x-auto rounded-md bg-muted p-2 text-xs">
                            {JSON.stringify(event.metadata, null, 2)}
                          </pre>
                        </TableCell>
                      </TableRow>
                    ) : null}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        )}

        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">Page {page + 1}</span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page === 0 || loading}
              onClick={() => {
                setLoading(true);
                setPage((p) => Math.max(0, p - 1));
              }}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!hasNextPage || loading}
              onClick={() => {
                setLoading(true);
                setPage((p) => p + 1);
              }}
            >
              Next
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
