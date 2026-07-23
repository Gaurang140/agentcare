"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { cn } from "@/lib/utils";
import { logout } from "@/lib/api";

interface NavShellProps {
  /** Distinguishes the patient portal shell from the staff console shell -
   * purely a label/redirect-target choice, never an access check (the
   * backend enforces role on every route this shell links to). */
  role: "patient" | "staff";
  children: React.ReactNode;
}

const PATIENT_LINKS = [
  { href: "/portal", label: "Requests" },
  { href: "/portal/appointments", label: "Appointments" },
  { href: "/portal/documents", label: "Documents" },
  { href: "/portal/reminders", label: "Reminders" },
];

const STAFF_LINKS = [
  { href: "/staff", label: "Queue" },
  { href: "/staff/escalations", label: "Escalations" },
  { href: "/staff/audit", label: "Audit" },
  { href: "/staff/catalog", label: "Catalog" },
];

/** Minimal shared header for the /portal and /staff sections: brand, the
 * signed-in user's name and role badge, sign-out, and each section's own
 * nav links. These links are plain <Link>s to routes the backend itself
 * gates by role; nothing here is an access check - see proxy.ts and
 * backend/app/auth/dependencies.py for the real enforcement. */
export function NavShell({ role, children }: NavShellProps) {
  const router = useRouter();
  const pathname = usePathname();
  const { user } = useCurrentUser();
  const links = role === "staff" ? STAFF_LINKS : PATIENT_LINKS;

  async function handleLogout() {
    try {
      await logout();
    } catch {
      // logout clears the cookie server-side even if this throws for some
      // other reason; still send the user to /login either way.
    }
    toast.success("Signed out");
    router.push("/login");
    router.refresh();
  }

  return (
    <div className="flex min-h-full flex-1 flex-col">
      <header className="flex flex-col gap-3 border-b px-6 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <span className="text-base font-semibold">AgentCare</span>
          <Badge variant="secondary" className="capitalize">
            {role === "staff" ? "Staff console" : "Patient portal"}
          </Badge>
          <nav className="flex items-center gap-1">
            {links.map((link) => {
              const home = role === "staff" ? "/staff" : "/portal";
              const active = link.href === home ? pathname === home : pathname.startsWith(link.href);
              return (
                <Link
                  key={link.href}
                  href={link.href}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-sm transition-colors",
                    active
                      ? "bg-muted font-medium text-foreground"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  {link.label}
                </Link>
              );
            })}
          </nav>
        </div>
        <div className="flex items-center gap-3">
          {user ? <span className="text-sm text-muted-foreground">{user.name}</span> : null}
          <Button variant="outline" size="sm" onClick={handleLogout}>
            Sign out
          </Button>
        </div>
      </header>
      <main className="flex flex-1 flex-col p-6">{children}</main>
    </div>
  );
}
