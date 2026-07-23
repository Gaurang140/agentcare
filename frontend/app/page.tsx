"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useCurrentUser } from "@/hooks/use-current-user";

/** Root route: no UI of its own, just sends the visitor to the right place
 * once we know whether they're signed in. Client-side by design (see
 * hooks/use-current-user.ts) - real access control stays on the backend. */
export default function HomePage() {
  const router = useRouter();
  const { user, loading } = useCurrentUser();

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    router.replace(user.role === "staff" ? "/staff" : "/portal");
  }, [loading, user, router]);

  return (
    <main className="flex min-h-full flex-1 items-center justify-center p-6">
      <p className="text-sm text-muted-foreground">Loading AgentCare...</p>
    </main>
  );
}
