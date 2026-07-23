"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  ApiError,
  listDepartments,
  staffCreateDepartment,
  staffCreateDoctor,
  staffGenerateSlots,
  staffListDoctors,
  staffSetDoctorActive,
} from "@/lib/api";
import type { DepartmentOut, DoctorOut, SlotOut } from "@/lib/types";

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function daysFromTodayIso(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

export default function StaffCatalogPage() {
  const [departments, setDepartments] = useState<DepartmentOut[]>([]);
  const [departmentsLoading, setDepartmentsLoading] = useState(true);
  const [doctors, setDoctors] = useState<DoctorOut[]>([]);
  const [doctorsLoading, setDoctorsLoading] = useState(true);

  const [deptName, setDeptName] = useState("");
  const [deptDescription, setDeptDescription] = useState("");
  const [creatingDept, setCreatingDept] = useState(false);

  const [doctorName, setDoctorName] = useState("");
  const [doctorDeptId, setDoctorDeptId] = useState<string>("");
  const [creatingDoctor, setCreatingDoctor] = useState(false);
  const [togglingDoctorId, setTogglingDoctorId] = useState<number | null>(null);

  const [slotDoctorId, setSlotDoctorId] = useState<string>("");
  const [slotDateFrom, setSlotDateFrom] = useState(todayIso());
  const [slotDateTo, setSlotDateTo] = useState(daysFromTodayIso(7));
  const [generating, setGenerating] = useState(false);
  const [generatedSlots, setGeneratedSlots] = useState<SlotOut[] | null>(null);

  // Neither loader resets its *Loading flag back to true: both start true
  // via useState already (the first paint), and every later call - the
  // mount effect below, plus every create/toggle handler - is a silent
  // background refresh rather than a flashed loading state, matching the
  // pattern used across the rest of the staff/portal pages.
  function loadDepartments() {
    listDepartments()
      .then(setDepartments)
      .catch((err: unknown) => {
        toast.error(err instanceof ApiError ? err.message : "Could not load departments");
      })
      .finally(() => setDepartmentsLoading(false));
  }

  function loadDoctors() {
    staffListDoctors()
      .then(setDoctors)
      .catch((err: unknown) => {
        toast.error(err instanceof ApiError ? err.message : "Could not load doctors");
      })
      .finally(() => setDoctorsLoading(false));
  }

  useEffect(() => {
    loadDepartments();
    loadDoctors();
  }, []);

  const departmentName = (id: number) => departments.find((d) => d.id === id)?.name ?? `#${id}`;

  async function handleCreateDepartment(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!deptName.trim()) return;
    setCreatingDept(true);
    try {
      await staffCreateDepartment({ name: deptName.trim(), description: deptDescription.trim() || null });
      toast.success("Department created");
      setDeptName("");
      setDeptDescription("");
      loadDepartments();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setCreatingDept(false);
    }
  }

  async function handleCreateDoctor(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!doctorName.trim() || !doctorDeptId) return;
    setCreatingDoctor(true);
    try {
      await staffCreateDoctor({ department_id: Number(doctorDeptId), name: doctorName.trim() });
      toast.success("Doctor created");
      setDoctorName("");
      loadDoctors();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setCreatingDoctor(false);
    }
  }

  async function handleToggleDoctor(doctor: DoctorOut) {
    setTogglingDoctorId(doctor.id);
    try {
      await staffSetDoctorActive(doctor.id, !doctor.active);
      toast.success(doctor.active ? "Doctor deactivated" : "Doctor activated");
      loadDoctors();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setTogglingDoctorId(null);
    }
  }

  async function handleGenerateSlots(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!slotDoctorId || !slotDateFrom || !slotDateTo) return;
    setGenerating(true);
    setGeneratedSlots(null);
    try {
      const result = await staffGenerateSlots({
        doctor_id: Number(slotDoctorId),
        date_from: slotDateFrom,
        date_to: slotDateTo,
      });
      setGeneratedSlots(result);
      toast.success(`Generated ${result.length} slot${result.length === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not reach the server");
    } finally {
      setGenerating(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Departments</CardTitle>
          <CardDescription>
            The hospital catalog. Department has no active flag in the data model - doctors do (see
            the Doctors card below) - so this card is list + create, not a toggle.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {departmentsLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Description</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {departments.map((dept) => (
                  <TableRow key={dept.id}>
                    <TableCell>{dept.name}</TableCell>
                    <TableCell className="text-muted-foreground">{dept.description ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          <form onSubmit={handleCreateDepartment} className="flex flex-wrap items-end gap-2 border-t pt-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="dept-name">New department name</Label>
              <Input
                id="dept-name"
                value={deptName}
                onChange={(e) => setDeptName(e.target.value)}
                placeholder="e.g. Neurology"
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="dept-description">Description (optional)</Label>
              <Input
                id="dept-description"
                value={deptDescription}
                onChange={(e) => setDeptDescription(e.target.value)}
                placeholder="optional"
              />
            </div>
            <Button type="submit" disabled={creatingDept || !deptName.trim()}>
              {creatingDept ? "Creating..." : "Add department"}
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Doctors</CardTitle>
          <CardDescription>Toggle a doctor inactive to stop them coming up as bookable.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {doctorsLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Department</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {doctors.map((doctor) => (
                  <TableRow key={doctor.id}>
                    <TableCell>{doctor.name}</TableCell>
                    <TableCell>{departmentName(doctor.department_id)}</TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className={
                          doctor.active
                            ? "border-transparent bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                            : "border-transparent bg-muted text-muted-foreground"
                        }
                      >
                        {doctor.active ? "Active" : "Inactive"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={togglingDoctorId === doctor.id}
                        onClick={() => handleToggleDoctor(doctor)}
                      >
                        {togglingDoctorId === doctor.id
                          ? "Saving..."
                          : doctor.active
                            ? "Deactivate"
                            : "Activate"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          <form onSubmit={handleCreateDoctor} className="flex flex-wrap items-end gap-2 border-t pt-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="doctor-name">New doctor name</Label>
              <Input
                id="doctor-name"
                value={doctorName}
                onChange={(e) => setDoctorName(e.target.value)}
                placeholder="e.g. Dr. Jane Doe"
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="doctor-department">Department</Label>
              <Select value={doctorDeptId} onValueChange={setDoctorDeptId}>
                <SelectTrigger id="doctor-department" className="w-48">
                  <SelectValue placeholder="Choose department" />
                </SelectTrigger>
                <SelectContent>
                  {departments.map((dept) => (
                    <SelectItem key={dept.id} value={String(dept.id)}>
                      {dept.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button type="submit" disabled={creatingDoctor || !doctorName.trim() || !doctorDeptId}>
              {creatingDoctor ? "Creating..." : "Add doctor"}
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Slots</CardTitle>
          <CardDescription>Generate bookable 30-minute slots for a doctor over a date range.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <form onSubmit={handleGenerateSlots} className="flex flex-wrap items-end gap-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="slot-doctor">Doctor</Label>
              <Select value={slotDoctorId} onValueChange={setSlotDoctorId}>
                <SelectTrigger id="slot-doctor" className="w-56">
                  <SelectValue placeholder="Choose doctor" />
                </SelectTrigger>
                <SelectContent>
                  {doctors.map((doctor) => (
                    <SelectItem key={doctor.id} value={String(doctor.id)}>
                      {doctor.name} · {departmentName(doctor.department_id)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="slot-from">From</Label>
              <Input
                id="slot-from"
                type="date"
                value={slotDateFrom}
                onChange={(e) => setSlotDateFrom(e.target.value)}
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="slot-to">To</Label>
              <Input
                id="slot-to"
                type="date"
                value={slotDateTo}
                onChange={(e) => setSlotDateTo(e.target.value)}
                required
              />
            </div>
            <Button type="submit" disabled={generating || !slotDoctorId}>
              {generating ? "Generating..." : "Generate slots"}
            </Button>
          </form>

          {generatedSlots !== null ? (
            generatedSlots.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No slots were created (they may already exist for this range).
              </p>
            ) : (
              <div className="flex flex-col gap-2">
                <p className="text-sm text-muted-foreground">
                  {generatedSlots.length} slot{generatedSlots.length === 1 ? "" : "s"} for{" "}
                  {generatedSlots[0].doctor}:
                </p>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Start</TableHead>
                      <TableHead>End</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {generatedSlots.slice(0, 10).map((slot) => (
                      <TableRow key={slot.slot_id}>
                        <TableCell>{new Date(slot.start_time).toLocaleString()}</TableCell>
                        <TableCell>{new Date(slot.end_time).toLocaleString()}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {generatedSlots.length > 10 ? (
                  <p className="text-xs text-muted-foreground">
                    Showing the first 10 of {generatedSlots.length}.
                  </p>
                ) : null}
              </div>
            )
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
