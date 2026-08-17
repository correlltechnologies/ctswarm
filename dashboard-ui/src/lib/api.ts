export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const message = body.detail || body.error || `${response.status} ${response.statusText}`
    throw new Error(typeof message === "string" ? message : JSON.stringify(message))
  }
  return body as T
}

export function repoName(url: string) {
  return (url || "Unknown repository")
    .replace(/\.git$/, "")
    .split("/")
    .filter(Boolean)
    .pop()
}

export function formatDuration(seconds: number) {
  const value = Math.max(0, Number(seconds || 0))
  if (value < 60) return `${Math.floor(value)}s`
  if (value < 3600) return `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`
}

export function formatMs(ms?: number, started?: string, completed?: string) {
  if (ms != null) return formatDuration(ms / 1000)
  const start = Date.parse(started || "")
  const end = completed ? Date.parse(completed) : Date.now()
  return Number.isFinite(start) && Number.isFinite(end)
    ? formatDuration((end - start) / 1000)
    : "-"
}

export function compactNumber(value: number) {
  return new Intl.NumberFormat([], { notation: "compact", maximumFractionDigits: 1 }).format(value)
}
