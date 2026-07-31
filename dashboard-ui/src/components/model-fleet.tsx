import { useMemo, useState } from "react"
import {
  Activity,
  Bot,
  CircleDollarSign,
  Cpu,
  Gauge,
  RefreshCw,
  Server,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  Sankey,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { compactNumber, formatMs } from "@/lib/api"
import type { ModelOverview } from "@/types"

const chartConfig = {
  executions: { label: "Executions", color: "var(--chart-1)" },
  calls: { label: "Router calls", color: "var(--chart-2)" },
} satisfies ChartConfig

function StatCard({
  label,
  value,
  detail,
  icon: Icon,
}: {
  label: string
  value: string
  detail: string
  icon: typeof Activity
}) {
  return (
    <Card className="border-border/70 bg-card/70 shadow-none">
      <CardContent className="flex items-start justify-between p-5">
        <div>
          <p className="text-xs font-medium text-muted-foreground">{label}</p>
          <p className="mt-2 text-2xl font-semibold tracking-tight">{value}</p>
          <p className="mt-1 text-xs text-muted-foreground">{detail}</p>
        </div>
        <div className="rounded-lg border bg-background/60 p-2.5 text-muted-foreground">
          <Icon className="size-4" />
        </div>
      </CardContent>
    </Card>
  )
}

function CapacityCards({ capacity }: { capacity: ModelOverview["capacity"] }) {
  const labels: Record<string, string> = {
    claude_code: "Claude Code",
    codex: "Codex",
    open_code: "Local / OpenCode",
  }
  return (
    <div className="grid gap-3 lg:grid-cols-3">
      {Object.entries(capacity).map(([key, item]) => (
        <Card key={key} className="border-border/70 bg-card/50 shadow-none">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-3">
              <CardTitle className="text-sm">{labels[key] || key}</CardTitle>
              <Badge variant={item.available ? "secondary" : "destructive"} className="text-[10px]">
                {item.available ? "available" : "unavailable"}
              </Badge>
            </div>
            <CardDescription className="min-h-8 text-xs leading-4">{item.reason}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="mb-2 flex justify-between font-mono text-xs text-muted-foreground">
              <span>{item.reason.includes("not queryable") ? "subscription quota unknown" : `${Math.round(item.fraction_remaining * 100)}% estimated headroom`}</span>
              <span>{item.calls} ledger-metered calls</span>
            </div>
            {!item.reason.includes("not queryable") && <Progress value={item.fraction_remaining * 100} className="h-1.5" />}
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

function FlowGraph({ overview }: { overview: ModelOverview }) {
  const [placement, setPlacement] = useState<"all" | "local" | "hosted">("all")
  const [edgeLimit, setEdgeLimit] = useState(36)
  const [focus, setFocus] = useState("")
  const concreteNodes = overview.graph.nodes
    .filter((item) => item.kind === "model")
    .sort((a, b) => a.label.localeCompare(b.label))
  const data = useMemo(() => {
    const nodeById = new Map(overview.graph.nodes.map((item) => [item.id, item]))
    const connected = new Set<string>()
    let candidates = overview.graph.edges.filter((edge) => {
      const source = nodeById.get(edge.source)
      const target = nodeById.get(edge.target)
      const placementNodes = [source, target].filter((item) => item?.kind === "backend" || item?.kind === "model")
      const isLocal = placementNodes.some((item) => item?.local)
      if (placement === "local" && !isLocal) return false
      if (placement === "hosted" && isLocal) return false
      return !focus || edge.source === focus || edge.target === focus
    })
    if (focus) {
      const neighbors = new Set(candidates.flatMap((edge) => [edge.source, edge.target]))
      candidates = overview.graph.edges.filter((edge) => neighbors.has(edge.source) && neighbors.has(edge.target))
    }
    const strongest = [...candidates]
      .sort((a, b) => b.value - a.value)
      .slice(0, edgeLimit)
    strongest.forEach((item) => {
      connected.add(item.source)
      connected.add(item.target)
    })
    const rawNodes = overview.graph.nodes.filter((item) => connected.has(item.id))
    const index = new Map(rawNodes.map((item, i) => [item.id, i]))
    return {
      nodes: rawNodes.map((item) => ({ name: item.label, ...item })),
      links: strongest
        .filter((item) => index.has(item.source) && index.has(item.target))
        .map((item) => ({
          source: index.get(item.source)!,
          target: index.get(item.target)!,
          value: Math.max(1, item.value),
        })),
    }
  }, [edgeLimit, focus, overview, placement])

  if (!data.links.length) {
    return <div className="grid h-80 place-items-center text-sm text-muted-foreground">Usage appears after the first routed call.</div>
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1.5">
          {(["all", "local", "hosted"] as const).map((item) => <Button key={item} size="xs" variant={placement === item ? "secondary" : "ghost"} onClick={() => setPlacement(item)}>{item}</Button>)}
          <span className="mx-1 h-6 w-px bg-border" />
          {[18, 36, 64].map((item) => <Button key={item} size="xs" variant={edgeLimit === item ? "secondary" : "ghost"} onClick={() => setEdgeLimit(item)}>{item} links</Button>)}
        </div>
        {focus && <Button size="xs" variant="outline" onClick={() => setFocus("")}>Clear model focus</Button>}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {concreteNodes.map((item) => <Button key={item.id} size="xs" variant={focus === item.id ? "default" : "outline"} className="font-mono" onClick={() => setFocus((value) => value === item.id ? "" : item.id)}>{item.label}</Button>)}
      </div>
      <div className="h-[420px] w-full overflow-hidden rounded-lg bg-background/35 p-2">
        <ChartContainer config={chartConfig} className="h-full w-full aspect-auto">
          <Sankey
            data={data}
            nodePadding={18}
            nodeWidth={9}
            linkCurvature={0.55}
            margin={{ top: 12, right: 130, bottom: 12, left: 130 }}
          >
            <RechartsTooltip
              contentStyle={{
                borderRadius: 8,
                border: "1px solid var(--border)",
                background: "var(--popover)",
                color: "var(--popover-foreground)",
                fontSize: 12,
              }}
            />
          </Sankey>
        </ChartContainer>
      </div>
    </div>
  )
}

export function ModelFleet({
  overview,
  loading,
  error,
  onRefresh,
}: {
  overview: ModelOverview | null
  loading: boolean
  error: string
  onRefresh: () => void
}) {
  if (!overview && loading) {
    return <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">{Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-32" />)}</div>
  }
  if (!overview) {
    return (
      <Alert variant="destructive">
        <TriangleAlert />
        <AlertTitle>Model telemetry unavailable</AlertTitle>
        <AlertDescription>{error || "The scheduler did not return a fleet snapshot."}</AlertDescription>
      </Alert>
    )
  }

  const { summary } = overview
  const localPercent = Math.round(summary.local_fraction * 100)
  const roleData = Object.entries(summary.execution_roles || {})
    .map(([role, executions]) => ({ role, executions }))
    .sort((a, b) => b.executions - a.executions)
    .slice(0, 10)
  const routeData = [
    { name: "Local", value: summary.local_calls, fill: "var(--chart-1)" },
    { name: "Hosted", value: Math.max(0, summary.router_calls - summary.local_calls), fill: "var(--chart-3)" },
  ]

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2">
            <Badge variant="outline" className="border-emerald-500/25 bg-emerald-500/8 text-emerald-400">Fleet live</Badge>
            <span className="font-mono text-xs text-muted-foreground">last {overview.window_hours}h</span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Model fleet</h1>
          <p className="mt-1 text-sm text-muted-foreground">Concrete routing, harness usage, role allocation, quality, latency, cost, and quota headroom.</p>
        </div>
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
          <RefreshCw className={loading ? "animate-spin" : ""} /> Refresh
        </Button>
      </div>

      {error && <Alert variant="destructive"><TriangleAlert /><AlertTitle>Partial telemetry</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Actual local calls" value={summary.local_calls.toLocaleString()} detail={`${localPercent}% of ${summary.router_calls.toLocaleString()} measured inference calls`} icon={Cpu} />
        <StatCard label="Active agents" value={summary.active_executions.toString()} detail={`${summary.local_active_executions.toLocaleString()} resolving to local models now`} icon={Activity} />
        <StatCard label="Reported tokens" value={compactNumber(summary.tokens)} detail={`${summary.models} models across ${summary.builds} builds · exact for new Ollama calls`} icon={Gauge} />
        <StatCard label="Recorded spend" value={`$${summary.cost_usd.toFixed(2)}`} detail={`${summary.router_failures} backend failures · ${summary.routing_rejections} route rejections`} icon={CircleDollarSign} />
      </div>

      <CapacityCards capacity={overview.capacity} />

      <Card className="border-border/70 shadow-none">
        <CardHeader><CardTitle className="text-base">Live tier resolution</CardTitle><CardDescription>Virtual tiers are policy inputs. These are the real models the next local call will reach.</CardDescription></CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-3">{Object.values(overview.routes || {}).map((route) => <div key={route.alias} className="rounded-lg border bg-background/35 p-4"><div className="font-mono text-[11px] text-muted-foreground">{route.alias} · routing tier</div><div className="mt-2 font-mono text-sm font-semibold text-primary">{route.model || "not dispatched"}</div><div className="mt-2 flex items-center gap-2"><Badge variant="outline">{route.backend || "pending"}</Badge>{route.degraded_to && <Badge variant="secondary">using {route.degraded_to} tier</Badge>}</div></div>)}</CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.7fr)_minmax(320px,.7fr)]">
        <Card className="border-border/70 shadow-none">
          <CardHeader>
            <div className="flex items-start justify-between gap-4">
              <div><CardTitle>Actual inference flow</CardTitle><CardDescription>Measured routing tiers → backends → real models. Link weights are router calls only.</CardDescription></div>
              <Server className="size-4 text-muted-foreground" />
            </div>
          </CardHeader>
          <CardContent><FlowGraph overview={overview} /></CardContent>
        </Card>

        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-1">
          <Card className="border-border/70 shadow-none">
            <CardHeader><CardTitle className="text-base">Local vs hosted</CardTitle><CardDescription>Concrete router calls, not configured intent.</CardDescription></CardHeader>
            <CardContent>
              <ChartContainer config={chartConfig} className="mx-auto h-44 w-full aspect-auto">
                <PieChart><Pie data={routeData} dataKey="value" nameKey="name" innerRadius={46} outerRadius={67} paddingAngle={3}>{routeData.map((item) => <Cell key={item.name} fill={item.fill} />)}</Pie><ChartTooltip content={<ChartTooltipContent hideLabel />} /></PieChart>
              </ChartContainer>
              <div className="mt-2 grid grid-cols-2 gap-2 text-center text-xs"><div><span className="font-mono text-base font-semibold">{localPercent}%</span><p className="text-muted-foreground">local</p></div><div><span className="font-mono text-base font-semibold">{100 - localPercent}%</span><p className="text-muted-foreground">hosted</p></div></div>
            </CardContent>
          </Card>
          <Card className="border-border/70 shadow-none">
            <CardHeader><CardTitle className="text-base">Busiest roles</CardTitle><CardDescription>Agent executions by responsibility.</CardDescription></CardHeader>
            <CardContent>
              <ChartContainer config={chartConfig} className="h-52 w-full aspect-auto">
                <BarChart data={roleData} layout="vertical" margin={{ left: 8, right: 8 }}><CartesianGrid horizontal={false} /><YAxis dataKey="role" type="category" width={104} tickLine={false} axisLine={false} fontSize={10} /><XAxis type="number" hide /><ChartTooltip content={<ChartTooltipContent />} /><Bar dataKey="executions" fill="var(--color-executions)" radius={[0, 4, 4, 0]} /></BarChart>
              </ChartContainer>
            </CardContent>
          </Card>
        </div>
      </div>

      <Card className="border-border/70 shadow-none">
        <CardHeader>
          <div className="flex items-start justify-between gap-4"><div><CardTitle>Real model performance</CardTitle><CardDescription>Concrete model names only. Routing aliases such as ctswarm/med are shown separately above.</CardDescription></div><Bot className="size-4 text-muted-foreground" /></div>
        </CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader><TableRow><TableHead>Real model</TableHead><TableHead>Placement</TableHead><TableHead>Live jobs</TableHead><TableHead>Inference calls</TableHead><TableHead>Call success</TableHead><TableHead>Direct agent runs</TableHead><TableHead>Latency</TableHead><TableHead>Reported tokens</TableHead><TableHead>Quality</TableHead></TableRow></TableHeader>
              <TableBody>
                {overview.models.map((model) => {
                  const callSuccess = model.call_success_rate
                  return (
                    <TableRow key={model.name}>
                      <TableCell><div className="font-mono text-xs font-medium">{model.name}</div><div className="mt-1 max-w-72 truncate text-[11px] text-muted-foreground">{Object.keys(model.roles).slice(0, 4).join(" · ") || "No role metadata"}</div></TableCell>
                      <TableCell><Badge variant="outline" className={model.local ? "border-emerald-500/20 text-emerald-400" : "text-muted-foreground"}>{model.local ? <Cpu /> : <Server />}{model.backends.join(", ") || model.harnesses.join(", ")}</Badge></TableCell>
                      <TableCell className="font-mono text-xs"><span className={model.live_jobs > 0 ? "text-sky-400" : "text-muted-foreground"}>{model.live_jobs.toLocaleString()}</span></TableCell>
                      <TableCell className="font-mono text-xs">{model.calls.toLocaleString()}</TableCell>
                      <TableCell>{callSuccess == null ? "—" : <span className={callSuccess >= 0.9 ? "text-emerald-400" : "text-amber-400"}>{Math.round(callSuccess * 100)}%</span>}</TableCell>
                      <TableCell className="font-mono text-xs">{model.executions.toLocaleString()}</TableCell>
                      <TableCell className="font-mono text-xs">{model.avg_latency_ms ? formatMs(model.avg_latency_ms) : "—"}</TableCell>
                      <TableCell className="font-mono text-xs">{compactNumber(model.tokens)}</TableCell>
                      <TableCell>{model.benchmark ? <div className="flex items-center gap-2"><ShieldCheck className={model.benchmark.eligible ? "size-4 text-emerald-400" : "size-4 text-amber-400"} /><span className="font-mono text-xs">{Math.round(model.benchmark.quality * 100)}%</span></div> : "—"}</TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
