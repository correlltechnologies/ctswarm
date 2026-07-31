import { useMemo, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { compactNumber, formatMs } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { ModelCatalogEntry, ModelOverview } from "@/types"

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="min-w-0 border-b p-4 last:border-b-0 sm:p-5 xl:border-b-0 xl:border-r xl:last:border-r-0"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-2 break-words text-2xl font-semibold tracking-[-0.03em]">{value}</p><p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{detail}</p></div>
}

function CapacityCards({ capacity }: { capacity: ModelOverview["capacity"] }) {
  const labels: Record<string, string> = { claude_code: "Claude Code", codex: "Codex", open_code: "Local / OpenCode" }
  return <div className="grid border sm:grid-cols-3">{Object.entries(capacity).map(([key, item]) => {
    const known = !item.reason.includes("not queryable")
    return <section key={key} className="min-w-0 border-b p-4 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0"><div className="flex flex-wrap items-baseline justify-between gap-2"><h3 className="font-medium">{labels[key] || key}</h3><span className={cn("text-xs", item.available ? "text-emerald-700 dark:text-emerald-400" : "text-destructive")}>{item.available ? "Available" : "Unavailable"}</span></div><p className="mt-2 min-h-10 break-words text-xs leading-5 text-muted-foreground">{item.reason}</p><div className="mt-4 flex flex-wrap justify-between gap-2 font-mono text-[11px] text-muted-foreground"><span>{known ? `${Math.round(item.fraction_remaining * 100)}% headroom` : "Quota unknown"}</span><span>{item.calls} measured calls</span></div>{known && <Progress value={item.fraction_remaining * 100} className="mt-2 h-1" />}</section>
  })}</div>
}

function RoutingFlow({ overview }: { overview: ModelOverview }) {
  const [placement, setPlacement] = useState<"all" | "local" | "hosted">("all")
  const nodeById = useMemo(() => new Map(overview.graph.nodes.map((node) => [node.id, node])), [overview.graph.nodes])
  const rows = useMemo(() => overview.graph.edges.map((edge) => {
    const source = nodeById.get(edge.source)
    const target = nodeById.get(edge.target)
    return { source, target, value: edge.value, local: Boolean(source?.local || target?.local) }
  }).filter((row) => row.source && row.target && (placement === "all" || (placement === "local" ? row.local : !row.local))).sort((a, b) => b.value - a.value).slice(0, 30), [nodeById, overview.graph.edges, placement])
  const max = Math.max(1, ...rows.map((row) => row.value))

  return <div>
    <div className="mb-4 flex flex-wrap border-b">{(["all", "local", "hosted"] as const).map((value) => <button key={value} onClick={() => setPlacement(value)} className={cn("border-b-2 border-transparent px-3 py-2 text-xs capitalize text-muted-foreground", placement === value && "border-foreground text-foreground")}>{value}</button>)}</div>
    <div className="space-y-0 border">
      {rows.map((row, index) => <div key={`${row.source!.id}-${row.target!.id}-${index}`} className="grid gap-2 border-b p-3 last:border-b-0 md:grid-cols-[minmax(120px,.8fr)_minmax(120px,.8fr)_minmax(180px,1.4fr)_64px] md:items-center">
        <div><span className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">{row.source!.kind}</span><p className="break-words font-mono text-xs">{row.source!.label}</p></div>
        <div><span className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">{row.target!.kind}</span><p className="break-words font-mono text-xs">{row.target!.label}</p></div>
        <div className="h-1 bg-muted"><div className="h-full bg-foreground" style={{ width: `${Math.max(2, row.value / max * 100)}%` }} /></div>
        <div className="text-left font-mono text-xs md:text-right">{row.value.toLocaleString()}</div>
      </div>)}
      {!rows.length && <div className="p-8 text-center text-sm text-muted-foreground">Usage appears after the first routed call.</div>}
    </div>
  </div>
}

function RoleBars({ roles }: { roles: Record<string, number> }) {
  const data = Object.entries(roles).sort((a, b) => b[1] - a[1]).slice(0, 10)
  const max = Math.max(1, ...data.map(([, value]) => value))
  return <div className="space-y-3">{data.map(([role, value]) => <div key={role} className="grid grid-cols-[minmax(100px,1fr)_minmax(100px,2fr)_48px] items-center gap-3"><span className="break-words text-xs">{role}</span><div className="h-1 bg-muted"><div className="h-full bg-foreground" style={{ width: `${value / max * 100}%` }} /></div><span className="text-right font-mono text-xs">{value}</span></div>)}{!data.length && <p className="text-sm text-muted-foreground">No role executions recorded.</p>}</div>
}

function formatContext(value: number) {
  return value >= 1000 ? `${Math.round(value / 1000)}K` : value.toLocaleString()
}

function catalogReason(model: ModelCatalogEntry) {
  if (model.routable_tiers.length) return `Eligible for ${model.routable_tiers.join(", ")} tier${model.routable_tiers.length === 1 ? "" : "s"}`
  const tiersByReason = new Map<string, string[]>()
  Object.entries(model.exclusions).forEach(([tier, reason]) => tiersByReason.set(reason, [...(tiersByReason.get(reason) || []), tier]))
  const reasons = [...tiersByReason.entries()].map(([reason, tiers]) => `${tiers.join("/")} tier${tiers.length === 1 ? "" : "s"}: ${reason}`)
  return reasons.join(" · ") || "No routing tier is configured"
}

function ModelCatalog({ overview }: { overview: ModelOverview }) {
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<"all" | "routable" | "local" | "hosted" | "excluded">("all")
  const models = useMemo(() => overview.catalog || [], [overview.catalog])
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return models.filter((model) => {
      const local = model.placement !== "hosted"
      const matchesFilter = filter === "all" || (filter === "routable" && model.routable) || (filter === "local" && local) || (filter === "hosted" && !local) || (filter === "excluded" && !model.routable)
      const matchesQuery = !needle || `${model.ref} ${model.backend} ${model.placement} ${model.tiers.join(" ")} ${model.notes} ${catalogReason(model)}`.toLowerCase().includes(needle)
      return matchesFilter && matchesQuery
    })
  }, [filter, models, query])
  const filters = [
    ["all", "All", models.length],
    ["routable", "Routable", models.filter((model) => model.routable).length],
    ["local", "Local", models.filter((model) => model.placement !== "hosted").length],
    ["hosted", "Hosted", models.filter((model) => model.placement === "hosted").length],
    ["excluded", "Excluded", models.filter((model) => !model.routable).length],
  ] as const

  return <Card>
    <CardHeader><CardTitle>Model catalog</CardTitle><CardDescription>Every model configured in routing policy, including models that are unavailable. “Routable” uses the same installation, benchmark, context, tool-call, circuit-breaker, and privacy gates as a real dispatch.</CardDescription></CardHeader>
    <CardContent>
      <div className="grid gap-3 border p-4 sm:grid-cols-4">
        <div><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">Configured</p><p className="mt-1 font-mono text-lg">{models.length}</p></div>
        <div><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">Installed</p><p className="mt-1 font-mono text-lg">{models.filter((model) => model.installed).length}</p></div>
        <div><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">Routable now</p><p className="mt-1 font-mono text-lg">{models.filter((model) => model.routable).length}</p></div>
        <div><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">Measured</p><p className="mt-1 font-mono text-lg">{models.filter((model) => model.benchmark).length}</p></div>
      </div>
      {overview.catalog_local_only && <p className="mt-3 border-l-2 border-amber-600 pl-3 text-xs leading-5 text-muted-foreground">Local-only routing is enabled. Hosted models remain visible in the catalog but are explicitly excluded from dispatch.</p>}
      {overview.catalog_error && <p className="mt-3 border-l-2 border-destructive pl-3 text-xs text-destructive">{overview.catalog_error}</p>}
      <div className="mt-4 flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div className="flex flex-wrap border-b">{filters.map(([value, label, count]) => <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)} className={cn("border-b-2 border-transparent px-3 py-2 text-xs text-muted-foreground", filter === value && "border-foreground text-foreground")}>{label} <span className="ml-1 font-mono text-[10px]">{count}</span></button>)}</div>
        <Input aria-label="Search model catalog" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search model, backend, tier, or reason" className="h-9 lg:max-w-sm" />
      </div>
      <div className="mt-4 border">
        <Table className="min-w-[1120px] table-fixed [&_td]:whitespace-normal [&_th]:whitespace-normal"><TableHeader><TableRow><TableHead className="w-[250px]">Model</TableHead><TableHead className="w-[105px]">Status</TableHead><TableHead className="w-[125px]">Backend / placement</TableHead><TableHead className="w-[80px]">Tiers</TableHead><TableHead className="w-[75px]">Context</TableHead><TableHead className="w-[145px]">Benchmark</TableHead><TableHead className="w-[340px]">Why</TableHead></TableRow></TableHeader><TableBody>{visible.map((model) => <TableRow key={`${model.backend}:${model.ref}`}>
          <TableCell><div className="break-all font-mono text-xs font-medium">{model.ref}</div><div className="mt-1 break-words text-[11px] leading-4 text-muted-foreground">{model.notes || "No catalog note"}</div></TableCell>
          <TableCell><div className={cn("break-words text-xs font-medium", model.routable ? "text-emerald-700 dark:text-emerald-400" : "text-muted-foreground")}>{model.routable ? "Routable" : model.installed ? "Excluded" : "Not installed"}</div>{model.warm && <div className="mt-1 text-[10px] text-muted-foreground">Loaded in memory</div>}{model.circuit_open && <div className="mt-1 text-[10px] text-destructive">Circuit open</div>}</TableCell>
          <TableCell><div className="break-all font-mono text-xs">{model.backend}</div><div className="mt-1 break-words text-[10px] text-muted-foreground">{model.placement.replaceAll("_", " ")}</div></TableCell>
          <TableCell className="font-mono text-xs">{model.tiers.join(" · ") || "—"}</TableCell>
          <TableCell className="font-mono text-xs">{formatContext(model.context)}</TableCell>
          <TableCell>{model.benchmark ? <><div className="font-mono text-xs">{Math.round(model.benchmark.quality * 100)}% quality</div><div className="mt-1 break-words text-[10px] text-muted-foreground">{model.benchmark.eligible ? "Passed gates" : "Failed gates"} · {Math.round(model.benchmark.tokens_per_s)} tok/s</div></> : <span className="text-xs text-muted-foreground">Not measured</span>}</TableCell>
          <TableCell><p className="break-words text-xs leading-5 text-muted-foreground">{catalogReason(model)}</p></TableCell>
        </TableRow>)}{!visible.length && <TableRow><TableCell colSpan={7} className="p-8 text-center text-sm text-muted-foreground">No catalog models match this filter.</TableCell></TableRow>}</TableBody></Table>
      </div>
    </CardContent>
  </Card>
}

export function ModelFleet({ overview, loading, error, onRefresh }: { overview: ModelOverview | null; loading: boolean; error: string; onRefresh: () => void }) {
  if (!overview && loading) return <div className="grid gap-4 md:grid-cols-2">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-32" />)}</div>
  if (!overview) return <Alert variant="destructive"><AlertTitle>Model telemetry unavailable</AlertTitle><AlertDescription>{error || "The scheduler did not return a fleet snapshot."}</AlertDescription></Alert>

  const { summary } = overview
  const localPercent = Math.round(summary.local_fraction * 100)
  const hostedCalls = Math.max(0, summary.router_calls - summary.local_calls)
  return <div className="space-y-5">
    <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between"><div><div className="mb-2 flex flex-wrap items-center gap-2 text-xs"><span className="text-emerald-700 dark:text-emerald-400">Fleet live</span><span className="font-mono text-muted-foreground">Last {overview.window_hours}h</span></div><h1 className="text-2xl font-semibold tracking-[-0.03em] sm:text-3xl">Model fleet</h1><p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">Concrete routing, capacity, quality, latency, and cost. Every label below is the actual backend or model used.</p></div><Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>{loading ? "Refreshing…" : "Refresh data"}</Button></div>
    {error && <Alert variant="destructive"><AlertTitle>Partial telemetry</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
    <Card className="grid overflow-hidden xl:grid-cols-4"><Metric label="Local inference" value={summary.local_calls.toLocaleString()} detail={`${localPercent}% of ${summary.router_calls.toLocaleString()} measured calls`} /><Metric label="Active agents" value={summary.active_executions.toString()} detail={`${summary.local_active_executions} currently routed to local models`} /><Metric label="Reported tokens" value={compactNumber(summary.tokens)} detail={`${summary.models} models across ${summary.builds} builds`} /><Metric label="Recorded spend" value={`$${summary.cost_usd.toFixed(2)}`} detail={`${summary.router_failures} backend failures · ${summary.routing_rejections} rejected routes`} /></Card>
    <CapacityCards capacity={overview.capacity} />
    <Card><CardHeader><CardTitle>Current tier resolution</CardTitle><CardDescription>Virtual policy tier, concrete backend, and the real model selected for the next call.</CardDescription></CardHeader><CardContent className="grid border-t p-0 md:grid-cols-3">{Object.values(overview.routes || {}).map((route) => <div key={route.alias} className="min-w-0 border-b p-4 last:border-b-0 md:border-b-0 md:border-r md:last:border-r-0"><p className="break-all font-mono text-[11px] text-muted-foreground">{route.alias}</p><p className="mt-2 break-words font-mono text-sm font-medium">{route.model || "Not dispatched"}</p><p className="mt-1 break-words text-xs text-muted-foreground">{route.backend || "Backend pending"}{route.degraded_to ? ` · using ${route.degraded_to} tier` : ""}</p></div>)}</CardContent></Card>
    <ModelCatalog overview={overview} />
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(280px,.7fr)]">
      <Card><CardHeader><CardTitle>Measured inference flow</CardTitle><CardDescription>Readable routing edges ordered by call volume. Bars are relative within the current filter.</CardDescription></CardHeader><CardContent><RoutingFlow overview={overview} /></CardContent></Card>
      <div className="space-y-4"><Card><CardHeader><CardTitle>Local and hosted</CardTitle><CardDescription>Concrete router calls, not configured intent.</CardDescription></CardHeader><CardContent><div className="flex h-2 overflow-hidden bg-muted"><div className="bg-foreground" style={{ width: `${localPercent}%` }} /><div className="bg-muted-foreground" style={{ width: `${100 - localPercent}%` }} /></div><div className="mt-3 grid grid-cols-2 gap-4 text-xs"><div><p className="font-mono text-base font-medium">{summary.local_calls.toLocaleString()}</p><p className="text-muted-foreground">Local · {localPercent}%</p></div><div><p className="font-mono text-base font-medium">{hostedCalls.toLocaleString()}</p><p className="text-muted-foreground">Hosted · {100 - localPercent}%</p></div></div></CardContent></Card><Card><CardHeader><CardTitle>Busiest roles</CardTitle><CardDescription>Agent executions by responsibility.</CardDescription></CardHeader><CardContent><RoleBars roles={summary.execution_roles || {}} /></CardContent></Card></div>
    </div>
    <Card><CardHeader><CardTitle>Real model performance</CardTitle><CardDescription>Concrete model names only. Roles wrap instead of being cut off.</CardDescription></CardHeader><CardContent className="p-0"><div className="overflow-x-auto"><Table><TableHeader><TableRow><TableHead>Real model and roles</TableHead><TableHead>Placement</TableHead><TableHead>Live</TableHead><TableHead>Calls</TableHead><TableHead>Success</TableHead><TableHead>Agent runs</TableHead><TableHead>Latency</TableHead><TableHead>Tokens</TableHead><TableHead>Quality</TableHead></TableRow></TableHeader><TableBody>{overview.models.map((model) => <TableRow key={model.name}><TableCell className="min-w-56"><div className="break-all font-mono text-xs font-medium">{model.name}</div><div className="mt-1 max-w-80 break-words text-[11px] leading-4 text-muted-foreground">{Object.keys(model.roles).join(" · ") || "No role metadata"}</div></TableCell><TableCell className="min-w-28"><div className="break-words text-xs">{model.local ? "Local" : "Hosted"}</div><div className="break-words font-mono text-[10px] text-muted-foreground">{model.backends.join(", ") || model.harnesses.join(", ")}</div></TableCell><TableCell className="font-mono text-xs">{model.live_jobs}</TableCell><TableCell className="font-mono text-xs">{model.calls.toLocaleString()}</TableCell><TableCell>{model.call_success_rate == null ? "—" : `${Math.round(model.call_success_rate * 100)}%`}</TableCell><TableCell className="font-mono text-xs">{model.executions.toLocaleString()}</TableCell><TableCell className="font-mono text-xs">{model.avg_latency_ms ? formatMs(model.avg_latency_ms) : "—"}</TableCell><TableCell className="font-mono text-xs">{compactNumber(model.tokens)}</TableCell><TableCell className="font-mono text-xs">{model.benchmark ? `${Math.round(model.benchmark.quality * 100)}%` : "—"}</TableCell></TableRow>)}</TableBody></Table></div></CardContent></Card>
  </div>
}
