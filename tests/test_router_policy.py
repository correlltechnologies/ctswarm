"""Routing policy regressions found under real multi-turn local agents."""

from ctswarm.catalog import Tier
from ctswarm.ledger import Ledger
from ctswarm.platform_detect import Accelerator, HostProfile
from ctswarm.router.policy import BenchScore, Router, RoutingTable


def test_successful_bench_size_is_not_treated_as_context_ceiling(tmp_path) -> None:
    """A passed 20k probe must not reject a model advertising 128k+ context."""
    router = Router(
        host=HostProfile(
            os_name="Linux",
            arch="x86_64",
            accelerator=Accelerator.CUDA,
            accel_memory_gb=24.0,
            system_memory_gb=64.0,
            has_ollama=True,
        ),
        ledger=Ledger(tmp_path / "ledger.db"),
        table=RoutingTable(
            {
                "qwen3.5:4b": BenchScore(
                    model_ref="qwen3.5:4b",
                    backend="ollama",
                    tool_call_rate=1.0,
                    schema_rate=1.0,
                    long_context_rate=1.0,
                    instruction_rate=1.0,
                    cancel_clean=True,
                    p50_latency_ms=100.0,
                    tokens_per_s=18.0,
                    max_context_ok=20_000,
                    samples=8,
                )
            }
        ),
        local_only=True,
        backends={"ollama"},
    )

    decision = router.decide(
        tier=Tier.LOW,
        needs_tools=True,
        min_context=30_000,
        installed={"qwen3.5:4b"},
    )

    assert decision.primary is not None
    assert decision.primary.model_ref == "qwen3.5:4b"


def test_catalog_snapshot_matches_live_router_policy(tmp_path) -> None:
    router = Router(
        host=HostProfile(
            os_name="Linux",
            arch="x86_64",
            accelerator=Accelerator.CUDA,
            accel_memory_gb=24.0,
            system_memory_gb=64.0,
            has_ollama=True,
        ),
        ledger=Ledger(tmp_path / "catalog.db"),
        local_only=True,
        backends={"ollama", "openrouter"},
    )

    rows = router.catalog_snapshot(
        installed_by_backend={
            "ollama": {"qwen3.5:4b"},
            "openrouter": {"deepseek/deepseek-v4-pro"},
        },
        warm_by_backend={"ollama": {"qwen3.5:4b"}, "openrouter": set()},
    )

    local = next(row for row in rows if row["ref"] == "qwen3.5:4b")
    hosted = next(row for row in rows if row["ref"] == "deepseek/deepseek-v4-pro")
    missing = next(row for row in rows if row["ref"] == "qwen3.5:9b")

    assert local["installed"] is True
    assert local["warm"] is True
    assert local["routable_tiers"] == ["low"]
    assert hosted["installed"] is True
    assert hosted["routable"] is False
    assert set(hosted["exclusions"].values()) == {
        "hosted backend excluded by local-only execution policy"
    }
    assert missing["installed"] is False
    assert "not installed" in next(iter(missing["exclusions"].values()))
