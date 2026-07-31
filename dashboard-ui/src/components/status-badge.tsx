import { cn } from "@/lib/utils"

const positive = new Set(["complete", "completed", "succeeded", "success", "approved"])
const negative = new Set(["failed", "error", "blocked", "stopped", "denied", "expired"])
const active = new Set(["running", "executing", "planning", "verifying", "gating"])

export function StatusBadge({ value }: { value?: string }) {
  const state = (value || "unknown").toLowerCase()
  return <span className={cn("inline-flex shrink-0 items-center gap-1.5 font-mono text-[10px] uppercase tracking-[.1em] text-muted-foreground", positive.has(state) && "text-emerald-700 dark:text-emerald-400", negative.has(state) && "text-destructive", active.has(state) && "text-foreground", state === "paused" && "text-amber-700 dark:text-amber-400")}><span className="size-1.5 rounded-[2px] bg-current" />{state}</span>
}
