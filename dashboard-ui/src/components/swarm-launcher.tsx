import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { api } from "@/lib/api"
import type { Build, McpServer, ProjectSummary } from "@/types"

const selectClass = "h-10 w-full min-w-0 rounded-[6px] border bg-background px-3 text-sm outline-none focus-visible:border-foreground focus-visible:ring-2 focus-visible:ring-ring/30"
const textareaClass = "min-h-40 w-full resize-y rounded-[6px] border-0 bg-transparent px-0 py-1 text-base leading-7 outline-none placeholder:text-muted-foreground focus-visible:ring-0"

const SCM_OPTIONS = [
  ["github", "GitHub"],
  ["bitbucket", "Bitbucket"],
  ["gitlab", "GitLab"],
  ["azure_devops", "Azure DevOps"],
  ["other", "Other Git remote"],
  ["local", "Local repository"],
] as const

function providerName(value: string) {
  return SCM_OPTIONS.find(([id]) => id === value)?.[1] || value.replaceAll("_", " ")
}

function ProjectContext({ project }: { project?: ProjectSummary }) {
  if (!project) return null
  return (
    <div className="grid gap-px border-t bg-border sm:grid-cols-2 lg:grid-cols-4">
      {[
        ["Repository", project.relative_path],
        ["Provider", providerName(project.scm_provider)],
        ["Current branch", project.branch],
        ["Local state", project.dirty ? `${project.modified + project.staged + project.untracked} uncommitted changes` : "Clean working tree"],
      ].map(([label, value]) => (
        <div key={label} className="min-w-0 bg-card p-4">
          <p className="text-[10px] uppercase tracking-[.12em] text-muted-foreground">{label}</p>
          <p className="mt-2 break-words text-xs leading-5">{value}</p>
        </div>
      ))}
    </div>
  )
}

export function SwarmLauncher({ initialProjectId = "", onLaunched }: { initialProjectId?: string; onLaunched: (build: Build) => void }) {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [projectId, setProjectId] = useState("")
  const [manualRemote, setManualRemote] = useState("")
  const [scmProvider, setScmProvider] = useState("github")
  const [sourceBranch, setSourceBranch] = useState("")
  const [goal, setGoal] = useState("")
  const [servers, setServers] = useState<McpServer[]>([])
  const [selectedServers, setSelectedServers] = useState<string[]>([])
  const [inheritMcp, setInheritMcp] = useState(true)
  const [strongPlanning, setStrongPlanning] = useState(true)
  const [createPullRequest, setCreatePullRequest] = useState(true)
  const [maxHours, setMaxHours] = useState("0")
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState("")

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === projectId),
    [projectId, projects],
  )
  const manual = projectId === "manual"

  const loadProjects = useCallback(async () => {
    const result = await api<{ projects: ProjectSummary[] }>("/api/projects")
    const next = result.projects || []
    setProjects(next)
    const project = next.find((item) => item.id === initialProjectId)
    if (project) {
      setProjectId(project.id)
      setScmProvider(project.scm_provider)
      setSourceBranch(project.branch === "Detached HEAD" ? "" : project.branch)
      setCreatePullRequest(project.scm_provider === "github")
    }
  }, [initialProjectId])

  const loadServers = useCallback(async (nextProjectId: string) => {
    const query = nextProjectId && nextProjectId !== "manual" ? `?project_id=${encodeURIComponent(nextProjectId)}` : ""
    const result = await api<{ servers: McpServer[] }>(`/api/mcp-servers${query}`)
    const next = result.servers || []
    setServers(next)
    setSelectedServers(next.filter((server) => server.available).map((server) => server.id))
  }, [])

  useEffect(() => {
    // Network callbacks commit only after the inventory request resolves.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadProjects()
      .catch((nextError) => setError(nextError instanceof Error ? nextError.message : String(nextError)))
      .finally(() => setLoading(false))
  }, [loadProjects])
  useEffect(() => {
    // MCP inventory is external state scoped to the selected repository.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadServers(projectId).catch((nextError) => {
      setError(nextError instanceof Error ? nextError.message : String(nextError))
    })
  }, [loadServers, projectId])

  function changeProject(nextId: string) {
    setProjectId(nextId)
    setError("")
    const project = projects.find((item) => item.id === nextId)
    if (project) {
      setScmProvider(project.scm_provider)
      setSourceBranch(project.branch === "Detached HEAD" ? "" : project.branch)
      setCreatePullRequest(project.scm_provider === "github")
    }
  }

  function toggleServer(id: string) {
    setSelectedServers((current) => current.includes(id)
      ? current.filter((item) => item !== id)
      : [...current, id])
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError("")
    try {
      const build = await api<Build>("/api/swarms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal: goal.trim(),
          project_id: manual ? "" : projectId,
          repo_url: manual ? manualRemote.trim() : "",
          scm_provider: scmProvider,
          source_branch: sourceBranch.trim(),
          create_pull_request: createPullRequest,
          inherit_mcp: inheritMcp,
          mcp_servers: inheritMcp ? selectedServers : [],
          require_strong_planning: strongPlanning,
          max_hours: Number(maxHours) || 0,
        }),
      })
      onLaunched(build)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError))
    } finally {
      setSubmitting(false)
    }
  }

  const canSubmit = Boolean(goal.trim() && (manual ? manualRemote.trim() : projectId))

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-2 sm:py-8">
      <header className="mx-auto max-w-3xl text-center">
        <p className="font-mono text-[11px] text-muted-foreground">NEW SWARM</p>
        <h1 className="mt-3 text-3xl font-semibold tracking-[-0.04em] sm:text-4xl">What should the swarm build?</h1>
        <p className="mx-auto mt-3 max-w-2xl text-sm leading-6 text-muted-foreground">Choose a repository, describe the outcome, and send it. Mission Control carries your Git provider, routing policy, and existing Claude/Codex MCP setup into the build.</p>
      </header>

      {error && <Alert variant="destructive"><AlertTitle>Could not start the swarm</AlertTitle><AlertDescription className="break-words">{error}</AlertDescription></Alert>}

      <form onSubmit={submit} className="space-y-4">
        <Card className="overflow-hidden shadow-[0_1px_1px_rgba(0,0,0,.04),0_8px_24px_-12px_rgba(0,0,0,.18)]">
          <CardHeader className="border-b">
            <div className="grid gap-4 md:grid-cols-[minmax(0,1.5fr)_minmax(180px,.7fr)]">
              <label className="min-w-0 space-y-2 text-xs font-medium">
                Project folder or remote
                <select aria-label="Project folder or remote" className={selectClass} value={projectId} onChange={(event) => changeProject(event.target.value)} disabled={loading}>
                  <option value="">Choose a project</option>
                  {projects.map((project) => <option key={project.id} value={project.id}>{project.relative_path}</option>)}
                  <option value="manual">Enter a remote URL</option>
                </select>
              </label>
              <label className="min-w-0 space-y-2 text-xs font-medium">
                Git provider
                <select aria-label="Git provider" className={selectClass} value={scmProvider} onChange={(event) => { setScmProvider(event.target.value); setCreatePullRequest(event.target.value === "github") }}>
                  {SCM_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
            </div>
            {manual && <label className="mt-4 block space-y-2 text-xs font-medium">Repository URL<Input value={manualRemote} onChange={(event) => setManualRemote(event.target.value)} placeholder="git@bitbucket.org:team/project.git" className="font-mono text-xs" /></label>}
          </CardHeader>
          <CardContent className="p-5 sm:p-6">
            <label htmlFor="swarm-goal" className="sr-only">What should the swarm build?</label>
            <textarea id="swarm-goal" className={textareaClass} value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="Describe the feature, fix, or application you want delivered. Include the user flow and what must be true before it is accepted." />
          </CardContent>
          <div className="flex flex-col gap-3 border-t bg-muted/20 p-4 sm:flex-row sm:items-center sm:justify-between">
            <p className="min-w-0 break-words text-xs leading-5 text-muted-foreground">{selectedProject?.remote_url || (manual ? manualRemote || "Remote URL required" : "Select the exact repository before sending")}</p>
            <Button type="submit" className="shrink-0" disabled={!canSubmit || submitting}>{submitting ? "Starting swarm…" : "Start swarm"}</Button>
          </div>
          <ProjectContext project={selectedProject} />
        </Card>

        {selectedProject?.dirty && <Alert><AlertTitle>Local changes are not included</AlertTitle><AlertDescription>The swarm starts from the selected remote and branch. Commit or push the {selectedProject.staged + selectedProject.modified + selectedProject.untracked} local changes first if they belong in the build.</AlertDescription></Alert>}

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader><CardTitle>Delivery</CardTitle><CardDescription>Where the isolated build starts and how finished work returns to your workflow.</CardDescription></CardHeader>
            <CardContent className="space-y-4">
              <label className="block space-y-2 text-xs font-medium">Starting branch<Input value={sourceBranch} onChange={(event) => setSourceBranch(event.target.value)} placeholder="Remote default branch" /></label>
              <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-4 accent-foreground" checked={createPullRequest} disabled={scmProvider !== "github"} onChange={(event) => setCreatePullRequest(event.target.checked)} /><span><span className="block font-medium">Create a pull request</span><span className="mt-1 block text-xs leading-5 text-muted-foreground">Automatic publishing is currently available for GitHub. Other providers run and verify the build, but do not publish it automatically yet.</span></span></label>
              <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-4 accent-foreground" checked={strongPlanning} onChange={(event) => setStrongPlanning(event.target.checked)} /><span><span className="block font-medium">Strong planning and acceptance</span><span className="mt-1 block text-xs leading-5 text-muted-foreground">Use subscription capacity for planning, issue definition, independent review, and acceptance when available.</span></span></label>
              <label className="block space-y-2 text-xs font-medium">Build deadline in hours<Input type="number" min="0" max="720" step="0.5" value={maxHours} onChange={(event) => setMaxHours(event.target.value)} /><span className="block font-normal leading-5 text-muted-foreground">Use 0 for no automatic deadline. Pause and stop remain available.</span></label>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>Existing MCP context</CardTitle><CardDescription>These registrations are read from your Claude and Codex configuration. Secrets and command details never appear here.</CardDescription></CardHeader>
            <CardContent className="space-y-4">
              <label className="flex items-start gap-3 text-sm"><input type="checkbox" className="mt-1 size-4 accent-foreground" checked={inheritMcp} onChange={(event) => setInheritMcp(event.target.checked)} /><span><span className="block font-medium">Inherit configured MCP servers</span><span className="mt-1 block text-xs leading-5 text-muted-foreground">Selected tools are named in build context and loaded by their existing runtime configuration.</span></span></label>
              <div className="divide-y border-y">
                {servers.map((server) => (
                  <label key={server.id} className="flex min-w-0 items-start gap-3 py-3 text-sm">
                    <input type="checkbox" className="mt-1 size-4 accent-foreground" disabled={!inheritMcp || !server.available} checked={inheritMcp && selectedServers.includes(server.id)} onChange={() => toggleServer(server.id)} />
                    <span className="min-w-0"><span className="flex flex-wrap items-baseline justify-between gap-2"><span className="break-words font-medium">{server.name}</span><span className="text-xs text-muted-foreground">{server.runtime} · {server.transport}</span></span><span className="mt-1 block break-words text-xs leading-5 text-muted-foreground">{server.note}</span></span>
                  </label>
                ))}
                {!servers.length && <p className="py-5 text-sm text-muted-foreground">No Claude or Codex MCP servers were discovered.</p>}
              </div>
            </CardContent>
          </Card>
        </div>
      </form>
    </div>
  )
}
