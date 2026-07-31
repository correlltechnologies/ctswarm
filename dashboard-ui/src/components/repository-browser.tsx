import { useCallback, useEffect, useMemo, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { GitCommit, GitWorktree, ProjectDetails, ProjectSummary } from "@/types"

function providerName(value: string) {
  const names: Record<string, string> = { github: "GitHub", bitbucket: "Bitbucket", gitlab: "GitLab", azure_devops: "Azure DevOps", other: "Other Git", local: "Local" }
  return names[value] || value.replaceAll("_", " ")
}

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
}

function WorktreeRow({ worktree }: { worktree: GitWorktree }) {
  const state = worktree.locked ? "Locked" : worktree.prunable ? "Prunable" : worktree.detached ? "Detached" : worktree.current ? "Current checkout" : "In progress"
  return (
    <article className="grid min-w-0 gap-3 border-b p-4 last:border-b-0 md:grid-cols-[minmax(0,1.4fr)_minmax(160px,.7fr)_110px] md:items-center">
      <div className="min-w-0"><p className="break-all font-mono text-xs">{worktree.path}</p><p className="mt-1 break-words text-xs text-muted-foreground">{worktree.branch || "No branch"}</p></div>
      <div className="min-w-0"><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">HEAD</p><p className="mt-1 break-all font-mono text-xs">{worktree.head || "Unknown"}</p></div>
      <p className="break-words text-xs md:text-right">{state}</p>
    </article>
  )
}

function CommitRow({ commit }: { commit: GitCommit }) {
  return (
    <article className="grid min-w-0 gap-3 border-b p-4 last:border-b-0 lg:grid-cols-[90px_minmax(0,1fr)_minmax(180px,.45fr)] lg:items-start">
      <p className="break-all font-mono text-xs text-muted-foreground">{commit.short}</p>
      <div className="min-w-0"><p className="break-words text-sm font-medium">{commit.subject}</p>{commit.refs && <p className="mt-1 break-words font-mono text-[10px] text-muted-foreground">{commit.refs}</p>}</div>
      <div className="min-w-0 text-xs text-muted-foreground lg:text-right"><p className="break-words">{commit.author}</p><p className="mt-1 break-words">{formatDate(commit.committed_at)}</p></div>
    </article>
  )
}

export function RepositoryBrowser({ onLaunch }: { onLaunch: (projectId: string) => void }) {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [selected, setSelected] = useState("")
  const [details, setDetails] = useState<ProjectDetails | null>(null)
  const [query, setQuery] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return needle ? projects.filter((project) => `${project.name} ${project.relative_path} ${project.remote_url} ${project.branch}`.toLowerCase().includes(needle)) : projects
  }, [projects, query])

  const loadProjects = useCallback(async () => {
    setError("")
    const result = await api<{ projects: ProjectSummary[] }>("/api/projects")
    setProjects(result.projects || [])
    setSelected((current) => current || result.projects[0]?.id || "")
  }, [])

  const loadDetails = useCallback(async (projectId: string) => {
    if (!projectId) { setDetails(null); return }
    setLoading(true)
    try { setDetails(await api<ProjectDetails>(`/api/projects/${encodeURIComponent(projectId)}?history_limit=40`)) }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : String(nextError)) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    // Network callbacks commit only after their requests resolve.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadProjects().catch((nextError) => { setError(nextError instanceof Error ? nextError.message : String(nextError)); setLoading(false) })
  }, [loadProjects])
  useEffect(() => {
    // Selection changes synchronize the details pane with an external API.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadDetails(selected)
  }, [loadDetails, selected])

  async function refresh() {
    setLoading(true)
    try { await Promise.all([loadProjects(), loadDetails(selected)]) }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : String(nextError)) }
    finally { setLoading(false) }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0"><p className="font-mono text-[11px] text-muted-foreground">REPOSITORY WORKSPACE</p><h1 className="mt-2 break-words text-2xl font-semibold tracking-[-0.03em] sm:text-3xl">Git history and active worktrees</h1><p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">Inspect every project under your configured folder before launching work. Repository files are mounted read-only; swarms build in isolated clones.</p></div>
        <Button variant="outline" onClick={() => void refresh()} disabled={loading}>{loading ? "Refreshing…" : "Refresh repositories"}</Button>
      </header>

      {error && <Alert variant="destructive"><AlertTitle>Repository data is incomplete</AlertTitle><AlertDescription className="break-words">{error}</AlertDescription></Alert>}

      <div className="grid min-w-0 gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card className="min-w-0 max-h-[32rem] overflow-y-auto xl:sticky xl:top-20 xl:max-h-[calc(100vh-7rem)] xl:self-start">
          <CardHeader><CardTitle>Projects</CardTitle><CardDescription>{projects.length} Git repositories found</CardDescription></CardHeader>
          <CardContent className="p-0">
            <div className="border-y p-3"><Input aria-label="Search repositories" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search repositories" /></div>
            <div className="divide-y">
              {visible.map((project) => (
                <button key={project.id} onClick={() => setSelected(project.id)} className={cn("w-full min-w-0 p-4 text-left transition-colors hover:bg-muted/50", selected === project.id && "bg-muted")}>
                  <span className="flex min-w-0 flex-wrap items-baseline justify-between gap-2"><span className="break-words text-sm font-medium">{project.name}</span><span className="text-[11px] text-muted-foreground">{providerName(project.scm_provider)}</span></span>
                  <span className="mt-2 block break-words font-mono text-[10px] leading-4 text-muted-foreground">{project.relative_path}</span>
                  <span className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground"><span>{project.branch}</span><span>{project.worktree_count} worktrees</span><span>{project.dirty ? "Local changes" : "Clean"}</span></span>
                </button>
              ))}
              {!visible.length && <p className="p-6 text-center text-sm text-muted-foreground">No repository matches this search.</p>}
            </div>
          </CardContent>
        </Card>

        <div className="min-w-0 space-y-4">
          {loading && !details ? <Skeleton className="h-[36rem]" /> : details ? (
            <>
              <Card>
                <CardHeader className="gap-3">
                  <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between"><div className="min-w-0"><CardTitle className="break-words text-xl">{details.name}</CardTitle><CardDescription className="mt-2 break-all font-mono">{details.path}</CardDescription></div><Button className="shrink-0" onClick={() => onLaunch(details.id)}>Start a swarm here</Button></div>
                </CardHeader>
                <CardContent className="grid gap-px bg-border p-0 sm:grid-cols-2 lg:grid-cols-4">
                  {[
                    ["Provider", providerName(details.scm_provider)],
                    ["Branch", details.branch],
                    ["Remote sync", `${details.ahead} ahead · ${details.behind} behind`],
                    ["Working tree", details.dirty ? `${details.staged} staged · ${details.modified} modified · ${details.untracked} untracked` : "Clean"],
                  ].map(([label, value]) => <div key={label} className="min-w-0 bg-card p-4"><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">{label}</p><p className="mt-2 break-words text-xs leading-5">{value}</p></div>)}
                </CardContent>
                <div className="min-w-0 border-t p-4"><p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">Origin</p><p className="mt-2 break-all font-mono text-xs">{details.remote_url || "No remote configured"}</p></div>
              </Card>

              <Card>
                <CardHeader><CardTitle>Worktrees in progress</CardTitle><CardDescription>Every checkout registered with this repository, including isolated feature branches.</CardDescription></CardHeader>
                <CardContent className="p-0 border-t">{details.worktrees.map((worktree) => <WorktreeRow key={worktree.path} worktree={worktree} />)}{!details.worktrees.length && <p className="p-6 text-sm text-muted-foreground">No worktrees are registered.</p>}</CardContent>
              </Card>

              <Card>
                <CardHeader><CardTitle>Recent history</CardTitle><CardDescription>The latest commits on the currently checked-out branch.</CardDescription></CardHeader>
                <CardContent className="p-0 border-t">{details.history.map((commit) => <CommitRow key={commit.commit} commit={commit} />)}{!details.history.length && <p className="p-6 text-sm text-muted-foreground">No commits were found.</p>}</CardContent>
              </Card>
            </>
          ) : <Card><CardContent className="p-10 text-center text-sm text-muted-foreground">Select a repository to inspect its history and worktrees.</CardContent></Card>}
        </div>
      </div>
    </div>
  )
}
