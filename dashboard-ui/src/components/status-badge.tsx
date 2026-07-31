import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

const positive = new Set(["complete", "completed", "succeeded", "success", "approved"])
const negative = new Set(["failed", "error", "blocked", "stopped", "denied", "expired"])
const active = new Set(["running", "executing", "planning", "verifying", "gating"])

export function StatusBadge({ value }: { value?: string }) {
  const state = (value || "unknown").toLowerCase()
  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1.5 rounded-md border-border/80 bg-background/40 px-2 py-1 font-mono text-[10px] uppercase tracking-wider",
        positive.has(state) && "border-emerald-500/25 bg-emerald-500/8 text-emerald-400",
        negative.has(state) && "border-red-500/25 bg-red-500/8 text-red-400",
        active.has(state) && "border-sky-500/25 bg-sky-500/8 text-sky-400",
        state === "paused" && "border-amber-500/25 bg-amber-500/8 text-amber-400",
      )}
    >
      <span className="relative flex size-1.5">
        {active.has(state) && <span className="absolute inline-flex size-full animate-ping rounded-full bg-current opacity-50" />}
        <span className="relative inline-flex size-1.5 rounded-full bg-current" />
      </span>
      {state}
    </Badge>
  )
}
