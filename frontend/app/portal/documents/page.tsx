"use client";

import { useEffect, useState } from "react";
import { CircleCheck, TriangleAlert } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ApiError, listDepartments, listDocuments } from "@/lib/api";
import type { DepartmentOut, DocumentMeta } from "@/lib/types";

function formatType(documentType: string): string {
  return documentType.replace(/_/g, " ");
}

interface DepartmentRequirement {
  department: DepartmentOut;
  required: string[];
  missing: string[];
}

export default function DocumentsPage() {
  const [documents, setDocuments] = useState<DocumentMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [requirements, setRequirements] = useState<DepartmentRequirement[]>([]);
  const [requirementsLoading, setRequirementsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    listDocuments()
      .then((result) => {
        if (!cancelled) setDocuments(result.documents);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          toast.error(err instanceof ApiError ? err.message : "Could not load your documents");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    listDepartments()
      .then(async (departments) => {
        // GET /api/documents only returns present/missing requirement
        // badges for one department_id at a time (routes_documents.py), so
        // building a "missing by department" card means one call per
        // department - a handful of rows in the seeded catalog, so this
        // stays cheap.
        const perDepartment = await Promise.all(
          departments.map(async (department) => {
            const result = await listDocuments({ department_id: department.id });
            const required = (result.requirements?.required as string[] | undefined) ?? [];
            const missing = (result.requirements?.missing as string[] | undefined) ?? [];
            return { department, required, missing };
          }),
        );
        if (!cancelled) {
          setRequirements(perDepartment.filter((row) => row.required.length > 0));
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          toast.error(
            err instanceof ApiError ? err.message : "Could not check document requirements",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setRequirementsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Missing documents</CardTitle>
          <CardDescription>By department, based on what departments require and what you&apos;ve already uploaded.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {requirementsLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : requirements.length === 0 ? (
            <p className="text-sm text-muted-foreground">No department has document requirements on file.</p>
          ) : (
            requirements.map((row) => (
              <div key={row.department.id} className="rounded-lg border p-3">
                <p className="text-sm font-medium">{row.department.name}</p>
                {row.missing.length === 0 ? (
                  <p className="mt-1 flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-400">
                    <CircleCheck className="size-3.5" /> All required documents on file
                  </p>
                ) : (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {row.missing.map((type) => (
                      <Badge
                        key={type}
                        variant="outline"
                        className="gap-1 border-transparent bg-amber-500/15 text-amber-700 dark:text-amber-400"
                      >
                        <TriangleAlert className="size-3" />
                        {formatType(type)}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            ))
          )}
        </CardContent>
      </Card>

      {/* No "duplicate" column: backend/app/tools/document_tools.py::store_document
          resolves a byte-identical re-upload by (patient_id, checksum) at
          write time and returns the *existing* row's id instead of
          inserting a second one - so a duplicate never gets its own row
          here, and neither the checksum nor a duplicate flag is exposed by
          any endpoint (DocumentMeta has neither; POST /api/requests only
          returns {workflow_id, status}). Faking a badge with no real signal
          behind it would violate this repo's no-invented-output rule, so
          this table only shows what GET /api/documents actually returns. */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Your documents</CardTitle>
          <CardDescription>Everything you&apos;ve uploaded so far.</CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : documents.length === 0 ? (
            <p className="text-sm text-muted-foreground">No documents uploaded yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Filename</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Uploaded</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {documents.map((doc) => (
                  <TableRow key={doc.id}>
                    <TableCell className="max-w-64 truncate">{doc.filename}</TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="capitalize">
                        {formatType(doc.document_type)}
                      </Badge>
                    </TableCell>
                    <TableCell>{new Date(doc.created_at).toLocaleString()}</TableCell>
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
