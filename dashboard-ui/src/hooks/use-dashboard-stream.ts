import { useEffect, useState } from "react"

import type { DashboardSnapshot, StreamState } from "@/types"

export function useDashboardStream(buildId: string) {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null)
  const [state, setState] = useState<StreamState>("connecting")
  const [error, setError] = useState("")

  useEffect(() => {
    const query = buildId ? `?build_id=${encodeURIComponent(buildId)}` : ""
    const source = new EventSource(`/api/dashboard/stream${query}`)

    source.onopen = () => {
      setState("live")
      setError("")
    }
    source.addEventListener("snapshot", (event) => {
      try {
        setSnapshot(JSON.parse((event as MessageEvent<string>).data) as DashboardSnapshot)
        setState("live")
        setError("")
      } catch {
        setError("The live stream returned an unreadable frame.")
      }
    })
    source.addEventListener("stream-error", (event) => {
      try {
        const value = JSON.parse((event as MessageEvent<string>).data) as { message?: string }
        setError(value.message || "Live telemetry is partially unavailable.")
      } catch {
        setError("Live telemetry is partially unavailable.")
      }
    })
    source.onerror = () => {
      setState("reconnecting")
      setError("Connection interrupted. Retrying automatically.")
    }

    return () => source.close()
  }, [buildId])

  return { snapshot, state, error }
}
