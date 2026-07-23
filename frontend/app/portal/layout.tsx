"use client";

import { NavShell } from "@/components/nav-shell";

export default function PortalLayout({ children }: { children: React.ReactNode }) {
  return <NavShell role="patient">{children}</NavShell>;
}
