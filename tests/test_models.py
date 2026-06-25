"""Task 1 — unified data model + desugaring tests."""

from __future__ import annotations

import pytest

from agent_cost_bench.models import (
    BenchConfig,
    CompareMode,
    CostSource,
    Target,
    TaskMode,
)
from agent_cost_bench.targets import make_cli_target, make_kiro_target, validate_targets


def test_model_compare_entry_desugars_to_kiro_target():
    # A bare model id should desugar to a Kiro target.
    t = make_kiro_target("claude-sonnet-4", usd_per_credit=0.04)
    assert t.cost_source == CostSource.KIRO_CREDITS
    assert t.capabilities.supports_spec is True
    assert t.capabilities.supports_agents is True
    assert t.model_id == "claude-sonnet-4"
    assert t.cli_path == "kiro"
    assert t.cli_model_flag == "--model={model}"
    assert "chat" in t.cli_base_args
    assert t.pricing.usd_per_credit == 0.04


def test_model_compare_dict_entry_overrides():
    t = make_kiro_target(
        {"id": "haiku", "display_name": "Haiku", "cli_path": "/opt/kiro", "enabled": False}
    )
    assert t.display_name == "Haiku"
    assert t.label == "Haiku"
    assert t.cli_path == "/opt/kiro"
    assert t.enabled is False


def test_kiro_target_native_spec_mode_default_and_override():
    # Default: Kiro targets run spec natively; --mode spec requires --v3.
    t = make_kiro_target("sonnet")
    assert t.spec_mode_args == ["--v3", "--mode", "spec"]
    # Loader-level override.
    t2 = make_kiro_target("sonnet", spec_mode_args=["--v3", "--mode", "spec", "--extra"])
    assert t2.spec_mode_args == ["--v3", "--mode", "spec", "--extra"]
    # Per-model override wins.
    t3 = make_kiro_target({"id": "sonnet", "spec_mode_args": []})
    assert t3.spec_mode_args == []


def test_cli_target_spec_mode_args_default_empty():
    t = make_cli_target({"name": "kiro", "cli_path": "kiro", "model_id": "m"})
    assert t.spec_mode_args == []


def test_cli_compare_runner_keeps_cost_source_and_model_id():
    t = make_cli_target(
        {
            "name": "claude-code",
            "cli_path": "claude",
            "model_id": "claude-sonnet-4-5",
            "cost_source": "claude_json",
            "cli_base_args": ["-p", "{prompt}", "--model", "{model}", "--output-format", "json"],
        },
        comparison_label="claude-sonnet-4.x across CLIs",
    )
    assert t.cost_source == CostSource.CLAUDE_JSON
    assert t.model_id == "claude-sonnet-4-5"
    assert t.comparison_label == "claude-sonnet-4.x across CLIs"
    # cli-compare runners don't claim spec support.
    assert t.capabilities.supports_spec is False


def test_cli_compare_runners_can_use_different_models():
    a = make_cli_target({"name": "kiro", "cli_path": "kiro", "model_id": "sonnet"})
    b = make_cli_target({"name": "copilot", "cli_path": "copilot", "model_id": "gpt-5"})
    assert a.model_id != b.model_id


def test_label_falls_back_to_name():
    t = Target(name="kiro", cli_path="kiro", model_id="sonnet")
    assert t.label == "kiro"
    t2 = Target(name="kiro", display_name="Kiro CLI", cli_path="kiro", model_id="sonnet")
    assert t2.label == "Kiro CLI"


def test_validate_targets_rejects_empty():
    with pytest.raises(ValueError):
        validate_targets([])


def test_validate_targets_rejects_all_disabled():
    disabled = Target(name="x", cli_path="x", model_id="m", enabled=False)
    with pytest.raises(ValueError):
        validate_targets([disabled])


def test_benchconfig_rejects_empty_targets():
    with pytest.raises(ValueError):
        BenchConfig(mode=CompareMode.CLI_COMPARE, targets=[])


def test_benchconfig_rejects_all_disabled_targets():
    with pytest.raises(ValueError):
        BenchConfig(
            mode=CompareMode.CLI_COMPARE,
            targets=[Target(name="x", cli_path="x", model_id="m", enabled=False)],
        )


def test_cli_compare_rejects_spec_mode_filter():
    with pytest.raises(ValueError):
        BenchConfig(
            mode=CompareMode.CLI_COMPARE,
            targets=[make_cli_target({"name": "kiro", "cli_path": "kiro", "model_id": "m"})],
            modes=[TaskMode.SPEC_DRIVEN],
        )


def test_effective_workers_per_target_default():
    targets = [
        make_cli_target({"name": "a", "cli_path": "a", "model_id": "m"}),
        make_cli_target({"name": "b", "cli_path": "b", "model_id": "m"}),
        make_cli_target({"name": "c", "cli_path": "c", "model_id": "m", "enabled": False}),
    ]
    cfg = BenchConfig(mode=CompareMode.CLI_COMPARE, targets=targets)
    # default concurrency="per_target" -> ENABLED targets (2), task count irrelevant
    assert cfg.concurrency == "per_target"
    assert cfg.effective_workers(n_tasks=5) == 2


def test_effective_workers_full_strategy():
    targets = [
        make_cli_target({"name": "a", "cli_path": "a", "model_id": "m"}),
        make_cli_target({"name": "b", "cli_path": "b", "model_id": "m"}),
    ]
    cfg = BenchConfig(mode=CompareMode.CLI_COMPARE, targets=targets, concurrency="full")
    # full -> targets × tasks = 2 × 5 = 10
    assert cfg.effective_workers(n_tasks=5) == 10


def test_effective_workers_full_with_max_concurrency_cap():
    targets = [
        make_cli_target({"name": "a", "cli_path": "a", "model_id": "m"}),
        make_cli_target({"name": "b", "cli_path": "b", "model_id": "m"}),
    ]
    cfg = BenchConfig(
        mode=CompareMode.CLI_COMPARE, targets=targets,
        concurrency="full", max_concurrency=6,
    )
    # full would be 2 × 10 = 20, capped at 6
    assert cfg.effective_workers(n_tasks=10) == 6


def test_effective_workers_explicit_int():
    cfg = BenchConfig(
        mode=CompareMode.CLI_COMPARE,
        targets=[make_cli_target({"name": "a", "cli_path": "a", "model_id": "m"})],
        concurrency=8,
    )
    assert cfg.effective_workers(n_tasks=3) == 8


def test_effective_workers_legacy_parallel_workers_override():
    cfg = BenchConfig(
        mode=CompareMode.CLI_COMPARE,
        targets=[make_cli_target({"name": "a", "cli_path": "a", "model_id": "m"})],
        parallel_workers=5,
    )
    assert cfg.effective_workers() == 5


def test_phase_result_timed_out_marker():
    from agent_cost_bench.models import PhaseResult

    t = PhaseResult(phase="spec", success=False, duration_seconds=1200.0,
                    error="claude-sonnet-4.6 timed out after 1200s on 'x' (phase: spec)")
    assert t.timed_out is True
    ok = PhaseResult(phase="spec", success=True, duration_seconds=1.0)
    assert ok.timed_out is False
    other = PhaseResult(phase="spec", success=False, duration_seconds=1.0, error="CLI exited with code 2")
    assert other.timed_out is False
