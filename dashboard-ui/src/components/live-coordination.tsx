import { useMemo, useState } from "react"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { compactNumber, formatMs } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { InferenceCall, StreamState, TraceNode } from "@/types"
import { StatusBadge } from "./status-badge"

const stages = [
  { key: "planning", label: "Plan", detail: "Scope and acceptance" },
  { key: "implementation", label: "Build", detail: "Production changes" },
  { key: "review", label: "Review", detail: "Independent inspection" },
  { key: "verification", label: "Verify", detail: "Tests and user flows" },
  { key: "integration", label: "Accept", detail: "Integrated result" },
]

function stageFor(node: TraceNode) {
  const value = `${node.phase} ${node.role}`.toLowerCase()
  if (/product|architect|plan|issue|advisor/.test(value)) return "planning"
  if (/review|qa|security/.test(value)) return "review"
  if (/verify|test/.test(value)) return "verification"
  if (/integrat|coordinator|release/.test(value)) return "integration"
  return "implementation"
}

function WorkflowStages({ nodes }: { nodes: TraceNode[] }) {
  const counts = stages.map((stage) => {
    const items = nodes.filter((node) => stageFor(node) === stage.key)
    return { ...stage, total: items.length, active: items.filter((node) => node.status.toLowerCase() === "running").length, failed: items.filter((node) => ["failed", "blocked"].includes(node.status.toLowerCase())).length, done: items.filter((node) => ["complete", "completed", "succeeded"].includes(node.status.toLowerCase())).length }
  })
  const activeIndex = Math.max(0, counts.findIndex((stage) => stage.active > 0))
  return <Card><CardHeader><CardTitle>Delivery stages</CardTitle><CardDescription>The swarm’s current position from planning through integrated acceptance.</CardDescription></CardHeader><CardContent><ol className="grid border md:grid-cols-5">{counts.map((stage, index) => {
    const state = stage.failed ? "Needs attention" : stage.active ? "In progress" : stage.total && stage.done === stage.total ? "Complete" : index < activeIndex ? "Complete" : "Waiting"
    return <li key={stage.key} className={cn("min-w-0 border-b p-4 last:border-b-0 md:border-b-0 md:border-r md:last:border-r-0", stage.active && "bg-muted/60")}><div className="flex items-baseline justify-between gap-2"><span className="font-mono text-[10px] text-muted-foreground">0{index + 1}</span><span className={cn("text-[11px]", stage.active && "font-medium", stage.failed && "text-destructive")}>{state}</span></div><h3 className="mt-3 font-medium">{stage.label}</h3><p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{stage.detail}</p><p className="mt-3 font-mono text-[10px] text-muted-foreground">{stage.done}/{stage.total} complete{stage.active ? ` · ${stage.active} active` : ""}</p></li>
  })}</ol></CardContent></Card>
}

function ActiveAgent({ node, onOpen }: { node: TraceNode; onOpen: (node: TraceNode) => void }) {
  return <button onClick={() => onOpen(node)} className="w-full rounded-[6px] border bg-background p-4 text-left transition-colors hover:bg-accent"><div className="flex min-w-0 flex-wrap items-start justify-between gap-2"><div className="min-w-0"><p className="break-words text-sm font-medium">{node.role}</p><p className="mt-2 break-words text-xs leading-5 text-muted-foreground">{node.task || "Waiting for task detail"}</p></div><StatusBadge value={node.status} /></div><dl className="mt-4 grid gap-3 border-t pt-3 text-xs sm:grid-cols-3"><div><dt className="text-muted-foreground">Model</dt><dd className="mt-1 break-all font-mono">{node.resolved_model || node.model || "Pending"}</dd></div><div><dt className="text-muted-foreground">Runtime</dt><dd className="mt-1 break-words">{node.harness} · {node.resolved_backend || node.provider}</dd></div><div><dt className="text-muted-foreground">Elapsed</dt><dd className="mt-1 font-mono">{formatMs(node.duration_ms, node.started_at, node.completed_at)}</dd></div></dl></button>
}

function ExecutionLanes({ nodes, onOpen }: { nodes: TraceNode[]; onOpen: (node: TraceNode) => void }) {
  const [status, setStatus] = useState("all")
  const filtered = useMemo(() => status === "all" ? nodes : nodes.filter((node) => status === "complete" ? ["complete", "completed", "succeeded"].includes(node.status.toLowerCase()) : node.status.toLowerCase() === status), [nodes, status])
  const grouped = stages.map((stage) => ({ ...stage, nodes: filtered.filter((node) => stageFor(node) === stage.key) })).filter((stage) => stage.nodes.length)
  return <Card><CardHeader><CardTitle>Execution lanes</CardTitle><CardDescription>Every agent grouped by delivery stage. Select a row to inspect its complete input and output.</CardDescription></CardHeader><CardContent><div className="mb-4 flex flex-wrap border-b">{["all", "running", "complete", "failed"].map((value) => <button key={value} onClick={() => setStatus(value)} className={cn("border-b-2 border-transparent px-3 py-2 text-xs capitalize text-muted-foreground", status === value && "border-foreground text-foreground")}>{value}</button>)}</div><div className="space-y-5">{grouped.map((group) => <section key={group.key}><div className="mb-2 flex flex-wrap items-baseline justify-between gap-2"><h3 className="text-xs font-medium uppercase tracking-[.12em]">{group.label}</h3><span className="font-mono text-[10px] text-muted-foreground">{group.nodes.length} execution{group.nodes.length === 1 ? "" : "s"}</span></div><div className="border">{group.nodes.map((node) => <button key={node.execution_id} onClick={() => onOpen(node)} className="grid w-full gap-2 border-b p-3 text-left last:border-b-0 hover:bg-accent sm:grid-cols-[minmax(120px,.55fr)_minmax(220px,1.5fr)_minmax(150px,.8fr)_auto] sm:items-start"><div><p className="break-words text-sm font-medium">{node.role}</p><p className="mt-1 font-mono text-[10px] text-muted-foreground">depth {node.depth}</p></div><p className="break-words text-xs leading-5 text-muted-foreground">{node.task || node.phase}</p><div><p className="break-all font-mono text-xs">{node.resolved_model || node.model}</p><p className="mt-1 break-words text-[10px] text-muted-foreground">{node.resolved_backend || node.provider} · {node.harness}</p></div><StatusBadge value={node.status} /></button>)}</div></section>)}{!grouped.length && <div className="border p-8 text-center text-sm text-muted-foreground">No executions match this filter.</div>}</div></CardContent></Card>
}

function InferenceFeed({ calls }: { calls: InferenceCall[] }) {
  return <Card><CardHeader><CardTitle>Inference activity</CardTitle><CardDescription>Newest concrete backend calls across the swarm.</CardDescription></CardHeader><CardContent className="p-0"><ScrollArea className="h-[430px]"><div className="divide-y">{calls.map((call) => <div key={call.id} className="grid gap-2 p-4 sm:grid-cols-[minmax(0,1fr)_auto]"><div className="min-w-0"><div className="flex min-w-0 flex-wrap items-baseline gap-2"><span className={cn("text-[11px] font-medium", call.ok ? "text-emerald-700 dark:text-emerald-400" : "text-destructive")}>{call.ok ? "Succeeded" : "Failed"}</span><span className="break-all font-mono text-xs">{call.model_ref}</span></div><p className="mt-1 break-words text-[10px] leading-4 text-muted-foreground">{call.backend} · {call.role || "harness call"}{call.virtual_model ? ` · ${call.virtual_model}` : ""}</p><p className="mt-1 text-[10px] text-muted-foreground">{compactNumber(call.prompt_tokens + call.output_tokens)} tokens · {call.latency_ms.toLocaleString()} ms</p></div><time className="font-mono text-[10px] text-muted-foreground">{new Date(call.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}</time></div>)}{!calls.length && <div className="p-10 text-center text-sm text-muted-foreground">Waiting for the first inference event.</div>}</div></ScrollArea></CardContent></Card>
}

export function LiveCoordination({ nodes, calls, streamState, streamError, onOpen }: { nodes: TraceNode[]; calls: InferenceCall[]; streamState: StreamState; streamError: string; onOpen: (node: TraceNode) => void }) {
  const active = nodes.filter((node) => node.status.toLowerCase() === "running")
  const completed = nodes.filter((node) => ["complete", "completed", "succeeded"].includes(node.status.toLowerCase())).length
  const failed = nodes.filter((node) => ["failed", "blocked"].includes(node.status.toLowerCase())).length
  return <div className="space-y-4"><div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"><span className={cn("font-medium", streamState === "live" ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400")}>{streamState === "live" ? "Live updates" : streamState}</span><span className="break-words text-muted-foreground">{streamError || "AgentField and inference ledger · updates every 2 seconds"}</span></div><WorkflowStages nodes={nodes} /><Card className="grid overflow-hidden sm:grid-cols-3"><Metric label="Working now" value={active.length} /><Metric label="Finished" value={completed} /><Metric label="Needs attention" value={failed} danger={failed > 0} /></Card><Card><CardHeader><CardTitle>Agents working now</CardTitle><CardDescription>Current task, concrete model, backend, harness, and elapsed time.</CardDescription></CardHeader><CardContent>{active.length ? <div className="grid gap-3 lg:grid-cols-2">{active.map((node) => <ActiveAgent key={node.execution_id} node={node} onOpen={onOpen} />)}</div> : <div className="border border-dashed p-8 text-center text-sm text-muted-foreground">No agent is actively executing. The build may be queued, between stages, or finished.</div>}</CardContent></Card><div className="grid min-w-0 gap-4 2xl:grid-cols-[minmax(0,1.7fr)_minmax(340px,.7fr)]"><ExecutionLanes nodes={nodes} onOpen={onOpen} /><InferenceFeed calls={calls} /></div></div>
}

function Metric({ label, value, danger = false }: { label: string; value: number; danger?: boolean }) {
  return <div className="border-b p-4 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0"><p className="text-xs text-muted-foreground">{label}</p><p className={cn("mt-1 text-2xl font-semibold tracking-[-0.03em]", danger && "text-destructive")}>{value}</p></div>
}
