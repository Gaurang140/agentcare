"use client";

import { useEffect, useState } from "react";
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

    listSlots(department.id)
      .then(setSlots)
      .catch((err: unknown) => {
        toast.error(err instanceof ApiError ? err.message : "Could not load open slots");
      })
      .finally(() => setSlotsLoading(false));
  }, [rescheduleTarget, departments]);

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
          ) : appointments.length === 0 ? (
            <p className="text-sm text-muted-foreground">No appointments yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Doctor</TableHead>
                  <TableHead>Department</TableHead>
                  <TableHead>Time</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {appointments.map((appt) => {
                  const cancellable = appt.status !== "cancelled";
                  return (
                    <TableRow key={appt.id}>
                      <TableCell>{appt.doctor}</TableCell>
                      <TableCell>{appt.department}</TableCell>
                      <TableCell>
                        {appt.start_time ? new Date(appt.start_time).toLocaleString() : "—"}
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={appt.status} />
                      </TableCell>
                      <TableCell className="max-w-48 truncate">{appt.reason ?? "—"}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-2">
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={!cancellable}
                            onClick={() => openRescheduleDialog(appt)}
                          >
                            Reschedule
                          </Button>
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={!cancellable}
                            onClick={() => setCancelTarget(appt)}
                          >
                            Cancel
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

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
