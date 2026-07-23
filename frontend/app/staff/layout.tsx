"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { NavShell } from "@/components/nav-shell";
import { useCurrentUser } from "@/hooks/use-current-user";

/** A patient landing on /staff would have every staff-only fetch on these
 * pages rejected by the backend's own require_role("staff") (403 on each
 * of routes_staff.py's routes) - this effect turns that into one clean
 * toast + redirect instead of a page full of failed-request toasts. This
 * check is UX only; proxy.ts only gates on cookie presence and the real
 * access control is backend/app/auth/dependencies.py::require_role. */
export default function StaffLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { user, loading } = useCurrentUser();

  useEffect(() => {
    if (!loading && user && user.role !== "staff") {
      toast.error("Staff access only");
      router.replace("/portal");
    }
  }, [loading, user, router]);

  return <NavShell role="staff">{children}</NavShell>;
}
