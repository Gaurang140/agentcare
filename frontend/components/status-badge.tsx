import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** Every status string this app ever shows the patient (workflow runs,
 * appointments, reminders) maps into one of a handful of tones - see each
 * caller for the exact literal values, none of which are enforced by the
 * backend schemas (they only declare `str`, per lib/types.ts's own
 * comment), so this stays a soft, best-effort mapping rather than a
 * switch over a closed union. Anything unrecognized falls back to a plain
 * neutral badge instead of guessing. */
const TONE: Record<string, string> = {
  completed: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  confirmed: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  sent: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  running: "bg-blue-500/15 text-blue-700 dark:text-blue-400",
  pending: "bg-blue-500/15 text-blue-700 dark:text-blue-400",
  waiting_approval: "bg-blue-500/15 text-blue-700 dark:text-blue-400",
  escalated: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  failed: "bg-destructive/15 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
};

export function StatusBadge({ status }: { status: string }) {
  const tone = TONE[status.toLowerCase()] ?? "bg-secondary text-secondary-foreground";
  return (
    <Badge variant="outline" className={cn("border-transparent capitalize", tone)}>
      {status.replace(/_/g, " ")}
    </Badge>
  );
}
