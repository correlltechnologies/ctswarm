import { useMemo, useState } from "react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, formatDuration, formatMs, repoName } from "@/lib/api"
import type { Approval, Build, ExecutionDetail, InferenceCall, StreamState, Trace, TraceNode } from "@/types"
import { LiveCoordination } from "./live-coordination"
import { StatusBadge } from "./status-badge"

const terminal = new Set(["complete", "failed", "stopped", "blocked"])

function friendlyModel(value: string) {
  if (value === "ctswarm/high" || value === "high") return "Planning and architecture"
  if (value === "ctswarm/med" || value === "med") return "Implementation and review"
  if (value === "ctswarm/low" || value === "low") return "Quick maintenance"
  if (value.startsWith("ctswarm/") && value.includes(":")) return value.slice(value.indexOf(":") + 1)
  return value
}

function workClass(value: string) {
  const friendly = friendlyModel(value)
  return friendly === value ? "Explicit model assignment" : friendly
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="min-w-0 border-b p-4 last:border-b-0 sm:border-r sm:[&:nth-child(2n)]:border-r-0 xl:border-b-0 xl:border-r xl:[&:nth-child(2n)]:border-r xl:last:border-r-0"><div className="text-[11px] font-medium uppercase tracking-[.12em] text-muted-foreground">{label}</div><div className="mt-2 break-words text-lg font-semibold">{value}</div><div className="mt-1 break-words font-mono text-[11px] leading-5 text-muted-foreground">{detail}</div></div>
}

export function BuildDetail({
  build,
  trace,
  approvals,
  inferenceCalls,
  streamState,
  streamError,
  onReload,
}: {
  build: Build
  trace: Trace | null
  approvals: Approval[]
  inferenceCalls: InferenceCall[]
  streamState: StreamState
  streamError: string
  onReload: () => Promise<void>
}) {
  const [query, setQuery] = useState("")
  const [selectedNode, setSelectedNode] = useState<TraceNode | null>(null)
  const [detail, setDetail] = useState<ExecutionDetail | null>(null)
  const [detailError, setDetailError] = useState("")
  const [busy, setBusy] = useState("")
  const nodes = useMemo(() => trace?.timeline || [], [trace?.timeline])
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return needle ? nodes.filter((node) => `${node.role} ${node.task} ${node.phase} ${node.model} ${node.harness} ${node.provider} ${node.status}`.toLowerCase().includes(needle)) : nodes
  }, [nodes, query])
  const models = [...new Set(nodes.map((node) => node.resolved_model || node.model))].filter((name) => name && name !== "Unknown")
  const pendingApprovals = approvals.filter((item) => !item.decision && !item.expired).length
  const firstSentenceEnd = build.goal.search(/[.!?](?:\s|$)/)
  const goalSummary = firstSentenceEnd >= 0 ? build.goal.slice(0, firstSentenceEnd + 1) : build.goal
  const hasLongBrief = goalSummary.length < build.goal.length

  async function control(action: "pause" | "resume" | "stop") {
    setBusy(action)
    try { await api(`/builds/${encodeURIComponent(build.build_id)}/${action}`, { method: "POST" }); await onReload() } finally { setBusy("") }
  }

  async function decide(item: Approval, decision: "approve" | "deny") {
    setBusy(item.dedupe_key)
    try {
      await api(`/api/dashboard/approvals/${encodeURIComponent(item.dedupe_key)}/decide`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, decided_by: "mission-control" }) })
      await onReload()
    } finally { setBusy("") }
  }

  async function openNode(node: TraceNode) {
    setSelectedNode(node); setDetail(null); setDetailError("")
    try { setDetail(await api<ExecutionDetail>(`/api/dashboard/executions/${encodeURIComponent(node.execution_id)}`)) } catch (error) { setDetailError(error instanceof Error ? error.message : String(error)) }
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="mb-2 flex flex-wrap items-center gap-2"><StatusBadge value={build.state} /><span className="font-mono text-xs text-muted-foreground">{build.build_id}</span></div>
          <h1 className="break-words text-2xl font-semibold tracking-[-0.03em] sm:text-3xl">{repoName(build.repo_url)}</h1>
          <p className="mt-2 max-w-4xl break-words text-sm leading-6 text-muted-foreground">{goalSummary}</p>
          {hasLongBrief && <details className="mt-2 max-w-4xl text-xs"><summary className="w-fit select-none text-primary hover:underline">View full build brief</summary><p className="mt-3 break-words border-l pl-4 text-sm leading-6 text-muted-foreground">{build.goal}</p></details>}
          {build.pr_url && <a href={build.pr_url} target="_blank" rel="noreferrer" className="mt-2 inline-flex text-xs text-primary hover:underline">Open pull request →</a>}
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" disabled={terminal.has(build.state) || build.state === "paused" || !!busy} onClick={() => control("pause")}>Pause</Button>
          <Button variant="outline" size="sm" disabled={terminal.has(build.state) || build.state !== "paused" || !!busy} onClick={() => control("resume")}>Resume</Button>
          <AlertDialog>
            <AlertDialogTrigger asChild><Button variant="destructive" size="sm" disabled={terminal.has(build.state) || !!busy}>Stop</Button></AlertDialogTrigger>
            <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Stop this build?</AlertDialogTitle><AlertDialogDescription>No new agent work will start. Completed commits remain on the build branch.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>Keep running</AlertDialogCancel><AlertDialogAction onClick={() => control("stop")}>Stop build</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
          </AlertDialog>
        </div>
      </div>

      {(build.error || trace?.error) && <Alert variant="destructive"><AlertTitle>Build needs attention</AlertTitle><AlertDescription>{build.error || trace?.error}</AlertDescription></Alert>}

      <Card className="grid overflow-hidden sm:grid-cols-2 xl:grid-cols-5">
        <Metric label="State" value={build.state} detail={`${formatDuration(build.elapsed_s || 0)} elapsed`} />
        <Metric label="Harness" value={trace?.harness || build.runtime || "Pending"} detail={trace?.runtime || build.runtime || "not assigned"} />
        <Metric label="Models" value={models.map(friendlyModel).join(" + ") || "Pending"} detail={Object.entries(trace?.model_policy || {}).map(([key, value]) => `${key}: ${friendlyModel(value)}`).join(" · ") || "awaiting execution"} />
        <Metric label="Agents" value={`${trace?.summary.statuses.running || 0} active`} detail={`${trace?.total_nodes || nodes.length} executions`} />
        <Metric label="Approvals" value={pendingApprovals ? `${pendingApprovals} pending` : `${approvals.length} total`} detail="for this build" />
      </Card>

      <Tabs defaultValue="live" className="space-y-4">
        <TabsList className="h-auto w-full justify-start overflow-x-auto rounded-none border-b bg-transparent p-0">
          {[["live", "Live coordination"], ["execution", "Execution"], ["routing", "Model routing"], ["timeline", "Timeline"], ["approvals", "Approvals"]].map(([value, label]) => <TabsTrigger key={value} value={value} className="rounded-none border-b-2 border-transparent px-4 py-3 data-[state=active]:border-primary data-[state=active]:bg-transparent">{label}</TabsTrigger>)}
        </TabsList>
        <TabsContent value="live">
          <LiveCoordination nodes={nodes} calls={inferenceCalls} streamState={streamState} streamError={streamError} onOpen={openNode} />
        </TabsContent>
        <TabsContent value="execution" className="space-y-3">
          <div className="max-w-md"><Input aria-label="Filter executions" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter role, task, model, or status" /></div>
          <Card><div className="overflow-x-auto"><Table><TableHeader><TableRow><TableHead>Agent / task</TableHead><TableHead>Phase</TableHead><TableHead>Resolved model</TableHead><TableHead>Work type</TableHead><TableHead>Harness</TableHead><TableHead>Status</TableHead><TableHead className="text-right">Duration</TableHead></TableRow></TableHeader><TableBody>{filtered.map((node) => <TableRow key={node.execution_id} className="cursor-pointer" onClick={() => openNode(node)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") openNode(node) }}><TableCell className="min-w-72 max-w-lg"><div className="break-words font-medium">{node.role}</div><div className="mt-1 break-words text-xs leading-5 text-muted-foreground">{node.task}</div></TableCell><TableCell>{node.phase}</TableCell><TableCell><div className="break-all font-mono text-xs">{friendlyModel(node.resolved_model || node.model)}</div><div className="break-words text-[10px] text-muted-foreground">{node.resolved_backend || node.provider} · {node.resolution || "direct"}</div></TableCell><TableCell><div className="break-words text-xs">{workClass(node.requested_model || node.model)}</div><div className="break-words text-[10px] text-muted-foreground">{node.model_source}</div></TableCell><TableCell><div className="break-words text-xs font-medium">{node.harness}</div><div className="break-all font-mono text-[10px] text-muted-foreground">{node.provider}</div></TableCell><TableCell><StatusBadge value={node.status} /></TableCell><TableCell className="text-right font-mono text-xs">{formatMs(node.duration_ms, node.started_at, node.completed_at)}</TableCell></TableRow>)}</TableBody></Table></div>{!filtered.length && <div className="p-10 text-center text-sm text-muted-foreground">No executions match this filter.</div>}</Card>
        </TabsContent>
        <TabsContent value="routing">
          <div className="space-y-4">
            <Card><CardHeader><CardTitle>Role policy</CardTitle><CardDescription>Default worker runtime plus explicit planning and review overrides.</CardDescription></CardHeader><CardContent><div className="border">{Object.entries(trace?.provider_policy || { default: trace?.runtime || build.runtime || "pending" }).map(([role, runtime]) => <div key={role} className="grid gap-1 border-b p-3 last:border-b-0 sm:grid-cols-[minmax(120px,.5fr)_1fr]"><span className="break-all font-mono text-xs">{role}</span><span className="break-words text-xs text-muted-foreground">{runtime.replaceAll("_", " ")}</span></div>)}</div></CardContent></Card>
            <div className="grid gap-4 lg:grid-cols-3">{Object.values(trace?.routes || {}).map((route) => <Card key={route.alias}><CardHeader><CardDescription className="break-words">{workClass(route.alias)}</CardDescription><CardTitle className="break-all font-mono text-base">{friendlyModel(route.model) || "Not dispatched"}</CardTitle></CardHeader><CardContent><p className="break-words text-xs text-muted-foreground">Provider: {route.backend || "pending"}</p>{route.degraded_to && <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">Using the best available fallback</p>}</CardContent></Card>)}</div>
            <div className="grid gap-4 lg:grid-cols-2">{Object.entries(trace?.summary.models || {}).map(([model, count]) => { const assigned = nodes.filter((node) => node.model === model); return <Card key={model}><CardHeader><CardTitle className="break-all font-mono text-base">{friendlyModel(model)}</CardTitle><CardDescription className="break-words">{count} runs · {[...new Set(assigned.map((node) => node.harness))].join(", ")}</CardDescription></CardHeader><CardContent><p className="break-words text-xs leading-5 text-muted-foreground">{[...new Set(assigned.map((node) => node.role))].join(" · ") || "No role metadata"}</p></CardContent></Card> })}</div>
          </div>
        </TabsContent>
        <TabsContent value="timeline"><Card><CardContent className="p-5"><div className="divide-y">{nodes.map((node) => <button key={node.execution_id} className="grid w-full gap-2 py-3 text-left sm:grid-cols-[90px_minmax(0,1fr)_auto]" onClick={() => openNode(node)}><span className="font-mono text-[11px] text-muted-foreground">{node.started_at ? new Date(node.started_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }) : "—"}</span><span><span className="block break-words text-sm font-medium">{node.role}</span><span className="mt-1 block break-words text-xs leading-5 text-muted-foreground">{node.task}</span></span><span className="font-mono text-xs text-muted-foreground">{formatMs(node.duration_ms, node.started_at, node.completed_at)}</span></button>)}</div></CardContent></Card></TabsContent>
        <TabsContent value="approvals"><div className="space-y-3">{approvals.map((item) => <Card key={item.dedupe_key}><CardContent className="grid gap-4 p-5 md:grid-cols-[auto_1fr_auto]"><StatusBadge value={item.decision?.decision || (item.expired ? "expired" : "pending")} /><div><h3 className="break-words font-medium">{item.action}</h3><p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{String(item.payload?.detail || item.payload?.why || item.rule_name)} · {item.risk} risk</p></div><div className="flex gap-2">{item.decision ? <span className="text-xs text-muted-foreground">{item.decision.decided_by || "resolved"}</span> : item.expired ? <span className="text-xs text-muted-foreground">Paused without approval</span> : <><Button size="sm" variant="outline" disabled={busy === item.dedupe_key} onClick={() => decide(item, "approve")}>Approve</Button><Button size="sm" variant="destructive" disabled={busy === item.dedupe_key} onClick={() => decide(item, "deny")}>Deny</Button></>}</div></CardContent></Card>)}{!approvals.length && <Card className="border-dashed"><CardContent className="p-10 text-center text-sm text-muted-foreground">No approval requests for this build.</CardContent></Card>}</div></TabsContent>
      </Tabs>

      <Sheet open={!!selectedNode} onOpenChange={(open) => { if (!open) setSelectedNode(null) }}>
        <SheetContent className="w-full p-0 sm:max-w-2xl"><SheetHeader className="border-b p-6"><SheetDescription className="break-all font-mono text-xs">{selectedNode?.execution_id}</SheetDescription><SheetTitle className="break-words">{selectedNode?.role || "Execution detail"}</SheetTitle></SheetHeader><ScrollArea className="h-[calc(100vh-108px)]"><div className="space-y-5 p-6">{detailError && <Alert variant="destructive"><AlertTitle>Detail unavailable</AlertTitle><AlertDescription>{detailError}</AlertDescription></Alert>}{selectedNode && <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">{[["Resolved model", friendlyModel(selectedNode.resolved_model || selectedNode.model)], ["Work type", workClass(selectedNode.requested_model || selectedNode.model)], ["Backend", selectedNode.resolved_backend || selectedNode.provider], ["Harness", selectedNode.harness], ["Status", detail?.status || selectedNode.status], ["Duration", formatMs(selectedNode.duration_ms, selectedNode.started_at, selectedNode.completed_at)]].map(([label, value]) => <Card key={label}><CardContent className="p-3"><div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div><div className="mt-1 break-all font-mono text-xs">{value}</div></CardContent></Card>)}</div>}<Separator /><div><h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">Input</h3><pre className="max-h-72 overflow-auto rounded-[6px] border bg-muted/30 p-4 font-mono text-xs leading-5">{JSON.stringify(detail?.input_data || {}, null, 2)}</pre></div><div><h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">Output</h3><pre className="max-h-96 overflow-auto rounded-[6px] border bg-muted/30 p-4 font-mono text-xs leading-5">{JSON.stringify(detail?.output_data ?? null, null, 2)}</pre></div></div></ScrollArea></SheetContent>
      </Sheet>
    </div>
  )
}
