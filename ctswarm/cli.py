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
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .backends import build_backends
from .bench.runner import bench_all, write_results
from .bench.suite import build_suite
from .catalog import Tier, build_catalog
from .ledger import Ledger
from .platform_detect import detect_host
from .router.policy import RoutingTable

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
    for entry in build_catalog(host):
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
    models: Optional[str] = typer.Option(
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
            catalog_refs = [
                entry.spec.ref
                for entry in build_catalog(host)
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
def usage(build_id: Optional[str] = typer.Option(None, help="Scope to one build.")) -> None:
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
