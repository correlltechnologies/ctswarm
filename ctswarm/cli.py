"""ctswarm command line.

Every command is safe to run repeatedly and reports what it found rather than
what it assumes. ``doctor`` in particular is the honest inventory: it draws a
hard line between capabilities that are wired up and verified, and capabilities
that are merely configured.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .backends import build_backends
from .bench.runner import bench_all, write_results
from .bench.suite import build_suite
from .catalog import build_catalog
from .ledger import Ledger
from .platform_detect import detect_host
from .router.policy import RoutingTable


def _load_dotenv(path: str = ".env") -> None:
    """Load .env into the environment without clobbering real env vars.

    Deliberately does not overwrite an already-set variable: an explicit
    `FOO=bar ctswarm ...` must win over the file, and a CI-injected secret must
    not be silently replaced by a stale local one.

    Written by hand rather than pulled in as a dependency because it needs to run
    before anything else and the parsing rules here are a dozen lines.
    """
    file = Path(path)
    if not file.exists():
        return
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

app = typer.Typer(
    add_completion=False,
    help="Governance, routing, and evidence layer around SWE-AF.",
    no_args_is_help=True,
)
console = Console()


def _ok(flag: bool) -> str:
    return "[green]yes[/green]" if flag else "[red]no[/red]"


@app.command()
def doctor() -> None:
    """Report what is wired up, what is missing, and how to fix each gap."""
    host = detect_host()

    console.print("\n[bold]Host[/bold]")
    host_table = Table(show_header=False, box=None, pad_edge=False)
    for key, value in host.to_dict().items():
        host_table.add_row(f"  {key}", str(value))
    console.print(host_table)

    console.print("\n[bold]Backends[/bold]")
    backends = build_backends(host)

    async def probe() -> dict:
        found = {}
        for name, backend in backends.items():
            found[name] = {
                "healthy": await backend.health(),
                "models": await backend.list_models(),
            }
            await backend.close()
        return found

    probed = asyncio.run(probe()) if backends else {}
    backend_table = Table("backend", "healthy", "models", box=None)
    for name, info in probed.items():
        backend_table.add_row(
            name, _ok(info["healthy"]), str(len(info["models"])) + " installed"
        )
    if not probed:
        backend_table.add_row("[dim]none detected[/dim]", "", "")
    console.print(backend_table)

    installed = {ref for info in probed.values() for ref in info["models"]}

    console.print("\n[bold]Model catalog[/bold]")
    catalog_table = Table("model", "placement", "tiers", "installed", "note", box=None)
    for entry in build_catalog(host, set(probed)):
        note = entry.spec.notes.split(".")[0] if entry.spec.notes else ""
        catalog_table.add_row(
            entry.spec.ref,
            entry.placement,
            ",".join(t.value for t in entry.spec.tiers) or "-",
            _ok(entry.spec.ref in installed),
            note[:60],
        )
    console.print(catalog_table)

    console.print("\n[bold]Routing table[/bold]")
    table = RoutingTable.load()
    if table.is_empty:
        console.print(
            "  [yellow]no measurements[/yellow]  run [bold]ctswarm bench[/bold] "
            "to qualify models before running a build"
        )
    else:
        score_table = Table(
            "model", "tool", "schema", "ctx", "instr", "tok/s", "eligible", box=None
        )
        for score in table.all():
            score_table.add_row(
                score.model_ref,
                f"{score.tool_call_rate:.0%}",
                f"{score.schema_rate:.0%}",
                f"{score.long_context_rate:.0%}",
                f"{score.instruction_rate:.0%}",
                f"{score.tokens_per_s:.0f}",
                _ok(score.eligible_for_agent_roles),
            )
        console.print(score_table)

    console.print("\n[bold]Credentials and runtimes[/bold]")
    creds = _credential_status()
    cred_table = Table("capability", "available", "how to enable", box=None)
    for name, (available, fix) in creds.items():
        cred_table.add_row(name, _ok(available), "" if available else fix)
    console.print(cred_table)
    console.print()


def _credential_status() -> dict[str, tuple[bool, str]]:
    """What the factory can currently authenticate as.

    Checked by looking for real artifacts, not by trusting environment variables
    that may name an expired token.
    """
    home = Path.home()
    return {
        "claude_code runtime": (
            bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
            or bool(os.environ.get("ANTHROPIC_API_KEY")),
            "run `claude setup-token` and put it in .env as CLAUDE_CODE_OAUTH_TOKEN",
        ),
        "codex runtime": (
            (home / ".codex" / "auth.json").exists()
            or bool(os.environ.get("OPENAI_API_KEY")),
            "run `codex login` on this host",
        ),
        "openrouter overflow": (
            bool(os.environ.get("OPENROUTER_API_KEY")),
            "add OPENROUTER_API_KEY to .env (get a key at openrouter.ai/keys)",
        ),
        "github PR creation": (
            bool(os.environ.get("GH_TOKEN")) or _gh_authed(),
            "run `gh auth login`, or set GH_TOKEN in .env",
        ),
        "slack approvals": (
            bool(os.environ.get("SLACK_BOT_TOKEN"))
            and bool(os.environ.get("SLACK_APPROVAL_CHANNEL")),
            "see docs/SLACK.md; local approval UI is used until this is set",
        ),
        "docker": (shutil.which("docker") is not None, "install Docker Engine"),
    }


def _gh_authed() -> bool:
    if not shutil.which("gh"):
        return False
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


@app.command()
def bench(
    models: str | None = typer.Option(
        None, help="Comma-separated model refs. Default: every installed candidate."
    ),
    backend: str = typer.Option("ollama", help="Backend to bench against."),
    context_chars: int = typer.Option(
        60_000, help="Haystack size for the long-context task."
    ),
) -> None:
    """Qualify models and write the routing table.

    Runs strictly one model at a time. Two models cannot be resident in a 12GB
    accelerator simultaneously, so concurrency would measure swap thrash instead
    of model quality.
    """
    host = detect_host()
    backends = build_backends(host)
    target = backends.get(backend)
    if target is None:
        console.print(f"[red]backend '{backend}' is not available on this host[/red]")
        raise typer.Exit(1)

    async def run() -> None:
        installed = set(await target.list_models())
        if models:
            refs = [m.strip() for m in models.split(",") if m.strip()]
        else:
            # Build the catalog for the backend being benched, not for whatever
            # this host has installed locally. Same bug as the router had:
            # host detection cannot see a hosted backend at all.
            catalog_refs = [
                entry.spec.ref
                for entry in build_catalog(host, {backend})
                if entry.usable and entry.spec.backend == backend
            ]
            refs = [ref for ref in catalog_refs if ref in installed]

        missing = [ref for ref in refs if ref not in installed]
        if missing:
            console.print(f"[yellow]not installed, skipping:[/yellow] {missing}")
        refs = [ref for ref in refs if ref in installed]

        if not refs:
            console.print("[red]no installed candidates to bench[/red]")
            raise typer.Exit(1)

        console.print(f"\n[bold]Benching {len(refs)} models sequentially[/bold]")
        console.print(f"[dim]{', '.join(refs)}[/dim]\n")

        suite = build_suite(long_context_chars=context_chars)

        def on_progress(model_ref: str, outcome) -> None:
            mark = "[green]PASS[/green]" if outcome.passed else "[red]FAIL[/red]"
            detail = "" if outcome.passed else f"  [dim]{outcome.detail[:70]}[/dim]"
            console.print(
                f"  {model_ref:<24} {outcome.task:<26} {mark} "
                f"{outcome.latency_ms:>6}ms{detail}"
            )

        def on_model_done(result) -> None:
            if result.error:
                # Distinct from a failing verdict. "Could not measure" and
                # "measured and found unfit" must never look the same.
                console.print(
                    f"  [bold]{result.model_ref}[/bold] -> [yellow]BLOCKED[/yellow]  "
                    f"{result.error}\n"
                )
                return
            score = result.to_score()
            verdict = (
                "[green]ELIGIBLE[/green]"
                if score.eligible_for_agent_roles
                else "[red]NOT ELIGIBLE[/red]"
            )
            console.print(
                f"  [bold]{result.model_ref}[/bold] -> {verdict}  "
                f"tools={score.tool_call_rate:.0%} schema={score.schema_rate:.0%} "
                f"ctx={score.long_context_rate:.0%} cancel={score.cancel_clean} "
                f"{score.tokens_per_s:.0f} tok/s\n"
            )

        results = await bench_all(
            target,
            refs,
            tasks=suite,
            on_progress=on_progress,
            on_model_done=on_model_done,
        )
        write_results(results)

        eligible = [r for r in results if r.to_score().eligible_for_agent_roles]
        console.print(
            f"[bold]{len(eligible)}/{len(results)} models eligible for agent roles[/bold]"
        )
        console.print("[dim]wrote bench/results/routing.json and detail.json[/dim]")

        for backend_obj in backends.values():
            await backend_obj.close()

    asyncio.run(run())


@app.command()
def route(
    role: str = typer.Option("coder", help="SWE-AF role name."),
    tools: bool = typer.Option(True, help="Whether the request needs tool calls."),
    context: int = typer.Option(8192, help="Estimated context tokens required."),
) -> None:
    """Explain what the router would pick for a role, and why."""
    from .router.policy import Router

    host = detect_host()
    ledger = Ledger()
    table = RoutingTable.load()
    router = Router(host=host, ledger=ledger, table=table)

    async def run() -> None:
        backends = build_backends(host)
        installed: set[str] = set()
        warm: set[str] = set()
        for backend_obj in backends.values():
            installed |= set(await backend_obj.list_models())
            loaded = getattr(backend_obj, "loaded_models", None)
            if loaded:
                warm |= set(await loaded())
            await backend_obj.close()

        decision = router.decide(
            role=role,
            needs_tools=tools,
            min_context=context,
            installed=installed,
            warm=warm,
        )
        console.print(json.dumps(decision.to_dict(), indent=2))

    asyncio.run(run())


@app.command()
def serve(
    host_addr: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8090, help="Port for the router."),
) -> None:
    """Run the router gateway.

    Binds to loopback by default. The router fronts local model endpoints that
    must never be reachable from the public internet.
    """
    import uvicorn

    uvicorn.run("ctswarm.router.server:app", host=host_addr, port=port, log_level="info")


@app.command()
def usage(build_id: str | None = typer.Option(None, help="Scope to one build.")) -> None:
    """Show model usage, cost, and the local-inference fraction."""
    ledger = Ledger()
    summary = ledger.usage_summary(build_id)
    table = Table("backend", "calls", "ok", "tokens", "cost usd", box=None)
    for name, row in summary["by_backend"].items():
        table.add_row(
            name,
            str(row["calls"]),
            str(row["ok"]),
            str(row["tokens"]),
            f"{row['cost']:.4f}",
        )
    console.print(table)
    console.print(
        f"\n  total calls      {summary['total_calls']}"
        f"\n  local fraction   {summary['local_fraction']:.1%}"
        f"\n  total cost       ${summary['total_cost_usd']:.4f}\n"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()


@app.command()
def verify(
    router: str = typer.Option("http://localhost:8090", help="Router base URL."),
    approvals: str = typer.Option("http://localhost:8091", help="Approval service URL."),
    sandbox: str = typer.Option("sandbox", help="Path to the sandbox target."),
    repo: str | None = typer.Option(None, help="Target repo to assert isolation on."),
    allow_skips: bool = typer.Option(
        False, help="Exit 0 even when probes were skipped."
    ),
    json_out: str | None = typer.Option(None, "--json", help="Write results to a file."),
) -> None:
    """Run the self-verification probe suite and print a scoreboard.

    Exits non-zero on any failure, and also on any skip unless --allow-skips is
    given. A suite that reports green because it quietly skipped its assertions
    manufactures confidence it did not earn.
    """
    from .verify.probes import Status, VerifyContext, run_all

    ctx = VerifyContext(
        router_url=router.rstrip("/"),
        approvals_url=approvals.rstrip("/"),
        sandbox_path=Path(sandbox),
        repo_path=Path(repo) if repo else None,
    )

    results = asyncio.run(run_all(ctx))

    console.print("\n[bold]ctswarm verification[/bold]\n")
    colors = {Status.PASS: "green", Status.FAIL: "red", Status.SKIP: "yellow"}
    for result in results:
        color = colors[result.status]
        console.print(
            f"  [{color}]{result.status.value:<4}[/{color}]  "
            f"[bold]{result.name:<18}[/bold]  {result.summary}"
        )

    failed = [r for r in results if r.status is Status.FAIL]
    skipped = [r for r in results if r.status is Status.SKIP]
    passed = [r for r in results if r.status is Status.PASS]

    console.print(
        f"\n  {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped\n"
    )

    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(
            json.dumps([r.to_dict() for r in results], indent=2), encoding="utf-8"
        )
        console.print(f"[dim]wrote {json_out}[/dim]\n")

    if failed:
        raise typer.Exit(1)
    if skipped and not allow_skips:
        console.print(
            "[yellow]Skipped probes are not passes. Re-run with --allow-skips to "
            "accept, or resolve the preconditions above.[/yellow]\n"
        )
        raise typer.Exit(2)


@app.command()
def capacity() -> None:
    """Show remaining headroom per runtime and which one would be chosen."""
    from .capacity import CapacityManager

    manager = CapacityManager()
    table = Table("runtime", "available", "remaining", "spent", "why", box=None)
    for name, info in manager.report().items():
        table.add_row(
            name,
            _ok(info["available"]),
            f"{info['fraction_remaining']:.0%}",
            f"${info['spent_usd']:.2f}",
            info["reason"],
        )
    console.print("\n[bold]Runtime capacity[/bold]")
    console.print(table)

    routine, why_routine = manager.select()
    strong, why_strong = manager.select(require_strong=True)
    console.print(
        f"\n  routine build   [bold]{routine.value}[/bold]  [dim]{why_routine}[/dim]"
        f"\n  planning/verify [bold]{strong.value}[/bold]  [dim]{why_strong}[/dim]\n"
    )
    console.print(
        "[dim]Subscription headroom is reconstructed from observed per-call usage,\n"
        "not polled: neither the claude nor codex CLI exposes a quota endpoint.\n"
        "An actual rate-limit response overrides the estimate.[/dim]\n"
    )


@app.command()
def committee(
    question: str = typer.Argument(..., help="The judgement call to put to a vote."),
    context_file: str | None = typer.Option(
        None, "--file", help="File whose contents are the evidence."
    ),
    rule: str = typer.Option(
        "majority", help="majority | unanimous | any_reject_blocks"
    ),
    min_families: int = typer.Option(
        2, help="Distinct model families required for a valid quorum."
    ),
) -> None:
    """Put a question to an independent multi-model committee.

    Members are drawn one per model family from bench-eligible models, because
    three models from the same family are one opinion sampled three times.
    """
    from .backends import build_backends
    from .committee import Rule, convene, eligible_members

    context = Path(context_file).read_text(encoding="utf-8") if context_file else ""
    table = RoutingTable.load()
    members = eligible_members(table)

    if len(members) < min_families:
        console.print(
            f"[red]only {len(members)} eligible model family/families "
            f"({members}); {min_families} required[/red]\n"
            "[yellow]Run `ctswarm bench`, or add a second backend "
            "(OPENROUTER_API_KEY) for genuine independence.[/yellow]"
        )
        raise typer.Exit(2)

    host = detect_host()
    backends = build_backends(host)
    backend = backends.get("ollama") or next(iter(backends.values()))

    async def run() -> None:
        result = await convene(
            question=question,
            context=context,
            backend=backend,
            members=members,
            rule=Rule(rule),
            min_families=min_families,
        )
        verdict = (
            "[green]APPROVED[/green]" if result.approved else "[red]BLOCKED[/red]"
        )
        console.print(f"\n{verdict}  [dim]{result.reason}[/dim]\n")
        for vote in result.votes:
            colour = {"approve": "green", "reject": "red", "abstain": "yellow"}[
                vote.verdict.value
            ]
            console.print(
                f"  [{colour}]{vote.verdict.value:<8}[/{colour}] "
                f"{vote.model_ref:<18} [dim]({vote.family})[/dim] "
                f"conf={vote.confidence:.2f}"
            )
            if vote.reasoning:
                console.print(f"           [dim]{vote.reasoning[:150]}[/dim]")
            for finding in vote.findings[:3]:
                console.print(f"           [yellow]![/yellow] {finding[:120]}")
        if result.needs_human:
            console.print("\n[yellow]Escalated: requires a human decision.[/yellow]")
        console.print()
        for backend_obj in backends.values():
            await backend_obj.close()

    asyncio.run(run())


@app.command()
def build(
    goal: str = typer.Argument(..., help="What you want built, in plain language."),
    repo: str = typer.Option(..., help="Target repository URL."),
    watch: bool = typer.Option(True, help="Stay attached and stream status."),
    status_interval: int = typer.Option(
        900, help="Seconds between status posts when nothing changes."
    ),
    max_hours: float = typer.Option(12.0, help="Wall-clock ceiling for the build."),
    gate_repo: str | None = typer.Option(
        None, "--gate-repo", help="Local checkout to run scanners and the committee on."
    ),
) -> None:
    """Queue a build on the always-on scheduler and optionally watch it.

    The scheduler owns runtime selection, concurrency, durable state, Slack
    status, and restart recovery. Refusing to bypass it is deliberate: a direct
    AgentField submission would evade the queue and shared-resource limit.
    """
    from .capacity import Runtime
    from .orchestrator import BuildRecord, BuildState, Orchestrator

    async def run() -> None:
        scheduler_url = _scheduler_url()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{scheduler_url}/builds",
                    json={
                        "goal": goal,
                        "repo_url": repo,
                        "require_strong_planning": True,
                        "max_ci_fix_cycles": 2,
                        "max_hours": max_hours,
                    },
                )
            response.raise_for_status()
            snapshot = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            console.print(
                f"[red]scheduler unavailable at {scheduler_url}: {exc}[/red]\n"
                "[dim]Start it with ./stack.sh up; builds do not bypass the "
                "durable concurrency queue.[/dim]"
            )
            raise typer.Exit(1) from exc

        build_id = snapshot["build_id"]
        console.print(
            f"\n[bold]{build_id}[/bold] queued"
            f"\n  repo     {repo}"
            f"\n  control  ctswarm pause/resume/stop {build_id}\n"
        )
        if not watch:
            return

        terminal = {state.value for state in BuildState if state.terminal}
        deadline = time.time() + max_hours * 3600 + 60
        last_state = ""
        last_detail = ""
        while True:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(
                        f"{scheduler_url}/builds/{build_id}"
                    )
                response.raise_for_status()
                snapshot = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                console.print(f"[yellow]status unavailable; retrying: {exc}[/yellow]")
                await asyncio.sleep(10)
                continue

            state = str(snapshot.get("state") or "queued")
            detail = str(snapshot.get("phase_detail") or "")
            if state != last_state or detail != last_detail:
                console.print(
                    f"  [dim]{time.strftime('%H:%M:%S')}[/dim] "
                    f"[bold]{state:<10}[/bold] "
                    f"{detail[:70] or goal[:70]}"
                )
                last_state, last_detail = state, detail
            if state in terminal:
                break
            if time.time() > deadline:
                console.print("[red]local watch deadline exceeded[/red]")
                raise typer.Exit(1)
            await asyncio.sleep(min(30, max(2, status_interval)))

        state = str(snapshot.get("state") or "")
        if state == BuildState.COMPLETE.value:
            target = gate_repo
            if target:
                console.print("\n[bold]Running gates on the integrated branch[/bold]")
                record = BuildRecord(
                    build_id=build_id,
                    goal=goal,
                    repo_url=repo,
                    runtime=Runtime(snapshot.get("runtime", "open_code")),
                    state=BuildState.COMPLETE,
                    execution_id=str(snapshot.get("execution_id") or ""),
                    phase_detail=str(snapshot.get("phase_detail") or ""),
                    pr_url=str(snapshot.get("pr_url") or ""),
                )
                orchestrator = Orchestrator()
                gates = await orchestrator.run_gates(record, target)
                scanners = gates.get("scanners", {})
                console.print(
                    f"  scanners: {'passed' if scanners.get('passed') else 'FAILED'}"
                )
                for name in scanners.get("failed", []):
                    console.print(f"    [red]failed[/red] {name}")
                for name in scanners.get("unavailable", []):
                    console.print(f"    [yellow]could not run[/yellow] {name}")
                committee = gates.get("committee", {})
                if committee.get("skipped"):
                    console.print(f"  committee: [yellow]skipped[/yellow] {committee['skipped']}")
                else:
                    console.print(
                        f"  committee: {'approved' if committee.get('approved') else 'BLOCKED'}"
                    )
                state = record.state.value
                snapshot["error"] = record.error
            else:
                console.print(
                    "\n[yellow]--gate-repo not given, so scanners and the committee "
                    "did not run. The build is NOT verified.[/yellow]"
                )

        console.print(f"\n[bold]final state: {state}[/bold]")
        if snapshot.get("pr_url"):
            console.print(f"  {snapshot['pr_url']}")
        if snapshot.get("error"):
            console.print(f"  [red]{snapshot['error']}[/red]")
        if state != BuildState.COMPLETE.value:
            raise typer.Exit(1)

    asyncio.run(run())


def _scheduler_url() -> str:
    return os.environ.get("CTSWARM_SCHEDULER_URL", "http://localhost:8092").rstrip("/")


def _build_control(build_id: str, action: str) -> None:
    try:
        response = httpx.post(
            f"{_scheduler_url()}/builds/{build_id}/{action}",
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]could not {action} {build_id}: {exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def pause(build_id: str) -> None:
    """Pause a build. No new work starts; in-flight calls finish."""
    _build_control(build_id, "pause")
    console.print(f"pause requested for {build_id}")


@app.command()
def resume(build_id: str) -> None:
    """Resume a paused build."""
    _build_control(build_id, "resume")
    console.print(f"resume requested for {build_id}")


@app.command()
def stop(build_id: str) -> None:
    """Stop a build. Work so far stays on its branch."""
    _build_control(build_id, "stop")
    console.print(f"stop requested for {build_id}")


@app.command()
def status(build_id: str | None = typer.Argument(None)) -> None:
    """Show build status and what the team is working on."""
    try:
        if build_id is None:
            response = httpx.get(f"{_scheduler_url()}/builds", timeout=15.0)
        else:
            response = httpx.get(
                f"{_scheduler_url()}/builds/{build_id}", timeout=15.0
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        console.print(f"[red]scheduler status unavailable: {exc}[/red]")
        raise typer.Exit(1) from exc

    if build_id is None:
        builds = payload.get("builds") or []
        if not builds:
            console.print("no builds recorded yet")
            return
        table = Table("build", "state", "goal", box=None)
        for snapshot in builds[:15]:
            table.add_row(
                snapshot.get("build_id", ""),
                snapshot.get("state", ""),
                (snapshot.get("goal") or "")[:60],
            )
        console.print(table)
        return

    console.print(
        f"\n[bold]{build_id}[/bold]  {payload.get('state', '')}"
        f"\n  goal        {str(payload.get('goal') or '')[:100]}"
        f"\n  runtime     {payload.get('runtime', 'pending selection')}"
        f"\n  execution   {payload.get('execution_id', '')}"
        f"\n  detail      {str(payload.get('phase_detail') or '')[:100]}"
        f"\n  PR          {payload.get('pr_url', '')}"
        f"\n  error       {payload.get('error', '')}\n"
    )
