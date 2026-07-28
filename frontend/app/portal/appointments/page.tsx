"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { toast } from "sonner";

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusBadge } from "@/components/status-badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  cancelAppointment,
  listAppointments,
  listDepartments,
  listSlots,
  rescheduleAppointment,
} from "@/lib/api";
import type { AppointmentOut, DepartmentOut, SlotOut } from "@/lib/types";

function formatSlot(slot: SlotOut): string {
  const start = new Date(slot.start_time);
  return `${start.toLocaleString()} · ${slot.doctor}`;
}

function isoDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function initialDateRange(): { from: string; to: string } {
  const from = new Date();
  const to = new Date();
  to.setDate(to.getDate() + 14);
  return { from: isoDate(from), to: isoDate(to) };
}

function formatAppointmentTime(appt: AppointmentOut): string {
  if (!appt.start_time) return "Not scheduled";
  const start = new Date(appt.start_time);
  if (!appt.end_time) return start.toLocaleString();
  const end = new Date(appt.end_time);
  return `${start.toLocaleString()} – ${end.toLocaleTimeString()}`;
}

export default function AppointmentsPage() {
  const [appointments, setAppointments] = useState<AppointmentOut[]>([]);
  const [departments, setDepartments] = useState<DepartmentOut[]>([]);
  const [loading, setLoading] = useState(true);

  const [cancelTarget, setCancelTarget] = useState<AppointmentOut | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const [rescheduleTarget, setRescheduleTarget] = useState<AppointmentOut | null>(null);
  const [slots, setSlots] = useState<SlotOut[]>([]);
  const [slotsLoading, setSlotsLoading] = useState(false);
  const [selectedSlotId, setSelectedSlotId] = useState<string>("");
  const [rescheduling, setRescheduling] = useState(false);
  const [rescheduleDates, setRescheduleDates] = useState(initialDateRange);

  // No synchronous setLoading(true) here: `loading` already starts true, and
  // a post-mutation reload (called from the confirm handlers below, not an
  // effect) intentionally leaves the table visible while it quietly
  // refetches rather than flashing a loading state again.
  function loadAppointments() {
    listAppointments()
      .then(setAppointments)
      .catch((err: unknown) => {
        toast.error(err instanceof ApiError ? err.message : "Could not load your appointments");
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    loadAppointments();
    listDepartments()
      .then(setDepartments)
      .catch(() => {
        // departments only feed the reschedule dialog's slot lookup - a
        // failure here surfaces when that dialog can't find a match, not
        // as a toast on a page load that otherwise succeeded.
      });
  }, []);

  // Only fetches, never a synchronous setState of its own: `slotsLoading`
  // flips true in openRescheduleDialog below (the click handler that sets
  // rescheduleTarget in the first place, not this effect) and closing the
  // dialog resets everything in closeRescheduleDialog - this effect's own
  // body only ever calls a state setter inside a promise callback.
  useEffect(() => {
    if (!rescheduleTarget) return;
    const department = departments.find((d) => d.name === rescheduleTarget.department);
    if (!department) return;

    listSlots(department.id, {
      date_from: rescheduleDates.from,
      date_to: rescheduleDates.to,
      limit: 100,
    })
      .then(setSlots)
      .catch((err: unknown) => {
        toast.error(err instanceof ApiError ? err.message : "Could not load open slots");
      })
      .finally(() => setSlotsLoading(false));
  }, [rescheduleTarget, departments, rescheduleDates]);

  function openRescheduleDialog(appt: AppointmentOut) {
    setSelectedSlotId("");
    setSlotsLoading(true);
    setRescheduleTarget(appt);
  }

  function closeRescheduleDialog() {
    setRescheduleTarget(null);
    setSlots([]);
    setSelectedSlotId("");
  }

  async function handleConfirmCancel() {
    if (!cancelTarget) return;
    setCancelling(true);
    try {
      await cancelAppointment(cancelTarget.id);
      toast.success("Appointment cancelled");
      setCancelTarget(null);
      loadAppointments();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setCancelling(false);
    }
  }

  async function handleConfirmReschedule() {
    if (!rescheduleTarget || !selectedSlotId) return;
    setRescheduling(true);
    try {
      await rescheduleAppointment(rescheduleTarget.id, { new_slot_id: Number(selectedSlotId) });
      toast.success("Appointment rescheduled");
      closeRescheduleDialog();
      loadAppointments();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setRescheduling(false);
    }
  }

  const rescheduleDepartmentFound =
    !rescheduleTarget || departments.some((d) => d.name === rescheduleTarget.department);
  const activeAppointments = appointments.filter((appointment) =>
    ["pending", "confirmed"].includes(appointment.status),
  );
  const appointmentHistory = appointments.filter(
    (appointment) => !["pending", "confirmed"].includes(appointment.status),
  );

  function appointmentRows(rows: AppointmentOut[], actions: boolean) {
    return (
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Appointment</TableHead>
            <TableHead>Schedule</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Request details</TableHead>
            {actions ? <TableHead className="text-right">Actions</TableHead> : null}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((appt) => (
            <TableRow key={appt.id}>
              <TableCell>
                <p className="font-medium">{appt.doctor}</p>
                <p className="text-xs text-muted-foreground">{appt.department}</p>
              </TableCell>
              <TableCell>
                <p>{formatAppointmentTime(appt)}</p>
                <p className="text-xs text-muted-foreground">
                  Booked {new Date(appt.created_at).toLocaleString()}
                </p>
              </TableCell>
              <TableCell>
                <StatusBadge status={appt.status} />
              </TableCell>
              <TableCell className="max-w-md whitespace-normal">
                <p>{appt.reason ?? "No reason recorded"}</p>
                {appt.workflow_id ? (
                  <Link
                    href={`/portal/workflows/${appt.workflow_id}`}
                    className="text-xs font-medium underline underline-offset-4"
                  >
                    View request #{appt.workflow_id}
                  </Link>
                ) : null}
              </TableCell>
              {actions ? (
                <TableCell className="text-right">
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => openRescheduleDialog(appt)}
                    >
                      Reschedule
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => setCancelTarget(appt)}
                    >
                      Cancel
                    </Button>
                  </div>
                </TableCell>
              ) : null}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Your appointments</CardTitle>
          <CardDescription>Cancel or reschedule any appointment that is still confirmed.</CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : activeAppointments.length === 0 ? (
            <p className="text-sm text-muted-foreground">No appointments yet.</p>
          ) : (
            appointmentRows(activeAppointments, true)
          )}
        </CardContent>
      </Card>

      {appointmentHistory.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Appointment history</CardTitle>
            <CardDescription>
              Cancelled and completed appointments remain visible for context.
            </CardDescription>
          </CardHeader>
          <CardContent>{appointmentRows(appointmentHistory, false)}</CardContent>
        </Card>
      ) : null}

      <Dialog open={cancelTarget !== null} onOpenChange={(open) => !open && setCancelTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel this appointment?</DialogTitle>
            <DialogDescription>
              {cancelTarget
                ? `${cancelTarget.doctor} · ${cancelTarget.department} - this cannot be undone.`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCancelTarget(null)} disabled={cancelling}>
              Keep appointment
            </Button>
            <Button variant="destructive" onClick={handleConfirmCancel} disabled={cancelling}>
              {cancelling ? "Cancelling..." : "Cancel appointment"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={rescheduleTarget !== null}
        onOpenChange={(open) => !open && closeRescheduleDialog()}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reschedule this appointment</DialogTitle>
            <DialogDescription>
              {rescheduleTarget
                ? `Pick a new open slot in ${rescheduleTarget.department}.`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid gap-1.5">
              <Label htmlFor="reschedule-from">From</Label>
              <Input
                id="reschedule-from"
                type="date"
                value={rescheduleDates.from}
                onChange={(event) => {
                  setSelectedSlotId("");
                  setSlotsLoading(true);
                  setRescheduleDates((current) => ({
                    ...current,
                    from: event.target.value,
                  }));
                }}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="reschedule-to">To</Label>
              <Input
                id="reschedule-to"
                type="date"
                value={rescheduleDates.to}
                onChange={(event) => {
                  setSelectedSlotId("");
                  setSlotsLoading(true);
                  setRescheduleDates((current) => ({
                    ...current,
                    to: event.target.value,
                  }));
                }}
              />
            </div>
          </div>
          {!rescheduleDepartmentFound ? (
            <p className="text-sm text-muted-foreground">
              Could not match this appointment&apos;s department to the catalog.
            </p>
          ) : slotsLoading ? (
            <p className="text-sm text-muted-foreground">Loading open slots...</p>
          ) : slots.length === 0 ? (
            <p className="text-sm text-muted-foreground">No open slots in this department right now.</p>
          ) : (
            <Select value={selectedSlotId} onValueChange={setSelectedSlotId}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Choose a slot" />
              </SelectTrigger>
              <SelectContent>
                {slots.map((slot) => (
                  <SelectItem key={slot.slot_id} value={String(slot.slot_id)}>
                    {formatSlot(slot)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={closeRescheduleDialog} disabled={rescheduling}>
              Cancel
            </Button>
            <Button
              onClick={handleConfirmReschedule}
              disabled={rescheduling || !selectedSlotId}
            >
              {rescheduling ? "Rescheduling..." : "Confirm new slot"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
