"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusBadge } from "@/components/status-badge";
import { ApiError, listReminders } from "@/lib/api";
import type { ReminderOut } from "@/lib/types";

function formatType(reminderType: string): string {
  return reminderType.replace(/_/g, " ");
}

export default function RemindersPage() {
  const [reminders, setReminders] = useState<ReminderOut[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    listReminders()
      .then((result) => {
        if (!cancelled) setReminders(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          toast.error(err instanceof ApiError ? err.message : "Could not load your reminders");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Your reminders</CardTitle>
        <CardDescription>Appointment reminders and post-visit follow-ups.</CardDescription>
      </CardHeader>
      <CardContent>
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : reminders.length === 0 ? (
          <p className="text-sm text-muted-foreground">No reminders scheduled.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Type</TableHead>
                <TableHead>Scheduled for</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {reminders.map((reminder) => (
                <TableRow key={reminder.id}>
                  <TableCell className="capitalize">{formatType(reminder.reminder_type)}</TableCell>
                  <TableCell>{new Date(reminder.scheduled_at).toLocaleString()}</TableCell>
                  <TableCell>
                    <StatusBadge status={reminder.sent ? "sent" : "pending"} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
