"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { StatusBadge } from "@/components/status-badge";
import { useCurrentUser } from "@/hooks/use-current-user";
import { ApiError, listWorkflows, submitRequest } from "@/lib/api";
import type { WorkflowRunSummary } from "@/lib/types";

/** Mirrors backend/app/api/routes_workflows.py::_validate_upload exactly -
 * client-side check is UX only, the backend re-validates on the actual
 * upload regardless. */
const ALLOWED_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg", ".txt"];
const MAX_FILE_BYTES = 10 * 1024 * 1024;

function fileExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot === -1 ? "" : filename.slice(dot).toLowerCase();
}

export default function PortalHomePage() {
  const router = useRouter();
  const { user, loading: userLoading } = useCurrentUser();

  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);

  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    listWorkflows()
      .then((result) => {
        if (!cancelled) setRuns(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          toast.error(err instanceof ApiError ? err.message : "Could not load your requests");
        }
      })
      .finally(() => {
        if (!cancelled) setRunsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []);
    const accepted: File[] = [];
    const rejectedType: string[] = [];
    const rejectedSize: string[] = [];

    for (const file of selected) {
      if (!ALLOWED_EXTENSIONS.includes(fileExtension(file.name))) {
        rejectedType.push(file.name);
        continue;
      }
      if (file.size > MAX_FILE_BYTES) {
        rejectedSize.push(file.name);
        continue;
      }
      accepted.push(file);
    }

    if (rejectedType.length) {
      toast.error(`Unsupported file type: ${rejectedType.join(", ")}`);
    }
    if (rejectedSize.length) {
      toast.error(`File too large, 10MB max: ${rejectedSize.join(", ")}`);
    }
    setFiles(accepted);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!text.trim()) {
      toast.error("Describe what you need first");
      return;
    }

    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("text", text);
      for (const file of files) formData.append("files", file);

      const result = await submitRequest(formData);
      toast.success("Request submitted");
      router.push(`/portal/workflows/${result.workflow_id}`);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>{userLoading ? "Loading..." : `Welcome, ${user?.name ?? "patient"}`}</CardTitle>
          <CardDescription>
            Describe what you need in plain language - book, reschedule, cancel, attach a document -
            and attach any files it depends on. AgentCare routes it to the right agent and shows you
            every step it takes.
          </CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="request-text">Your request</Label>
              <Textarea
                id="request-text"
                placeholder="e.g. I need a cardiology appointment next week"
                required
                value={text}
                onChange={(e) => setText(e.target.value)}
                rows={4}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="request-files">Attach documents (optional)</Label>
              <Input
                id="request-files"
                type="file"
                multiple
                accept={ALLOWED_EXTENSIONS.join(",")}
                onChange={handleFileChange}
              />
              <p className="text-xs text-muted-foreground">
                PDF, PNG, JPG or TXT, up to 10MB each.
              </p>
              {files.length > 0 ? (
                <ul className="text-xs text-muted-foreground">
                  {files.map((file) => (
                    <li key={file.name}>{file.name}</li>
                  ))}
                </ul>
              ) : null}
            </div>
            <Button type="submit" disabled={submitting} className="self-start">
              {submitting ? "Submitting..." : "Submit request"}
            </Button>
          </CardContent>
        </form>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Your recent requests</CardTitle>
          <CardDescription>Click any row to follow its progress.</CardDescription>
        </CardHeader>
        <CardContent>
          {runsLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : runs.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No requests yet - submit one above to get started.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Request</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow
                    key={run.id}
                    className="cursor-pointer"
                    onClick={() => router.push(`/portal/workflows/${run.id}`)}
                  >
                    <TableCell>{run.id}</TableCell>
                    <TableCell className="max-w-xs truncate">{run.request_text}</TableCell>
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
    </div>
  );
}
