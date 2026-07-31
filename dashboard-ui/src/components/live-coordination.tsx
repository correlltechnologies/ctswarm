import { useMemo, useState } from "react"
import {
  Activity,
  Bot,
  CheckCircle2,
  CircleDot,
  GitFork,
  Maximize2,
  Minus,
  Plus,
  Radio,
  TriangleAlert,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"
import { compactNumber, formatMs } from "@/lib/api"
import type { InferenceCall, StreamState, TraceNode } from "@/types"

const statusColor: Record<string, string> = {
  running: "var(--primary)",
  complete: "oklch(0.72 0.16 155)",
  completed: "oklch(0.72 0.16 155)",
  succeeded: "oklch(0.72 0.16 155)",
  failed: "var(--destructive)",
  blocked: "oklch(0.72 0.14 85)",
  queued: "var(--muted-foreground)",
  pending: "var(--muted-foreground)",
}

function short(value: string, limit: number) {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value
}

function StreamIndicator({ state, error }: { state: StreamState; error: string }) {
  const live = state === "live"
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant="outline" className={cn("gap-1.5", live && "border-emerald-500/30 bg-emerald-500/8 text-emerald-500")}>
        <span className={cn("size-1.5 rounded-full bg-amber-500", live && "animate-pulse bg-emerald-500 motion-reduce:animate-none")} />
        {live ? "Live stream" : state}
      </Badge>
      <span className="text-xs text-muted-foreground">{error || "AgentField + inference ledger · 2 second cadence"}</span>
    </div>
  )
}

function AgentCard({ node, onOpen }: { node: TraceNode; onOpen: (node: TraceNode) => void }) {
  return (
    <button
      onClick={() => onOpen(node)}
      className="group w-full rounded-lg border bg-background/35 p-4 text-left transition-colors hover:border-primary/35 hover:bg-accent/35 active:bg-accent/60"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="relative flex size-2">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary opacity-40 motion-reduce:animate-none" />
              <span className="relative inline-flex size-2 rounded-full bg-primary" />
            </span>
            <span className="truncate text-sm font-medium">{node.role}</span>
          </div>
          <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">{node.task || "Waiting for task detail"}</p>
        </div>
        <Badge variant="secondary" className="shrink-0 font-mono text-[10px]">{node.phase}</Badge>
      </div>
      <div className="mt-4 grid grid-cols-[1fr_auto] items-end gap-3 border-t pt-3">
        <div className="min-w-0">
          <div className="truncate font-mono text-xs text-primary">{node.resolved_model || node.model || "model pending"}</div>
          <div className="mt-1 truncate text-[10px] text-muted-foreground">{node.harness} · {node.resolved_backend || node.provider}</div>
        </div>
        <span className="font-mono text-[11px] text-muted-foreground">{formatMs(node.duration_ms, node.started_at, node.completed_at)}</span>
      </div>
    </button>
  )
}

function CoordinationGraph({ nodes, onOpen }: { nodes: TraceNode[]; onOpen: (node: TraceNode) => void }) {
  const [status, setStatus] = useState("all")
  const [zoom, setZoom] = useState(1)
  const filtered = useMemo(
    () => status === "all" ? nodes : nodes.filter((node) => node.status.toLowerCase() === status),
    [nodes, status],
  )
  const layout = useMemo(() => {
    const byDepth = new Map<number, TraceNode[]>()
    filtered.forEach((node) => {
      const depth = Number.isFinite(node.depth) ? node.depth : 0
      byDepth.set(depth, [...(byDepth.get(depth) || []), node])
    })
    const maxRows = Math.max(1, ...Array.from(byDepth.values(), (items) => items.length))
    const maxDepth = Math.max(0, ...Array.from(byDepth.keys()))
    const width = Math.max(820, (maxDepth + 1) * 250 + 80)
    const height = Math.max(330, maxRows * 92 + 90)
    const positions = new Map<string, { x: number; y: number }>()
    Array.from(byDepth.entries()).forEach(([depth, items]) => {
      const columnHeight = (items.length - 1) * 92
      items.forEach((node, index) => positions.set(node.execution_id, {
        x: 40 + depth * 250,
        y: 54 + (height - 110 - columnHeight) / 2 + index * 92,
      }))
    })
    return { width, height, positions }
  }, [filtered])

  const statuses = ["all", "running", "complete", "failed"]
  return (
    <Card className="min-w-0 border-border/70 shadow-none">
      <CardHeader className="gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <CardTitle className="flex items-center gap-2 text-base"><GitFork className="size-4" /> Coordination graph</CardTitle>
          <CardDescription>Parent-child execution topology. Select any agent to inspect its input and output.</CardDescription>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button variant="outline" size="icon-sm" aria-label="Zoom out" onClick={() => setZoom((value) => Math.max(.7, value - .15))}><Minus /></Button>
          <Button variant="outline" size="icon-sm" aria-label="Fit graph" onClick={() => setZoom(1)}><Maximize2 /></Button>
          <Button variant="outline" size="icon-sm" aria-label="Zoom in" onClick={() => setZoom((value) => Math.min(1.75, value + .15))}><Plus /></Button>
        </div>
      </CardHeader>
      <CardContent>
        <div className="mb-3 flex flex-wrap gap-1.5">
          {statuses.map((item) => <Button key={item} size="xs" variant={status === item ? "secondary" : "ghost"} onClick={() => setStatus(item)}>{item}</Button>)}
        </div>
        <div className="max-h-[520px] min-h-80 overflow-auto rounded-lg border bg-muted/15">
          {filtered.length ? (
            <svg width={layout.width * zoom} height={layout.height * zoom} viewBox={`0 0 ${layout.width} ${layout.height}`} aria-label="Agent coordination graph">
              <g>
                {filtered.map((node) => {
                  const from = node.parent_execution_id ? layout.positions.get(node.parent_execution_id) : undefined
                  const to = layout.positions.get(node.execution_id)
                  if (!from || !to) return null
                  const startX = from.x + 190
                  const endX = to.x
                  const midX = (startX + endX) / 2
                  return <path key={`edge-${node.execution_id}`} d={`M ${startX} ${from.y + 27} C ${midX} ${from.y + 27}, ${midX} ${to.y + 27}, ${endX} ${to.y + 27}`} fill="none" stroke="var(--border)" strokeWidth="1.5" />
                })}
              </g>
              {filtered.map((node) => {
                const point = layout.positions.get(node.execution_id)!
                const color = statusColor[node.status.toLowerCase()] || "var(--muted-foreground)"
                return (
                  <g key={node.execution_id} transform={`translate(${point.x} ${point.y})`} role="button" tabIndex={0} className="outline-none" onClick={() => onOpen(node)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onOpen(node) }}>
                    <title>{`${node.role}: ${node.task}\n${node.resolved_model || node.model}`}</title>
                    <rect width="190" height="54" rx="8" fill="var(--card)" stroke={color} strokeWidth={node.status === "running" ? 2 : 1} />
                    <circle cx="14" cy="16" r="4" fill={color} />
                    <text x="25" y="20" fill="var(--card-foreground)" fontSize="12" fontWeight="600">{short(node.role || "agent", 22)}</text>
                    <text x="14" y="40" fill="var(--muted-foreground)" fontSize="10">{short(node.task || node.phase, 31)}</text>
                    <text x="174" y="19" textAnchor="end" fill="var(--muted-foreground)" fontSize="9">d{node.depth}</text>
                  </g>
                )
              })}
            </svg>
          ) : <div className="grid h-80 place-items-center text-sm text-muted-foreground">No agents match this status.</div>}
        </div>
      </CardContent>
    </Card>
  )
}

function InferenceFeed({ calls }: { calls: InferenceCall[] }) {
  return (
    <Card className="border-border/70 shadow-none">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base"><Radio className="size-4" /> Inference activity</CardTitle>
        <CardDescription>Newest real backend calls across the swarm. Model aliases remain secondary metadata.</CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        <ScrollArea className="h-[430px]">
          <div className="divide-y">
            {calls.map((call) => (
              <div key={call.id} className="grid grid-cols-[auto_minmax(0,1fr)_auto] gap-3 p-4">
                <div className={cn("mt-0.5 grid size-7 place-items-center rounded-md bg-emerald-500/10 text-emerald-500", !call.ok && "bg-destructive/10 text-destructive")}>{call.ok ? <CheckCircle2 className="size-3.5" /> : <TriangleAlert className="size-3.5" />}</div>
                <div className="min-w-0">
                  <div className="truncate font-mono text-xs font-medium text-primary">{call.model_ref}</div>
                  <div className="mt-1 truncate text-[10px] text-muted-foreground">{call.backend} · {call.role || "harness call"}{call.virtual_model ? ` · ${call.virtual_model}` : ""}</div>
                  <div className="mt-1 text-[10px] text-muted-foreground">{compactNumber(call.prompt_tokens + call.output_tokens)} tokens · {call.latency_ms.toLocaleString()} ms</div>
                </div>
                <time className="font-mono text-[10px] text-muted-foreground">{new Date(call.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}</time>
              </div>
            ))}
            {!calls.length && <div className="p-10 text-center text-sm text-muted-foreground">Waiting for the first inference event.</div>}
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  )
}

export function LiveCoordination({
  nodes,
  calls,
  streamState,
  streamError,
  onOpen,
}: {
  nodes: TraceNode[]
  calls: InferenceCall[]
  streamState: StreamState
  streamError: string
  onOpen: (node: TraceNode) => void
}) {
  const active = nodes.filter((node) => node.status.toLowerCase() === "running")
  const completed = nodes.filter((node) => ["complete", "completed", "succeeded"].includes(node.status.toLowerCase())).length
  const failed = nodes.filter((node) => ["failed", "blocked"].includes(node.status.toLowerCase())).length
  const stats: Array<{ label: string; value: number; icon: typeof Activity; color: string }> = [
    { label: "Working now", value: active.length, icon: Activity, color: "text-primary" },
    { label: "Finished", value: completed, icon: CheckCircle2, color: "text-emerald-500" },
    { label: "Needs attention", value: failed, icon: CircleDot, color: failed ? "text-destructive" : "text-muted-foreground" },
  ]

  return (
    <div className="space-y-4">
      <StreamIndicator state={streamState} error={streamError} />
      <div className="grid gap-3 sm:grid-cols-3">
        {stats.map(({ label, value, icon: Icon, color }) => (
          <Card key={label} className="border-border/70 shadow-none"><CardContent className="flex items-center justify-between p-4"><div><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 text-2xl font-semibold">{value}</div></div><Icon className={cn("size-4", color)} /></CardContent></Card>
        ))}
      </div>
      <Card className="border-border/70 shadow-none">
        <CardHeader><CardTitle className="flex items-center gap-2 text-base"><Bot className="size-4" /> Agents working now</CardTitle><CardDescription>Current task, phase, concrete model, backend, harness, and elapsed time.</CardDescription></CardHeader>
        <CardContent>
          {active.length ? <div className="grid gap-3 lg:grid-cols-2">{active.map((node) => <AgentCard key={node.execution_id} node={node} onOpen={onOpen} />)}</div> : <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">No agent is actively executing. The build may be queued, between phases, or finished.</div>}
        </CardContent>
      </Card>
      <div className="grid min-w-0 gap-4 2xl:grid-cols-[minmax(0,1.7fr)_minmax(340px,.7fr)]">
        <CoordinationGraph nodes={nodes} onOpen={onOpen} />
        <InferenceFeed calls={calls} />
      </div>
    </div>
  )
}
