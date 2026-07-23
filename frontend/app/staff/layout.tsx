"use client";

import { NavShell } from "@/components/nav-shell";

export default function StaffLayout({ children }: { children: React.ReactNode }) {
  return <NavShell role="staff">{children}</NavShell>;
}
