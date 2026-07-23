"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useCurrentUser } from "@/hooks/use-current-user";

export default function StaffHomePage() {
  const { user, loading } = useCurrentUser();

  return (
    <Card>
      <CardHeader>
        <CardTitle>{loading ? "Loading..." : `Welcome, ${user?.name ?? "staff"}`}</CardTitle>
        <CardDescription>
          Review incoming requests, resolve escalations, and check the audit trail here.
        </CardDescription>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        The requests queue, escalations queue, audit view and catalog admin land here in the next
        build pass.
      </CardContent>
    </Card>
  );
}
