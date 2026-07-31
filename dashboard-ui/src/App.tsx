import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Boxes, ChartNetwork, Hammer, Menu, Moon, Radio, Search, Sun } from "lucide-react"

import { BuildDetail } from "@/components/build-detail"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { useTheme } from "@/components/theme-provider"
import { api, formatDuration, repoName } from "@/lib/api"
import { cn } from "@/lib/utils"
import { useDashboardStream } from "@/hooks/use-dashboard-stream"
import type { Approval, Build, ModelOverview, Trace } from "@/types"

type View = "fleet" | "builds"

const ModelFleet = lazy(() =>
  import("@/components/model-fleet").then((module) => ({ default: module.ModelFleet })),
)

function BuildList({
  builds,
  selected,
  query,
  onQuery,
  onSelect,
}: {
  builds: Build[]
  selected: string
  query: string
  onQuery: (value: string) => void
  onSelect: (id: string) => void
}) {
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return needle ? builds.filter((item) => `${item.build_id} ${item.goal} ${item.repo_url} ${item.state}`.toLowerCase().includes(needle)) : builds
  }, [builds, query])
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="relative mx-3 mb-3"><Search className="absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input value={query} onChange={(event) => onQuery(event.target.value)} className="h-9 bg-background/50 pl-8 text-xs" placeholder="Search builds" /></div>
      <ScrollArea className="min-h-0 flex-1 px-2">
        <div className="space-y-1 pb-4">
          {visible.map((build) => (
            <button key={build.build_id} onClick={() => onSelect(build.build_id)} className={cn("w-full rounded-lg border border-transparent p-3 text-left transition-colors hover:bg-accent/60", selected === build.build_id && "border-border bg-accent") }>
              <div className="flex items-start justify-between gap-2"><span className="min-w-0 truncate text-sm font-medium">{repoName(build.repo_url)}</span><StatusBadge value={build.state} /></div>
              <div className="mt-2 truncate font-mono text-[10px] text-muted-foreground">{build.build_id}</div>
              <div className="mt-2 flex justify-between text-[11px] text-muted-foreground"><span>{(build.runtime || "pending").replaceAll("_", " ")}</span><span className="font-mono">{formatDuration(build.elapsed_s || 0)}</span></div>
            </button>
          ))}
          {!visible.length && <div className="p-6 text-center text-xs text-muted-foreground">No builds match.</div>}
        </div>
      </ScrollArea>
    </div>
  )
}

function Sidebar({
  view,
  setView,
  builds,
  selected,
  query,
  setQuery,
  selectBuild,
}: {
  view: View
  setView: (view: View) => void
  builds: Build[]
  selected: string
  query: string
  setQuery: (value: string) => void
  selectBuild: (id: string) => void
}) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-sidebar text-sidebar-foreground">
      <div className="flex h-16 items-center gap-3 px-4"><div className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground"><Boxes className="size-4" /></div><div><div className="text-sm font-semibold">ctswarm</div><div className="text-[11px] text-muted-foreground">Mission Control</div></div></div>
      <div className="space-y-1 px-2 pb-3">
        <Button variant={view === "fleet" ? "secondary" : "ghost"} className="w-full justify-start" onClick={() => setView("fleet")}><ChartNetwork /> Model fleet</Button>
        <Button variant={view === "builds" ? "secondary" : "ghost"} className="w-full justify-start" onClick={() => setView("builds")}><Hammer /> Builds <span className="ml-auto rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">{builds.length}</span></Button>
      </div>
      <Separator />
      <div className="px-4 py-3 text-[10px] font-medium uppercase tracking-[.16em] text-muted-foreground">Recent builds</div>
      <BuildList builds={builds} selected={selected} query={query} onQuery={setQuery} onSelect={selectBuild} />
      <div className="border-t p-4 text-[10px] leading-4 text-muted-foreground">Local execution first. Hosted capacity is reserved for planning and independent review.</div>
    </div>
  )
}

export function App() {
  const { theme, setTheme } = useTheme()
  const [view, setView] = useState<View>("fleet")
  const [builds, setBuilds] = useState<Build[]>([])
  const [selected, setSelected] = useState("")
  const [build, setBuild] = useState<Build | null>(null)
  const [trace, setTrace] = useState<Trace | null>(null)
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [overview, setOverview] = useState<ModelOverview | null>(null)
  const [overviewLoading, setOverviewLoading] = useState(true)
  const [overviewError, setOverviewError] = useState("")
  const [query, setQuery] = useState("")
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [mobileOpen, setMobileOpen] = useState(false)
  const selectedRequest = useRef(0)
  const stream = useDashboardStream(selected)

  const loadBuilds = useCallback(async () => {
    const result = await api<{ builds: Build[] }>("/builds?limit=200")
    setBuilds(result.builds || [])
    setSelected((current) => result.builds.some((item) => item.build_id === current) ? current : (result.builds[0]?.build_id || ""))
    setUpdatedAt(new Date())
  }, [])

  const loadSelected = useCallback(async () => {
    const request = ++selectedRequest.current
    if (!selected) { setBuild(null); setTrace(null); setApprovals([]); return }
    const id = encodeURIComponent(selected)
    const [nextBuild, nextTrace, nextApprovals] = await Promise.all([
      api<Build>(`/builds/${id}`),
      api<Trace>(`/builds/${id}/trace`).catch((error) => ({ timeline: [], model_policy: {}, summary: { statuses: {}, roles: {}, models: {}, harnesses: {} }, total_nodes: 0, execution_id: "", workflow_id: "", status: "failed", runtime: "", harness: "", error: error.message })),
      api<{ approvals: Approval[] }>(`/builds/${id}/approvals`).catch(() => ({ approvals: [] })),
    ])
    if (request !== selectedRequest.current) return
    setBuild(nextBuild); setTrace(nextTrace); setApprovals(nextApprovals.approvals || [])
  }, [selected])

  const loadOverview = useCallback(async () => {
    setOverviewLoading(true); setOverviewError("")
    try { setOverview(await api<ModelOverview>("/api/dashboard/models?window_hours=168")) }
    catch (error) { setOverviewError(error instanceof Error ? error.message : String(error)) }
    finally { setOverviewLoading(false) }
  }, [])

  useEffect(() => {
    // Fetch callbacks update state asynchronously after network completion.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadBuilds(); void loadOverview()
  }, [loadBuilds, loadOverview])
  useEffect(() => {
    // The request token prevents stale selection responses from committing.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadSelected()
  }, [loadSelected])
  useEffect(() => {
    const buildTimer = window.setInterval(() => { if (!document.hidden && stream.state !== "live") void loadBuilds() }, 15000)
    const fleetTimer = window.setInterval(() => { if (!document.hidden && view === "fleet") void loadOverview() }, 20000)
    return () => { window.clearInterval(buildTimer); window.clearInterval(fleetTimer) }
  }, [loadBuilds, loadOverview, stream.state, view])

  useEffect(() => {
    if (!stream.snapshot) return
    // The SSE frame is authoritative while connected; REST remains a fallback.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBuilds(stream.snapshot.builds || [])
    setUpdatedAt(new Date(stream.snapshot.generated_at * 1000))
    if (stream.snapshot.build?.build_id === selected) {
      setBuild(stream.snapshot.build)
      if (stream.snapshot.trace) setTrace(stream.snapshot.trace)
      if (stream.snapshot.approvals) setApprovals(stream.snapshot.approvals)
    }
  }, [selected, stream.snapshot])

  function selectBuild(id: string) { setSelected(id); setView("builds"); setMobileOpen(false) }
  const sidebarProps = { view, setView: (next: View) => { setView(next); setMobileOpen(false) }, builds, selected, query, setQuery, selectBuild }

  return (
    <TooltipProvider>
      <div className="min-h-svh bg-background lg:grid lg:h-svh lg:grid-cols-[280px_minmax(0,1fr)] lg:overflow-hidden">
        <aside className="hidden min-h-0 border-r lg:block"><Sidebar {...sidebarProps} /></aside>
        <div className="min-w-0 lg:min-h-0 lg:overflow-y-auto">
          <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b bg-background/90 px-4 backdrop-blur-xl lg:h-16 lg:px-6">
            <div className="flex items-center gap-3">
              <Sheet open={mobileOpen} onOpenChange={setMobileOpen}><SheetTrigger asChild><Button variant="ghost" size="icon" className="lg:hidden"><Menu /><span className="sr-only">Open navigation</span></Button></SheetTrigger><SheetContent side="left" className="w-[300px] p-0"><SheetHeader className="sr-only"><SheetTitle>Navigation</SheetTitle></SheetHeader><Sidebar {...sidebarProps} /></SheetContent></Sheet>
              <div><div className="text-sm font-medium">{view === "fleet" ? "Model fleet" : build ? repoName(build.repo_url) : "Builds"}</div><div className="flex items-center gap-1.5 font-mono text-[10px] text-muted-foreground"><Radio className={cn("size-2.5", stream.state === "live" && "text-emerald-500")} />{stream.state === "live" && updatedAt ? `live · ${updatedAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}` : stream.state}</div></div>
            </div>
            <Tooltip><TooltipTrigger asChild><Button variant="ghost" size="icon" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? <Sun /> : <Moon />}<span className="sr-only">Toggle theme</span></Button></TooltipTrigger><TooltipContent>Toggle theme</TooltipContent></Tooltip>
          </header>
          <main className="mx-auto w-full max-w-[1680px] p-4 sm:p-6 lg:p-8">
            {view === "fleet" ? <Suspense fallback={<Skeleton className="h-[36rem]" />}><ModelFleet overview={overview} loading={overviewLoading} error={overviewError} onRefresh={() => void loadOverview()} /></Suspense> : build ? <BuildDetail build={build} trace={trace} approvals={approvals} inferenceCalls={stream.snapshot?.inference_calls || []} streamState={stream.state} streamError={stream.error || stream.snapshot?.stream_error || ""} onReload={async () => { await loadBuilds(); await loadSelected() }} /> : builds.length ? <Skeleton className="h-96" /> : <div className="grid min-h-[60vh] place-items-center text-center"><div><Hammer className="mx-auto size-8 text-muted-foreground" /><h1 className="mt-4 text-lg font-semibold">No builds yet</h1><p className="mt-1 text-sm text-muted-foreground">Submit a build and its full execution trace will appear here.</p></div></div>}
          </main>
        </div>
      </div>
    </TooltipProvider>
  )
}

export default App
