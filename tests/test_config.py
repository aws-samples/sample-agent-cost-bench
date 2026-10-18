"""Task 3 — config loader + task discovery tests."""

from __future__ import annotations

import textwrap

import pytest

from agent_cost_bench.config import (
    discover_tasks,
    load_cli_compare_config,
    load_model_compare_config,
)
from agent_cost_bench.models import CompareMode, CostSource


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
    # Credit telemetry only appears in stream-json output on kiro-cli 2.32+.
    args = cfg.targets[0].cli_base_args
    assert args[args.index("--output-format") + 1] == "stream-json"
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


def _make_tb_source(root):
    """A minimal Terminal-Bench 2.x task tree usable as a task_sources path."""
    d = root / "tb" / "hello"
    (d / "tests").mkdir(parents=True)
    (d / "task.toml").write_text('version = "1.0"\n[environment]\ndocker_image = "ubuntu:22.04"\n')
    (d / "instruction.md").write_text("# Hello\n\nPrint hello.\n")
    (d / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")
    return str(root / "tb")


def test_task_sources_imported_and_discovered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    tb_dir = _make_tb_source(tmp_path)
    cfg_path = _write(
        tmp_path / "cli.yaml",
        f"""
        runners:
          - name: kiro
            cli_path: kiro
            model_id: claude-sonnet-4
        tasks_dir: {tasks_dir}
        workspace_base: {tmp_path / "ws"}
        task_sources:
          - type: terminal-bench
            path: {tb_dir}
            tasks: [hello]
        """,
    )
    cfg = load_cli_compare_config(cfg_path)
    assert len(cfg.task_sources) == 1
    assert cfg.task_sources[0].type == "terminal-bench"

    tasks = {t.id: t for t in discover_tasks(cfg)}
    # task_sources are exclusive: only the imported Terminal-Bench task runs;
    # the repo's own tasks_dir tasks (e.g. 't-vibe') are skipped.
    assert "terminal-bench-hello" in tasks
    assert "t-vibe" not in tasks
    imported = tasks["terminal-bench-hello"]
    assert imported.verify is not None
    assert imported.verify.parser == "reward-file"
    assert imported.verify.image == "ubuntu:22.04"


def test_task_sources_name_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    tb_dir = _make_tb_source(tmp_path)
    cfg_path = _write(
        tmp_path / "cli.yaml",
        f"""
        runners:
          - name: kiro
            cli_path: kiro
            model_id: claude-sonnet-4
        tasks_dir: {tasks_dir}
        workspace_base: {tmp_path / "ws"}
        task_sources:
          - type: terminal-bench
            path: {tb_dir}
            tasks: [hello]
        """,
    )
    cfg = load_cli_compare_config(cfg_path)
    ids = {t.id for t in discover_tasks(cfg)}
    assert "terminal-bench-hello" in ids


def _make_named_tb_task(root, name):
    d = root / "tb" / name
    (d / "tests").mkdir(parents=True)
    (d / "task.toml").write_text('version = "1.0"\n[environment]\ndocker_image = "ubuntu:22.04"\n')
    (d / "instruction.md").write_text(f"# {name}\n\nDo it.\n")
    (d / "tests" / "test.sh").write_text("echo 1 > /logs/verifier/reward.txt\n")


def test_terminal_bench_defaults_to_supported_allowlist(tmp_path, monkeypatch):
    """With no explicit tasks:, a terminal-bench source imports only the built-in
    harness-compatible allowlist — not every task in the source."""
    from agent_cost_bench.importers.terminal_bench import terminal_bench_supported_tasks

    monkeypatch.chdir(tmp_path)
    tasks_dir = _make_tasks(tmp_path)
    allow = terminal_bench_supported_tasks()
    # One allowlisted task + one that is NOT on the allowlist.
    _make_named_tb_task(tmp_path, allow[0])
    _make_named_tb_task(tmp_path, "some-unsupported-env-task")

    cfg_path = _write(
        tmp_path / "cli.yaml",
        f"""
        runners:
          - name: kiro
            cli_path: kiro
            model_id: claude-sonnet-4
        tasks_dir: {tasks_dir}
        workspace_base: {tmp_path / "ws"}
        task_sources:
          - type: terminal-bench
            path: {tmp_path / "tb"}
        """,
    )
    cfg = load_cli_compare_config(cfg_path)
    ids = {t.id for t in discover_tasks(cfg)}
    assert f"terminal-bench-{allow[0]}" in ids
    assert "terminal-bench-some-unsupported-env-task" not in ids


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
        ("cursor",        {},                                   CostSource.CURSOR_JSON),
        ("cursor-agent",  {},                                   CostSource.CURSOR_JSON),
        ("agy",           {},                                   CostSource.ANTIGRAVITY_JSON),
        ("antigravity",   {},                                   CostSource.ANTIGRAVITY_JSON),
        # antigravity wins over the generic per-token rule despite having rates
        ("agy",           {"usd_per_input_token": 0.000001,
                           "usd_per_output_token": 0.000004},  CostSource.ANTIGRAVITY_JSON),
        # devin wins over the generic per-token rule below despite having rates
        ("devin",         {"usd_per_input_token": 0.000005,
                           "usd_per_output_token": 0.000025},  CostSource.DEVIN_EXPORT),
        ("devin",         {},                                   CostSource.DEVIN_EXPORT),
        ("pi",            {},                                   CostSource.PI_JSON),
        ("/opt/homebrew/bin/pi", {},                            CostSource.PI_JSON),
        # pi reports USD itself; optional token rates must not demote it to TOKENS
        ("pi",            {"usd_per_input_token": 0.000002,
                           "usd_per_output_token": 0.00001},    CostSource.PI_JSON),
        ("qodercli",      {},                                   CostSource.QODER_JSON),
        ("qodercli-wake", {},                                   CostSource.QODER_JSON),
        ("/usr/local/bin/qoder", {},                            CostSource.QODER_JSON),
        # a longer name merely ENDING in "pi" is not the pi CLI
        ("copi",          {},                                   CostSource.NONE),
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
    right sources inferred for kiro-cli, claude, copilot, codex, and devin."""
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
          - name: devin
            cli_path: devin
            model_id: claude-opus-4-8
            pricing:
              usd_per_input_token: 0.000005
              usd_per_output_token: 0.000025
            cli_base_args: ["-p", "{{prompt}}", "--model", "{{model}}",
                            "--export", "devin-usage.json"]
          - name: pi
            cli_path: pi
            model_id: global.anthropic.claude-sonnet-5
            cli_base_args: ["-p", "--mode", "json", "--model", "{{model}}", "{{prompt}}"]
        tasks_dir: {tasks_dir}
        """,
    )
    cfg = load_cli_compare_config(cfg_path)
    by_name = {t.name: t for t in cfg.targets}
    assert by_name["kiro"].cost_source == CostSource.KIRO_CREDITS
    assert by_name["claude-code"].cost_source == CostSource.CLAUDE_JSON
    assert by_name["copilot"].cost_source == CostSource.COPILOT_JSON
    assert by_name["codex"].cost_source == CostSource.CODEX_JSON
    assert by_name["devin"].cost_source == CostSource.DEVIN_EXPORT
    assert by_name["pi"].cost_source == CostSource.PI_JSON
