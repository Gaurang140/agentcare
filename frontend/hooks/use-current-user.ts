"use client";

import { useEffect, useState } from "react";

import { ApiError, me } from "@/lib/api";
import type { UserSummary } from "@/lib/types";

interface UseCurrentUserResult {
  user: UserSummary | null;
  /** True while the initial GET /api/auth/me is in flight. */
  loading: boolean;
  /** Set when /me failed - a 401 (not logged in) is the common case, not
   * necessarily a real error, so check `error.status` before showing it. */
  error: ApiError | null;
  refresh: () => void;
}

/** Client-side "who am I" hook. The backend is the only source of truth for
 * role (GET /api/auth/me) - this just gives components a convenient,
 * reactive read of it for nav/redirect UX. */
export function useCurrentUser(): UseCurrentUserResult {
  const [user, setUser] = useState<UserSummary | null>(null);
  // Starts true for the initial /me call; a manual refresh() intentionally
  // does not flip this back on (setState synchronously inside an effect
  // body just to reset a flag trips react-hooks/set-state-in-effect, and a
  // silent background refetch is the better UX here anyway).
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    me()
      .then((result) => {
        if (!cancelled) {
          setUser(result);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setUser(null);
        setError(err instanceof ApiError ? err : new ApiError(0, "network_error", "Could not reach the server"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  return { user, loading, error, refresh: () => setNonce((n) => n + 1) };
}
