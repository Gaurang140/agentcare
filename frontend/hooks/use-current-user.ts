"use client";

import { useEffect, useState } from "react";

import { me } from "@/lib/api";
import type { UserSummary } from "@/lib/types";

interface UseCurrentUserResult {
  user: UserSummary | null;
  /** True while the initial GET /api/auth/me is in flight. */
  loading: boolean;
}

/** Client-side "who am I" hook. The backend is the only source of truth for
 * role (GET /api/auth/me) - this just gives components a convenient,
 * reactive read of it for nav/redirect UX. */
export function useCurrentUser(): UseCurrentUserResult {
  const [user, setUser] = useState<UserSummary | null>(null);
  // Starts true for the initial /me call.
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    me()
      .then((result) => {
        if (!cancelled) {
          setUser(result);
        }
      })
      .catch(() => {
        if (cancelled) return;
        setUser(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { user, loading };
}
