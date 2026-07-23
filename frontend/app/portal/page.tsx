"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useCurrentUser } from "@/hooks/use-current-user";

export default function PortalHomePage() {
  const { user, loading } = useCurrentUser();

  return (
    <Card>
      <CardHeader>
        <CardTitle>{loading ? "Loading..." : `Welcome, ${user?.name ?? "patient"}`}</CardTitle>
        <CardDescription>
          Submit a request, track its progress, and manage your appointments and documents here.
        </CardDescription>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        Request submission, appointments, documents and reminders land here in the next build pass.
      </CardContent>
    </Card>
  );
}
