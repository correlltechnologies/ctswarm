"""Approval service: receives escalations, delivers cards, records decisions.

SWE-AF already has a human-in-the-loop path (``swe_af/hitl/ask_user.py``) that
posts an approval request and waits on a webhook reply. This service is the
surface for that path, not a reimplementation of it.

Three invariants the whole design turns on:

1. **Silence is never approval.** An expired request resolves to PAUSE.
2. **Exactly one card per action.** Deduplication is content-derived, because
   retries and replans re-raise the same action many times per build.
3. **The local UI always works.** Slack may be unconfigured or down; an approval
   that cannot be seen is an approval that cannot be granted, and a CRITICAL
   request must never be silently swallowed by a notification channel.
"""

from __future__ import annotations

import json
import os
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..ledger import Ledger
from .rules import Decision, classify
from .slack import SlackNotifier, verify_signature
from .store import ApprovalStore

app = FastAPI(title="ctswarm approvals", version="0.1.0")


def _store() -> ApprovalStore:
    return ApprovalStore(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))


def _ledger() -> Ledger:
    return Ledger(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))


def _notifier() -> SlackNotifier:
    return SlackNotifier(
        bot_token=os.environ.get("SLACK_BOT_TOKEN"),
        channel=os.environ.get("SLACK_APPROVAL_CHANNEL"),
        signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    )


@app.get("/health")
async def health() -> JSONResponse:
    notifier = _notifier()
    return JSONResponse(
        {
            "ok": True,
            "slack_configured": notifier.configured,
            # The local UI is always available, which is what guarantees an
            # approval is actionable even with no Slack workspace at all.
            "local_ui": True,
            "pending": len(_store().pending()),
        }
    )


@app.post("/approval/request")
async def request_approval(request: Request) -> JSONResponse:
    """Entry point for SWE-AF escalations.

    The caller does not decide whether approval is needed; this service does,
    using deterministic rules. An agent must not be able to argue that its own
    action is routine.
    """
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    action = payload.get("action") or ""
    if not action:
        return JSONResponse({"error": "action is required"}, status_code=400)

    approval = classify(
        action=action,
        detail=payload.get("detail", "") or "",
        build_id=payload.get("build_id", "") or "",
        repo=payload.get("repo", "") or "",
        branch=payload.get("branch", "") or "",
        pull_request=payload.get("pull_request", "") or "",
        environment=payload.get("environment", "") or "",
        files_affected=tuple(payload.get("files_affected") or ()),
        evidence=payload.get("evidence") or {},
        alternatives=tuple(payload.get("alternatives") or ()),
        recommendation=payload.get("recommendation", "") or "",
        estimated_cost_usd=float(payload.get("estimated_cost_usd") or 0.0),
        retry_history=tuple(payload.get("retry_history") or ()),
    )

    ledger = _ledger()

    if approval is None:
        # Explicitly autonomous. Recorded, not notified: the plan is clear that
        # routine recoverable events must not generate cards, and over-notifying
        # trains reflex approval, which is worse than no gate.
        ledger.record_event(
            "approval_not_required",
            {"action": action, "detail": payload.get("detail", "")[:300]},
            build_id=payload.get("build_id"),
        )
        return JSONResponse({"approval_required": False, "decision": "approve"})

    store = _store()
    is_new, row = store.create(approval)

    if not is_new:
        # Same action re-raised by a retry or replan. Return current state
        # without sending a second card.
        current = store.current_decision(approval.dedupe_key)
        ledger.record_event(
            "approval_deduplicated",
            {"dedupe_key": approval.dedupe_key, "action": action},
            build_id=approval.build_id,
        )
        return JSONResponse(
            {
                "approval_required": True,
                "dedupe_key": approval.dedupe_key,
                "duplicate": True,
                "decision": current["decision"] if current else None,
            }
        )

    notifier = _notifier()
    delivered, message_ref = await notifier.post(approval, approval.dedupe_key)
    channel = "slack" if delivered else "local"
    store.mark_notified(
        approval.dedupe_key, channel=channel, message_ref=message_ref if delivered else ""
    )

    ledger.record_event(
        "approval_requested",
        {
            "dedupe_key": approval.dedupe_key,
            "action": action,
            "risk": approval.risk.value,
            "rule": approval.rule_name,
            "delivered_via": channel,
            "delivery_detail": message_ref,
        },
        build_id=approval.build_id,
    )

    return JSONResponse(
        {
            "approval_required": True,
            "dedupe_key": approval.dedupe_key,
            "risk": approval.risk.value,
            "delivered_via": channel,
            "local_url": f"/approvals/{approval.dedupe_key}",
        }
    )


@app.get("/approval/status/{dedupe_key}")
async def approval_status(dedupe_key: str) -> JSONResponse:
    """Poll a decision.

    An expired-and-undecided request reports PAUSE. This is the single most
    important behavior in the service: a build that loses its notification
    channel must stop, never continue.
    """
    store = _store()
    record = store.get(dedupe_key)
    if not record:
        return JSONResponse({"error": "unknown approval"}, status_code=404)

    decision = record.get("decision")
    if decision:
        return JSONResponse(
            {
                "resolved": True,
                "decision": decision["decision"],
                "decided_by": decision["decided_by"],
                "note": decision["note"],
            }
        )

    if record["expires_at"] < time.time():
        return JSONResponse(
            {
                "resolved": True,
                "decision": Decision.EXPIRED.value,
                "effect": "pause",
                "note": "expired without a response; build pauses, never proceeds",
            }
        )

    return JSONResponse({"resolved": False, "decision": None})


@app.post("/approvals/{dedupe_key}/decide")
async def decide(dedupe_key: str, request: Request) -> JSONResponse:
    """Record a decision from the local UI or an operator script."""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}

    raw = (payload.get("decision") or "").lower()
    try:
        decision = Decision(raw)
    except ValueError:
        return JSONResponse(
            {"error": f"decision must be one of {[d.value for d in Decision]}"},
            status_code=400,
        )
    if decision is Decision.EXPIRED:
        # Expiry is derived from time, never asserted by a caller. Allowing it to
        # be posted would let a client fabricate a terminal state.
        return JSONResponse({"error": "expired is not settable"}, status_code=400)

    store = _store()
    record = store.get(dedupe_key)
    if not record:
        return JSONResponse({"error": "unknown approval"}, status_code=404)
    if record.get("decision"):
        return JSONResponse(
            {"error": "already decided", "decision": record["decision"]},
            status_code=409,
        )
    if record["expires_at"] < time.time():
        return JSONResponse(
            {
                "error": "approval expired; the build remains paused",
                "decision": Decision.EXPIRED.value,
            },
            status_code=409,
        )

    result = store.decide(
        dedupe_key,
        decision,
        decided_by=payload.get("decided_by", "local"),
        note=payload.get("note", ""),
        source="local",
    )
    _ledger().record_event(
        "approval_decided",
        {"dedupe_key": dedupe_key, "decision": decision.value, "source": "local"},
        build_id=record["build_id"],
    )

    notifier = _notifier()
    if record.get("message_ref"):
        await notifier.update_resolved(
            record["message_ref"],
            decision=decision.value,
            decided_by=payload.get("decided_by", "local"),
        )

    return JSONResponse({"ok": True, "decision": result})


@app.post("/slack/interactivity")
async def slack_interactivity(request: Request) -> JSONResponse:
    """Slack button callbacks.

    Signature verification is mandatory. This endpoint decides whether
    irreversible actions proceed, so accepting an unsigned POST would be a direct
    path to approving a production deploy from anywhere on the internet.
    """
    body = await request.body()
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        return JSONResponse(
            {"error": "SLACK_SIGNING_SECRET not configured; refusing unsigned callback"},
            status_code=503,
        )
    if not verify_signature(
        signing_secret=secret,
        timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
        signature=request.headers.get("X-Slack-Signature", ""),
        body=body,
    ):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    parsed = parse_qs(body.decode("utf-8"))
    raw_payload = (parsed.get("payload") or ["{}"])[0]
    try:
        interaction = json.loads(raw_payload)
    except ValueError:
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    actions = interaction.get("actions") or []
    if not actions:
        return JSONResponse({"ok": True})

    action_id = actions[0].get("action_id", "")
    dedupe_key = actions[0].get("value", "")
    user = (interaction.get("user") or {}).get("username", "slack-user")

    # Build controls (pause / resume / stop) are not approvals. They are
    # always-safe owner actions on a live build, so they take a separate path
    # and never touch the approval record.
    if action_id.startswith("build_"):
        from ..orchestrator import Orchestrator

        orchestrator = Orchestrator(ledger=_ledger())
        build_id = dedupe_key
        if action_id == "build_pause":
            orchestrator.request_pause(build_id, who=user)
            return JSONResponse({"text": f"Pausing {build_id}. No new work will start."})
        if action_id == "build_resume":
            orchestrator.request_resume(build_id, who=user)
            return JSONResponse({"text": f"Resuming {build_id}."})
        if action_id == "build_stop":
            orchestrator.request_stop(build_id, who=user)
            return JSONResponse(
                {"text": f"Stopping {build_id}. Work so far stays on its branch."}
            )
        return JSONResponse({"error": f"unknown build action {action_id}"}, status_code=400)

    try:
        decision = Decision(action_id)
    except ValueError:
        return JSONResponse({"error": f"unknown action {action_id}"}, status_code=400)

    store = _store()
    record = store.get(dedupe_key)
    if not record:
        return JSONResponse({"error": "unknown approval"}, status_code=404)
    if record.get("decision"):
        return JSONResponse({"text": "This request was already decided."})

    store.decide(dedupe_key, decision, decided_by=user, source="slack")
    _ledger().record_event(
        "approval_decided",
        {"dedupe_key": dedupe_key, "decision": decision.value, "source": "slack"},
        build_id=record["build_id"],
    )

    if record.get("message_ref"):
        await _notifier().update_resolved(
            record["message_ref"], decision=decision.value, decided_by=user
        )

    return JSONResponse({"text": f"Recorded: {decision.value}"})


@app.get("/", response_class=HTMLResponse)
@app.get("/approvals", response_class=HTMLResponse)
async def local_ui() -> HTMLResponse:
    """Zero-setup approval UI.

    Deliberately dependency-free and server-rendered. This is the fallback that
    makes the whole approval design honest: it works with no Slack workspace, no
    app manifest, and no public callback URL.
    """
    store = _store()
    pending = store.pending()

    rows = []
    for record in pending:
        payload = record["payload"]
        expired = record["expired"]
        badge = "EXPIRED (build paused)" if expired else record["risk"].upper()
        files = ", ".join(payload.get("files_affected", [])[:6]) or "n/a"
        evidence = payload.get("evidence") or {}
        evidence_html = (
            "".join(
                f"<li><b>{_esc(k)}</b>: {_esc(str(v))}</li>"
                for k, v in list(evidence.items())[:8]
            )
            or "<li>none recorded</li>"
        )
        buttons = (
            ""
            if expired
            else f"""
        <div class="actions">
          <button class="approve" onclick="decide('{record['dedupe_key']}','approve')">Approve</button>
          <button class="deny" onclick="decide('{record['dedupe_key']}','deny')">Deny</button>
          <button onclick="decide('{record['dedupe_key']}','modify')">Request changes</button>
          <button onclick="decide('{record['dedupe_key']}','pause')">Pause build</button>
        </div>"""
        )
        rows.append(
            f"""
      <article class="card risk-{_esc(record['risk'])}">
        <header>
          <span class="badge">{_esc(badge)}</span>
          <h2>{_esc(payload.get('action', ''))}</h2>
        </header>
        <p class="why">{_esc(payload.get('why_blocked', ''))}</p>
        <dl>
          <dt>Build</dt><dd><code>{_esc(record['build_id'] or 'n/a')}</code></dd>
          <dt>Rule</dt><dd><code>{_esc(record['rule_name'])}</code></dd>
          <dt>Reversible</dt><dd>{'yes' if record['reversible'] else '<b>no</b>'}</dd>
          <dt>Repo / branch</dt>
          <dd><code>{_esc(payload.get('repo', 'n/a'))}</code> / <code>{_esc(payload.get('branch', 'n/a'))}</code></dd>
          <dt>Files</dt><dd><code>{_esc(files)}</code></dd>
        </dl>
        <details><summary>Requested action</summary><pre>{_esc(payload.get('detail', ''))}</pre></details>
        <details><summary>Evidence collected</summary><ul>{evidence_html}</ul></details>
        <p class="rec">{_esc(payload.get('recommendation', ''))}</p>
        {buttons}
      </article>"""
        )

    empty = "<p class='empty'>No pending approvals. The factory is running autonomously.</p>"
    content = "".join(rows) if rows else empty

    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ctswarm approvals</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --line:#ddd; --card:#fafafa; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#111; --fg:#eee; --muted:#999; --line:#333; --card:#1a1a1a; }}
  }}
  body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,sans-serif;
         margin:0; padding:2rem 1rem; }}
  main {{ max-width:52rem; margin:0 auto; }}
  h1 {{ font-size:1.3rem; margin:0 0 .25rem; }}
  .sub {{ color:var(--muted); margin:0 0 2rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-left-width:4px;
           border-radius:8px; padding:1rem 1.25rem; margin-bottom:1.5rem; }}
  .risk-critical {{ border-left-color:#c53030; }}
  .risk-high {{ border-left-color:#dd6b20; }}
  .risk-medium {{ border-left-color:#d69e2e; }}
  .risk-low {{ border-left-color:#718096; }}
  header {{ display:flex; align-items:center; gap:.75rem; flex-wrap:wrap; }}
  header h2 {{ font-size:1.05rem; margin:0; font-family:ui-monospace,monospace; }}
  .badge {{ font-size:.7rem; letter-spacing:.06em; font-weight:700;
            border:1px solid var(--line); border-radius:99px; padding:.15rem .6rem; }}
  .why {{ color:var(--fg); }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:.25rem 1rem; font-size:.9rem; margin:1rem 0; }}
  dt {{ color:var(--muted); }}
  dd {{ margin:0; }}
  pre {{ overflow-x:auto; background:var(--bg); padding:.75rem; border-radius:6px;
         border:1px solid var(--line); font-size:.85rem; }}
  details {{ margin:.5rem 0; }}
  summary {{ cursor:pointer; color:var(--muted); font-size:.9rem; }}
  .rec {{ font-style:italic; color:var(--muted); }}
  .actions {{ display:flex; gap:.5rem; flex-wrap:wrap; margin-top:1rem; }}
  button {{ font:inherit; padding:.45rem 1rem; border-radius:6px; cursor:pointer;
            border:1px solid var(--line); background:var(--bg); color:var(--fg); }}
  button.approve {{ background:#276749; border-color:#276749; color:#fff; }}
  button.deny {{ background:#9b2c2c; border-color:#9b2c2c; color:#fff; }}
  .empty {{ color:var(--muted); }}
  .note {{ margin-top:3rem; padding-top:1rem; border-top:1px solid var(--line);
           color:var(--muted); font-size:.85rem; }}
</style></head>
<body><main>
  <h1>ctswarm approvals</h1>
  <p class="sub">{len(pending)} pending. No response pauses the build safely; silence is never approval.</p>
  {content}
  <p class="note">This local surface always works, with or without Slack.
     Decisions recorded here are append-only and identical to Slack decisions.</p>
</main>
<script>
async function decide(key, decision) {{
  const note = decision === 'deny' || decision === 'modify'
    ? (prompt('Optional note for the build:') || '') : '';
  const res = await fetch(`/approvals/${{key}}/decide`, {{
    method:'POST', headers:{{'content-type':'application/json'}},
    body: JSON.stringify({{decision, decided_by:'local-ui', note}})
  }});
  if (!res.ok) {{ const e = await res.json(); alert('Failed: ' + (e.error || res.status)); return; }}
  location.reload();
}}
</script>
</body></html>"""
    )


def _esc(value: str) -> str:
    """Escape untrusted text for HTML.

    Card content originates from agent output, which is untrusted input. An agent
    that emits markup into a detail field must not be able to inject script into
    the approval surface that governs it.
    """
    import html

    return html.escape(str(value), quote=True)
