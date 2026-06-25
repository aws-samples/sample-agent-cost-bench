"""Task 3 — config loader + task discovery tests."""

from __future__ import annotations

import textwrap

import pytest

from agent_cost_bench.config import (
    discover_tasks,
    load_cli_compare_config,
    load_model_compare_config,
)
from agent_cost_bench.models import CompareMode, CostSource, TaskMode


def _write(p, text):
    p.write_text(textwrap.dedent(text))
    return str(p)


def _make_tasks(root):
    vibe = root / "tasks" / "vibe" / "t-vibe"
    vibe.mkdir(parents=True)
    (vibe / "task.yaml").write_text("id: t-vibe\nmode: vibe\ndescription: v\nprompt: do it\n")
    (vibe / "verify.sh").write_text("#!/bin/bash\nexit 0\n")
    spec = root / "tasks" / "spec" / "t-spec"
    (spec / "seed").mkdir(parents=True)
    (spec / "task.yaml").write_text("id: t-spec\nmode: spec-driven\ndescription: s\n")
    (spec / "seed" / "requirements.md").write_text("# r\n")
    (spec / "verify.sh").write_text("#!/bin/bash\nexit 0\n")
    return str(root / "tasks")


def test_cli_compare_config_loads_and_keeps_per_runner_cost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    cfg_path = _write(
        tmp_path / "cli.yaml",
        f"""
        comparison_label: claude-sonnet-4.x across CLIs
        runners:
          - name: kiro
            cli_path: kiro
            model_id: claude-sonnet-4
            cost_source: kiro_credits
            pricing: {{usd_per_credit: 0.04}}
          - name: claude-code
            cli_path: claude
            model_id: claude-sonnet-4-5
            cost_source: claude_json
        tasks_dir: {tasks_dir}
        """,
    )
    cfg = load_cli_compare_config(cfg_path)
    assert cfg.mode == CompareMode.CLI_COMPARE
    assert cfg.comparison_label == "claude-sonnet-4.x across CLIs"
    by_name = {t.name: t for t in cfg.targets}
    assert by_name["kiro"].cost_source == CostSource.KIRO_CREDITS
    assert by_name["claude-code"].cost_source == CostSource.CLAUDE_JSON
    assert by_name["kiro"].model_id != by_name["claude-code"].model_id
    # cli-compare is vibe-only -> spec task skipped.
    tasks = discover_tasks(cfg)
    ids = {t.id for t in tasks}
    assert "t-vibe" in ids
    assert "t-spec" not in ids


def test_model_compare_config_gets_kiro_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    cfg_path = _write(
        tmp_path / "model.yaml",
        f"""
        kiro_cli_path: kiro
        models:
          - claude-sonnet-4
          - id: claude-haiku-4-5
            display_name: Haiku
        pricing: {{usd_per_credit: 0.04}}
        tasks_dir: {tasks_dir}
        """,
    )
    cfg = load_model_compare_config(cfg_path)
    assert cfg.mode == CompareMode.MODEL_COMPARE
    assert all(t.cost_source == CostSource.KIRO_CREDITS for t in cfg.targets)
    assert all(t.capabilities.supports_spec for t in cfg.targets)
    assert cfg.targets[0].pricing.usd_per_credit == 0.04
    # model-compare runs both vibe and spec tasks.
    ids = {t.id for t in discover_tasks(cfg)}
    assert ids == {"t-vibe", "t-spec"}


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-123")
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    cfg_path = _write(
        tmp_path / "model.yaml",
        f"""
        kiro_api_key: ${{MY_KEY}}
        models: [claude-sonnet-4]
        tasks_dir: {tasks_dir}
        """,
    )
    cfg = load_model_compare_config(cfg_path)
    assert cfg.kiro_api_key == "secret-123"


def test_task_id_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    cfg_path = _write(
        tmp_path / "model.yaml",
        f"""
        models: [claude-sonnet-4]
        tasks_dir: {tasks_dir}
        task_ids: [t-spec]
        """,
    )
    cfg = load_model_compare_config(cfg_path)
    ids = {t.id for t in discover_tasks(cfg)}
    assert ids == {"t-spec"}


def test_effort_passthrough_both_schemas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    cli_cfg = _write(
        tmp_path / "cli.yaml",
        f"""
        effort: low
        runners:
          - {{name: kiro, cli_path: kiro, model_id: s,
              cli_base_args: [chat, "--model={{model}}", "--effort={{effort}}"]}}
        tasks_dir: {tasks_dir}
        """,
    )
    assert load_cli_compare_config(cli_cfg).effort == "low"

    model_cfg = _write(
        tmp_path / "model.yaml",
        f"""
        models: [claude-sonnet-4]
        effort: medium
        tasks_dir: {tasks_dir}
        """,
    )
    assert load_model_compare_config(model_cfg).effort == "medium"

    # Default applies when omitted.
    default_cfg = _write(
        tmp_path / "d.yaml",
        f"""
        models: [claude-sonnet-4]
        tasks_dir: {tasks_dir}
        """,
    )
    assert load_model_compare_config(default_cfg).effort == "high"


def test_missing_models_raises(tmp_path):
    cfg_path = _write(tmp_path / "bad.yaml", "kiro_cli_path: kiro\n")
    with pytest.raises(ValueError):
        load_model_compare_config(cfg_path)


def test_comparison_label_default_and_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    # No label provided -> applicable default.
    no_label = _write(
        tmp_path / "a.yaml",
        f"""
        runners:
          - {{name: kiro, cli_path: kiro, model_id: s}}
        tasks_dir: {tasks_dir}
        """,
    )
    assert load_cli_compare_config(no_label).comparison_label == "cross-CLI comparison"

    # Legacy `shared_model` key still honoured.
    legacy = _write(
        tmp_path / "b.yaml",
        f"""
        shared_model: legacy-label
        runners:
          - {{name: kiro, cli_path: kiro, model_id: s}}
        tasks_dir: {tasks_dir}
        """,
    )
    assert load_cli_compare_config(legacy).comparison_label == "legacy-label"


# ---------------------------------------------------------------------------
# cost_source auto-inference from binary name
# ---------------------------------------------------------------------------


def test_cost_source_inferred_from_binary_name(tmp_path, monkeypatch):
    """cost_source is optional in YAML — inferred from cli_path basename."""
    from agent_cost_bench.targets import _infer_cost_source
    from agent_cost_bench.models import Pricing

    # Binary name → expected cost_source
    cases = [
        ("kiro",          {},                                   CostSource.KIRO_CREDITS),
        ("kiro-cli",      {},                                   CostSource.KIRO_CREDITS),
        ("/usr/bin/kiro", {},                                   CostSource.KIRO_CREDITS),
        ("claude",        {},                                   CostSource.CLAUDE_JSON),
        ("/opt/claude",   {},                                   CostSource.CLAUDE_JSON),
        ("copilot",       {},                                   CostSource.COPILOT_JSON),
        ("codex",         {"usd_per_input_token": 0.000001,
                           "usd_per_output_token": 0.000004},  CostSource.CODEX_JSON),
        ("codex",         {},                                   CostSource.CODEX_JSON),
        ("my-cli",        {"usd_per_input_token": 0.000001,
                           "usd_per_output_token": 0.000004},  CostSource.TOKENS),
        ("my-cli",        {"usd_per_premium_request": 0.04},   CostSource.PREMIUM_REQUEST),
        ("unknown-tool",  {},                                   CostSource.NONE),
    ]
    for cli_path, pricing, expected in cases:
        result = _infer_cost_source(cli_path, pricing)
        assert result == expected, f"{cli_path!r} + {pricing} → expected {expected}, got {result}"


def test_explicit_cost_source_wins_over_inference(tmp_path, monkeypatch):
    """An explicit cost_source in YAML always overrides auto-inference."""
    from agent_cost_bench.targets import make_cli_target

    # 'claude' binary but forced to kiro_credits — contrived but proves override works.
    t = make_cli_target({
        "name": "custom",
        "cli_path": "claude",
        "model_id": "some-model",
        "cost_source": "kiro_credits",
        "pricing": {"usd_per_credit": 0.04},
    })
    assert t.cost_source == CostSource.KIRO_CREDITS


def test_cli_compare_config_no_cost_source_in_yaml(tmp_path, monkeypatch):
    """End-to-end: a cli-compare config with no cost_source fields gets the
    right sources inferred for kiro-cli, claude, copilot, and codex."""
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    cfg_path = _write(
        tmp_path / "infer.yaml",
        f"""
        runners:
          - name: kiro
            cli_path: kiro-cli
            model_id: claude-sonnet-4.6
            pricing: {{usd_per_credit: 0.04}}
            cli_base_args: [chat, "--model={{model}}"]
          - name: claude-code
            cli_path: claude
            model_id: claude-sonnet-4-6
            cli_base_args: ["-p", "{{prompt}}", "--output-format", "json", "--model", "{{model}}"]
          - name: copilot
            cli_path: copilot
            model_id: claude-sonnet-4.6
            pricing: {{usd_per_premium_request: 0.04}}
            cli_base_args: ["-p", "{{prompt}}", "--model", "{{model}}", "--output-format", "json"]
          - name: codex
            cli_path: codex
            model_id: gpt-5.5
            pricing:
              usd_per_input_token: 0.000005
              usd_per_output_token: 0.000030
            cli_base_args: ["exec", "--json", "--ephemeral", "-m", "{{model}}", "{{prompt}}"]
        tasks_dir: {tasks_dir}
        """,
    )
    cfg = load_cli_compare_config(cfg_path)
    by_name = {t.name: t for t in cfg.targets}
    assert by_name["kiro"].cost_source == CostSource.KIRO_CREDITS
    assert by_name["claude-code"].cost_source == CostSource.CLAUDE_JSON
    assert by_name["copilot"].cost_source == CostSource.COPILOT_JSON
    assert by_name["codex"].cost_source == CostSource.CODEX_JSON
