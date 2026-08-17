import { useMemo, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { api, compactNumber, formatMs } from "@/lib/api"
import { cn } from "@/lib/utils"
import type {
  ModelCatalogEntry,
  ModelOverview,
  RoutingPolicy,
  RoutingTarget,
} from "@/types"

const WORK_NAMES: Record<string, string> = {
  high: "Planning and architecture",
  med: "Implementation and review",
  low: "Quick maintenance",
}

const PROVIDER_NAMES: Record<string, string> = {
  ollama: "Ollama",
  openrouter: "OpenRouter",
  claude_code: "Claude Code",
  codex: "Codex",
  open_code: "Local and OpenRouter",
  mlx: "MLX",
  lmstudio: "LM Studio",
}

const LANE_COPY: Record<keyof RoutingPolicy, { name: string; description: string }> = {
  planning: {
    name: "Planning and architecture",
    description: "Product planning, architecture, technical leadership, and replanning.",
  },
  implementation: {
    name: "Implementation",
    description: "Coding, QA, integration tests, and CI repair.",
  },
  review: {
    name: "Review and acceptance",
    description: "Code review, verification, and final quality synthesis.",
  },
  maintenance: {
    name: "Repository maintenance",
    description: "Git operations, merges, and other mechanical work.",
  },
}

const LANES = Object.keys(LANE_COPY) as Array<keyof RoutingPolicy>

function providerName(value: string) {
  return PROVIDER_NAMES[value] || value.replaceAll("_", " ")
}

function workName(value: string) {
  const key = value.replace("ctswarm/", "")
  return WORK_NAMES[key] || value
}

function modelWork(model: ModelCatalogEntry) {
  const values = model.tiers.map((tier) => WORK_NAMES[tier]).filter(Boolean)
  return [...new Set(values)].join(" · ") || "No recommended work"
}

function isOpenRouterOnRequest(model: ModelCatalogEntry) {
  return model.backend === "openrouter" && model.installed && !model.circuit_open
}

function rankModels(models: ModelCatalogEntry[]) {
  return [...models].sort((left, right) =>
    (right.benchmark?.quality ?? 0) - (left.benchmark?.quality ?? 0)
    || (right.benchmark?.tokens_per_s ?? 0) - (left.benchmark?.tokens_per_s ?? 0)
    || left.ref.localeCompare(right.ref))
}

function availability(model: ModelCatalogEntry) {
  if (model.routable) return { label: "Available now", tone: "text-emerald-700 dark:text-emerald-400" }
  if (isOpenRouterOnRequest(model)) return { label: "Available when assigned", tone: "text-blue-700 dark:text-blue-400" }
  if (model.circuit_open) return { label: "Temporarily blocked", tone: "text-destructive" }
  if (model.installed) return { label: "Not approved", tone: "text-amber-700 dark:text-amber-400" }
  return { label: "Not installed", tone: "text-muted-foreground" }
}

function readableReason(model: ModelCatalogEntry) {
  if (model.routable) return `Ready for ${modelWork(model).toLowerCase()}.`
  if (isOpenRouterOnRequest(model)) return "Ready through OpenRouter when you assign it to a work category above."
  const reasons = [...new Set(Object.values(model.exclusions))].map((reason) => reason
    .replace("hosted backend excluded by local-only execution policy", "not selected for automatic hosted routing")
    .replace(/not rated for (high|med|low) tier/g, "not approved for this kind of work")
    .replace(/ tier/g, " work"))
  return reasons.join(" · ") || "No route is configured for this model."
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="min-w-0 border-b p-4 last:border-b-0 sm:p-5 xl:border-b-0 xl:border-r xl:last:border-r-0">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-2 break-words text-2xl font-semibold tracking-[-0.03em]">{value}</p>
      <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{detail}</p>
    </div>
  )
}

function ProviderSummary({ overview }: { overview: ModelOverview }) {
  const local = overview.catalog.filter((model) => model.backend === "ollama" && model.routable)
  const hosted = overview.catalog.filter(isOpenRouterOnRequest)
  const providers = [
    {
      name: "Ollama",
      status: local.length ? `${local.length} models ready` : "No model ready",
      detail: local.length ? local.map((model) => model.ref).join(", ") : "Install and benchmark a local model before assigning work.",
      available: local.length > 0,
    },
    {
      name: "OpenRouter",
      status: hosted.length ? `${hosted.length} models available` : "Not configured",
      detail: hosted.length ? "Used only when you explicitly assign a work category to OpenRouter." : "Add an OpenRouter key to make hosted models selectable.",
      available: hosted.length > 0,
    },
    ...(["claude_code", "codex"] as const).map((key) => {
      const capacity = overview.capacity[key]
      return {
        name: providerName(key),
        status: capacity?.available ? "Available" : "Unavailable",
        detail: capacity?.reason || "Capacity has not been reported.",
        available: Boolean(capacity?.available),
      }
    }),
  ]

  return (
    <Card>
      <CardHeader>
        <CardTitle>Provider availability</CardTitle>
        <CardDescription>What can accept work right now. Availability and assignment are separate so a hosted provider is never used by surprise.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-px bg-border p-0 sm:grid-cols-2 xl:grid-cols-4">
        {providers.map((provider) => (
          <section key={provider.name} className="min-w-0 bg-card p-4 sm:p-5">
            <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-2">
              <h3 className="break-words text-sm font-medium">{provider.name}</h3>
              <span className={cn("text-xs", provider.available ? "text-emerald-700 dark:text-emerald-400" : "text-muted-foreground")}>{provider.status}</span>
            </div>
            <p className="mt-3 break-words text-xs leading-5 text-muted-foreground">{provider.detail}</p>
          </section>
        ))}
      </CardContent>
    </Card>
  )
}

function RoutingControls({ overview, onSaved }: { overview: ModelOverview; onSaved: () => void }) {
  const [draft, setDraft] = useState<RoutingPolicy>(() => structuredClone(overview.routing_policy))
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState("")
  const [saveError, setSaveError] = useState("")
  const options = useMemo(() => ({
    ollama: rankModels(overview.catalog.filter((model) => model.backend === "ollama" && model.routable)),
    openrouter: rankModels(overview.catalog.filter(isOpenRouterOnRequest)),
  }), [overview.catalog])
  const dirty = JSON.stringify(draft) !== JSON.stringify(overview.routing_policy)

  function changeTarget(lane: keyof RoutingPolicy, target: RoutingTarget) {
    const models = target === "ollama" || target === "openrouter" ? options[target] : []
    setMessage("")
    setSaveError("")
    setDraft((current) => ({
      ...current,
      [lane]: { target, model: models[0]?.ref || "" },
    }))
  }

  function changeModel(lane: keyof RoutingPolicy, model: string) {
    setMessage("")
    setSaveError("")
    setDraft((current) => ({ ...current, [lane]: { ...current[lane], model } }))
  }

  async function save() {
    setSaving(true)
    setMessage("")
    setSaveError("")
    try {
      const result = await api<{ message: string }>("/api/dashboard/routing-policy", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ policy: draft }),
      })
      setMessage(result.message)
      onSaved()
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error))
    } finally {
      setSaving(false)
    }
  }

  const providerOptions: Array<{ value: RoutingTarget; label: string; disabled?: boolean }> = [
    { value: "auto", label: "Automatic (recommended)" },
    { value: "ollama", label: "Ollama · local", disabled: !options.ollama.length },
    { value: "openrouter", label: "OpenRouter · hosted", disabled: !options.openrouter.length },
    { value: "claude_code", label: "Claude Code", disabled: !overview.capacity.claude_code?.available },
    { value: "codex", label: "Codex", disabled: !overview.capacity.codex?.available },
  ]

  return (
    <Card>
      <CardHeader className="gap-2">
        <CardTitle>Work assignments</CardTitle>
        <CardDescription>Choose where each kind of work goes. Automatic uses available subscription capacity for judgment-heavy work and local models for routine execution.</CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        <div className="border-t">
          {LANES.map((lane) => {
            const assignment = draft[lane]
            const modelOptions = assignment.target === "ollama" || assignment.target === "openrouter" ? options[assignment.target] : []
            return (
              <div key={lane} className="grid min-w-0 gap-4 border-b p-4 last:border-b-0 sm:p-5 lg:grid-cols-[minmax(220px,1fr)_minmax(220px,.8fr)_minmax(240px,1fr)] lg:items-center">
                <div className="min-w-0">
                  <h3 className="break-words text-sm font-medium">{LANE_COPY[lane].name}</h3>
                  <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{LANE_COPY[lane].description}</p>
                </div>
                <label className="min-w-0 text-xs text-muted-foreground">
                  Provider
                  <select
                    aria-label={`${LANE_COPY[lane].name} provider`}
                    value={assignment.target}
                    onChange={(event) => changeTarget(lane, event.target.value as RoutingTarget)}
                    className="mt-1.5 h-10 w-full min-w-0 rounded-[6px] border bg-background px-3 text-sm text-foreground outline-none focus:border-foreground"
                  >
                    {providerOptions.map((option) => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}{option.disabled ? " · unavailable" : ""}</option>)}
                  </select>
                </label>
                <div className="min-w-0">
                  {modelOptions.length ? (
                    <label className="block min-w-0 text-xs text-muted-foreground">
                      Model
                      <select
                        aria-label={`${LANE_COPY[lane].name} model`}
                        value={assignment.model}
                        onChange={(event) => changeModel(lane, event.target.value)}
                        className="mt-1.5 h-10 w-full min-w-0 rounded-[6px] border bg-background px-3 font-mono text-xs text-foreground outline-none focus:border-foreground"
                      >
                        {modelOptions.map((model) => <option key={model.ref} value={model.ref}>{model.ref}</option>)}
                      </select>
                    </label>
                  ) : (
                    <div className="border-l-2 pl-3">
                      <p className="text-xs font-medium">{assignment.target === "auto" ? "Capacity-aware selection" : `Provider default model`}</p>
                      <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{assignment.target === "auto" ? "The strongest available provider is selected within the production budget." : `${providerName(assignment.target)} chooses its compatible default model.`}</p>
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>
        <div className="flex flex-col gap-3 border-t p-4 sm:flex-row sm:items-center sm:justify-between sm:p-5">
          <div className="min-w-0 text-xs leading-5 text-muted-foreground">
            <p>Changes apply to new builds. Running builds keep their submitted assignments.</p>
            {message && <p className="text-emerald-700 dark:text-emerald-400">{message}</p>}
            {saveError && <p className="break-words text-destructive">{saveError}</p>}
          </div>
          <Button className="shrink-0" disabled={!dirty || saving} onClick={save}>{saving ? "Saving…" : "Save assignments"}</Button>
        </div>
      </CardContent>
    </Card>
  )
}

function CurrentChoices({ overview }: { overview: ModelOverview }) {
  const ordered = ["high", "med", "low"].map((name) => Object.values(overview.routes || {}).find((route) => route.alias.endsWith(name))).filter(Boolean)
  return (
    <Card>
      <CardHeader>
        <CardTitle>Current automatic choices</CardTitle>
        <CardDescription>The concrete models automatic routing would select right now. Internal routing aliases are intentionally hidden.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-px bg-border p-0 md:grid-cols-3">
        {ordered.map((route) => route && (
          <section key={route.alias} className="min-w-0 bg-card p-4 sm:p-5">
            <p className="break-words text-xs text-muted-foreground">{workName(route.alias)}</p>
            <p className="mt-2 break-all font-mono text-sm font-medium">{route.model || "No model available"}</p>
            <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{providerName(route.backend || "Provider pending")}{route.degraded_to ? " · using the best available fallback" : ""}</p>
          </section>
        ))}
      </CardContent>
    </Card>
  )
}

type CatalogFilter = "available" | "ollama" | "hosted" | "unavailable" | "all"

function ModelCatalog({ overview }: { overview: ModelOverview }) {
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<CatalogFilter>("available")
  const models = overview.catalog
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return models.filter((model) => {
      const ready = model.routable || isOpenRouterOnRequest(model)
      const matchesFilter = filter === "all"
        || (filter === "available" && ready)
        || (filter === "ollama" && model.backend === "ollama")
        || (filter === "hosted" && model.backend === "openrouter")
        || (filter === "unavailable" && !ready)
      const haystack = `${model.ref} ${model.backend} ${modelWork(model)} ${model.notes} ${readableReason(model)}`.toLowerCase()
      return matchesFilter && (!needle || haystack.includes(needle))
    })
  }, [filter, models, query])

  const filters: Array<[CatalogFilter, string, number]> = [
    ["available", "Available", models.filter((model) => model.routable || isOpenRouterOnRequest(model)).length],
    ["ollama", "Ollama", models.filter((model) => model.backend === "ollama").length],
    ["hosted", "OpenRouter", models.filter((model) => model.backend === "openrouter").length],
    ["unavailable", "Unavailable", models.filter((model) => !model.routable && !isOpenRouterOnRequest(model)).length],
    ["all", "All models", models.length],
  ]

  return (
    <Card>
      <CardHeader>
        <CardTitle>Model catalog</CardTitle>
        <CardDescription>Concrete models only. “Available” means the model can be assigned now; unavailable models remain visible with a plain-language reason.</CardDescription>
      </CardHeader>
      <CardContent>
        {overview.catalog_error && <Alert variant="destructive"><AlertTitle>Catalog partially unavailable</AlertTitle><AlertDescription>{overview.catalog_error}</AlertDescription></Alert>}
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div className="flex max-w-full overflow-x-auto border-b" aria-label="Catalog filters">
            {filters.map(([value, label, count]) => (
              <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)} className={cn("shrink-0 border-b-2 border-transparent px-3 py-2 text-xs text-muted-foreground", filter === value && "border-foreground text-foreground")}>{label} <span className="ml-1 font-mono text-[10px]">{count}</span></button>
            ))}
          </div>
          <Input aria-label="Search model catalog" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search model, provider, or purpose" className="h-10 lg:max-w-sm" />
        </div>
        <div className="mt-4 border">
          <div className="hidden border-b bg-muted/30 px-4 py-2.5 text-[10px] uppercase tracking-[.12em] text-muted-foreground lg:grid lg:grid-cols-[minmax(220px,1.1fr)_minmax(170px,.75fr)_minmax(220px,.9fr)_minmax(260px,1.35fr)] lg:gap-5">
            <span>Model and provider</span><span>Availability</span><span>Recommended work</span><span>Why</span>
          </div>
          {visible.map((model) => {
            const state = availability(model)
            return (
              <article key={`${model.backend}:${model.ref}`} className="grid min-w-0 gap-4 border-b p-4 last:border-b-0 lg:grid-cols-[minmax(220px,1.1fr)_minmax(170px,.75fr)_minmax(220px,.9fr)_minmax(260px,1.35fr)] lg:gap-5">
                <div className="min-w-0">
                  <p className="break-all font-mono text-xs font-medium">{model.ref}</p>
                  <p className="mt-1 break-words text-xs text-muted-foreground">{providerName(model.backend)} · {model.placement.replaceAll("_", " ")}</p>
                </div>
                <div className="min-w-0">
                  <p className={cn("break-words text-xs font-medium", state.tone)}>{state.label}</p>
                  <p className="mt-1 break-words text-[11px] leading-4 text-muted-foreground">{model.warm ? "Loaded in memory" : model.installed ? "Provider connected" : "No local/provider installation"}</p>
                </div>
                <div className="min-w-0">
                  <p className="break-words text-xs leading-5">{modelWork(model)}</p>
                  <p className="mt-1 break-words font-mono text-[10px] leading-4 text-muted-foreground">{model.context >= 1000 ? `${Math.round(model.context / 1000)}K` : model.context} context{model.benchmark ? ` · ${Math.round(model.benchmark.tokens_per_s)} tok/s` : ""}</p>
                </div>
                <div className="min-w-0">
                  <p className="break-words text-xs leading-5 text-muted-foreground">{readableReason(model)}</p>
                  <details className="mt-2 text-xs">
                    <summary className="w-fit cursor-pointer text-foreground">Model notes and evidence</summary>
                    <div className="mt-2 space-y-2 border-l pl-3 text-xs leading-5 text-muted-foreground">
                      <p className="break-words">{model.notes || "No additional catalog notes."}</p>
                      <p className="break-words">{model.benchmark ? `${Math.round(model.benchmark.quality * 100)}% measured quality · ${Math.round(model.benchmark.tool_call_rate * 100)}% tool calls · ${Math.round(model.benchmark.schema_rate * 100)}% structured output` : "No benchmark result is available."}</p>
                    </div>
                  </details>
                </div>
              </article>
            )
          })}
          {!visible.length && <div className="p-10 text-center text-sm text-muted-foreground">No models match this filter and search.</div>}
        </div>
      </CardContent>
    </Card>
  )
}

function RoutingFlow({ overview }: { overview: ModelOverview }) {
  const nodeById = useMemo(() => new Map(overview.graph.nodes.map((node) => [node.id, node])), [overview.graph.nodes])
  const rows = useMemo(() => overview.graph.edges.map((edge) => ({
    source: nodeById.get(edge.source),
    target: nodeById.get(edge.target),
    value: edge.value,
  })).filter((row) => row.source && row.target).sort((a, b) => b.value - a.value).slice(0, 16), [nodeById, overview.graph.edges])
  const max = Math.max(1, ...rows.map((row) => row.value))

  return (
    <div className="border">
      {rows.map((row, index) => (
        <div key={`${row.source!.id}-${row.target!.id}-${index}`} className="grid min-w-0 gap-3 border-b p-3 last:border-b-0 sm:grid-cols-[minmax(150px,.9fr)_minmax(180px,1fr)_minmax(120px,1.4fr)_56px] sm:items-center">
          <div className="min-w-0"><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">Work</p><p className="break-words text-xs">{workName(row.source!.label)}</p></div>
          <div className="min-w-0"><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">Concrete model</p><p className="break-all font-mono text-xs">{workName(row.target!.label)}</p></div>
          <div className="h-1 bg-muted"><div className="h-full bg-foreground" style={{ width: `${Math.max(2, row.value / max * 100)}%` }} /></div>
          <div className="font-mono text-xs sm:text-right">{row.value.toLocaleString()}</div>
        </div>
      ))}
      {!rows.length && <div className="p-8 text-center text-sm text-muted-foreground">Usage appears after the first routed call.</div>}
    </div>
  )
}

function ModelPerformance({ overview }: { overview: ModelOverview }) {
  return (
    <div className="border">
      <div className="hidden border-b bg-muted/30 px-4 py-2.5 text-[10px] uppercase tracking-[.12em] text-muted-foreground lg:grid lg:grid-cols-[minmax(240px,1.3fr)_minmax(150px,.7fr)_repeat(5,minmax(74px,.4fr))] lg:gap-4">
        <span>Model and roles</span><span>Provider</span><span>Live</span><span>Calls</span><span>Success</span><span>Latency</span><span>Tokens</span>
      </div>
      {overview.models.map((model) => (
        <article key={model.name} className="grid min-w-0 gap-4 border-b p-4 last:border-b-0 lg:grid-cols-[minmax(240px,1.3fr)_minmax(150px,.7fr)_repeat(5,minmax(74px,.4fr))] lg:gap-4 lg:items-start">
          <div className="min-w-0"><p className="break-all font-mono text-xs font-medium">{model.name}</p><p className="mt-1 break-words text-[11px] leading-4 text-muted-foreground">{Object.keys(model.roles).join(" · ") || "No role metadata"}</p></div>
          <div className="min-w-0"><p className="break-words text-xs">{model.local ? "Local" : "Hosted"}</p><p className="mt-1 break-words font-mono text-[10px] text-muted-foreground">{model.backends.map(providerName).join(", ") || model.harnesses.join(", ")}</p></div>
          {[
            ["Live", model.live_jobs.toLocaleString()],
            ["Calls", model.calls.toLocaleString()],
            ["Success", model.call_success_rate == null ? "-" : `${Math.round(model.call_success_rate * 100)}%`],
            ["Latency", model.avg_latency_ms ? formatMs(model.avg_latency_ms) : "-"],
            ["Tokens", compactNumber(model.tokens)],
          ].map(([label, value]) => <div key={label} className="min-w-0"><p className="text-[10px] uppercase tracking-[.1em] text-muted-foreground lg:hidden">{label}</p><p className="mt-1 break-words font-mono text-xs lg:mt-0">{value}</p></div>)}
        </article>
      ))}
      {!overview.models.length && <div className="p-8 text-center text-sm text-muted-foreground">No model performance has been recorded.</div>}
    </div>
  )
}

export function ModelFleet({ overview, loading, error, onRefresh }: { overview: ModelOverview | null; loading: boolean; error: string; onRefresh: () => void }) {
  if (!overview && loading) return <div className="grid gap-4 md:grid-cols-2">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-32" />)}</div>
  if (!overview) return <Alert variant="destructive"><AlertTitle>Model telemetry unavailable</AlertTitle><AlertDescription>{error || "The scheduler did not return a fleet snapshot."}</AlertDescription></Alert>

  const { summary } = overview
  const localPercent = Math.round(summary.local_fraction * 100)
  const available = overview.catalog.filter((model) => model.routable || isOpenRouterOnRequest(model)).length

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <p className="mb-2 text-xs text-emerald-700 dark:text-emerald-400">Routing services online</p>
          <h1 className="break-words text-2xl font-semibold tracking-[-0.03em] sm:text-3xl">Models and routing</h1>
          <p className="mt-1 max-w-3xl break-words text-sm leading-6 text-muted-foreground">See what is available, choose which provider handles each kind of work, and verify what the swarm actually used.</p>
        </div>
        <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>{loading ? "Refreshing…" : "Refresh data"}</Button>
      </div>
      {error && <Alert variant="destructive"><AlertTitle>Partial telemetry</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}

      <Card className="grid overflow-hidden xl:grid-cols-4">
        <Metric label="Available models" value={available.toLocaleString()} detail={`${overview.catalog.filter((model) => model.routable).length} automatic · ${overview.catalog.filter(isOpenRouterOnRequest).length} available on assignment`} />
        <Metric label="Local usage" value={`${localPercent}%`} detail={`${summary.local_calls.toLocaleString()} of ${summary.router_calls.toLocaleString()} measured calls`} />
        <Metric label="Active agents" value={summary.active_executions.toString()} detail={`${summary.local_active_executions} currently using local inference`} />
        <Metric label="Recorded spend" value={`$${summary.cost_usd.toFixed(2)}`} detail={`${summary.router_failures} backend failures · ${summary.routing_rejections} rejected routes`} />
      </Card>

      <ProviderSummary overview={overview} />
      <RoutingControls key={JSON.stringify(overview.routing_policy)} overview={overview} onSaved={onRefresh} />
      <CurrentChoices overview={overview} />
      <ModelCatalog overview={overview} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,.75fr)]">
        <Card>
          <CardHeader><CardTitle>Observed routing</CardTitle><CardDescription>Recent work mapped to the concrete model that served it.</CardDescription></CardHeader>
          <CardContent><RoutingFlow overview={overview} /></CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Usage summary</CardTitle><CardDescription>Actual calls, tokens, and provider mix from the last {overview.window_hours} hours.</CardDescription></CardHeader>
          <CardContent>
            <div className="flex h-2 overflow-hidden bg-muted"><div className="bg-foreground" style={{ width: `${localPercent}%` }} /><div className="bg-muted-foreground" style={{ width: `${100 - localPercent}%` }} /></div>
            <div className="mt-4 grid grid-cols-2 gap-4">
              <div><p className="font-mono text-lg font-medium">{summary.local_calls.toLocaleString()}</p><p className="mt-1 text-xs text-muted-foreground">Local calls</p></div>
              <div><p className="font-mono text-lg font-medium">{Math.max(0, summary.router_calls - summary.local_calls).toLocaleString()}</p><p className="mt-1 text-xs text-muted-foreground">Hosted calls</p></div>
              <div><p className="font-mono text-lg font-medium">{compactNumber(summary.tokens)}</p><p className="mt-1 text-xs text-muted-foreground">Reported tokens</p></div>
              <div><p className="font-mono text-lg font-medium">{summary.builds.toLocaleString()}</p><p className="mt-1 text-xs text-muted-foreground">Builds measured</p></div>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Model performance</CardTitle><CardDescription>Concrete models only, with their actual roles and observed results. Long names and role lists wrap instead of being clipped.</CardDescription></CardHeader>
        <CardContent className="p-0"><ModelPerformance overview={overview} /></CardContent>
      </Card>
    </div>
  )
}
