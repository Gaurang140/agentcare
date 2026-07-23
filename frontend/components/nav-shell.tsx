"use client";

import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-current-user";
import { logout } from "@/lib/api";

interface NavShellProps {
  /** Distinguishes the patient portal shell from the staff console shell -
   * purely a label/redirect-target choice, never an access check (the
   * backend enforces role on every route this shell links to). */
  role: "patient" | "staff";
  children: React.ReactNode;
}

/** Minimal shared header for the /portal and /staff sections: brand, the
 * signed-in user's name and role badge, and sign-out. Section-specific nav
 * links get added here as later tasks build out real portal/staff pages. */
export function NavShell({ role, children }: NavShellProps) {
  const router = useRouter();
  const { user } = useCurrentUser();

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
      <header className="flex items-center justify-between border-b px-6 py-3">
        <div className="flex items-center gap-3">
          <span className="text-base font-semibold">AgentCare</span>
          <Badge variant="secondary" className="capitalize">
            {role === "staff" ? "Staff console" : "Patient portal"}
          </Badge>
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
