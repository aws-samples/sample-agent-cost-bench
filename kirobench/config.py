"""
Config loaders: two tailored YAML schemas desugar into one unified BenchConfig.

* ``load_cli_compare_config``   — schema with an optional ``comparison_label``
  and a ``runners:`` list (each with its own ``cost_source`` and ``model_id``).
  cli-compare is vibe-only.
* ``load_model_compare_config`` — schema with a simple ``models:`` list (bare
  ids or dicts), optional judge config, Kiro CLI templating, and per-task
  scoring/spec_workflow defaults.

Both expand ``${VAR}`` / ``${VAR:-default}`` placeholders and produce a
``BenchConfig`` whose ``targets`` are built by the desugaring helpers in
:mod:`kirobench.targets`.

``discover_tasks`` walks the tasks directory, applies mode/task-id filters, and
skips unparseable or mode-incompatible tasks gracefully.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from rich.console import Console

from .models import (
    BenchConfig,
    CompareMode,
    ScoringWeights,
    SpecWorkflow,
    TaskConfig,
    TaskMode,
)
from .targets import make_cli_target, make_kiro_target

console = Console()

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} / ${VAR:-default} placeholders."""
    if isinstance(value, str):

        def _sub(m: re.Match) -> str:
            var_name, default = m.group(1), m.group(2)
            return os.environ.get(var_name, default if default is not None else "")

        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _read_yaml(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return _expand_env(raw)


# Fields that map straight from YAML onto BenchConfig (shared by both schemas).
_PASSTHROUGH = {
    "effort",
    "concurrency",
    "max_concurrency",
    "tasks_dir",
    "task_ids",
    "timeout_minutes",
    "task_timeout_minutes",
    "repeats",
    "transient_retries",
    "functional_pass_threshold",
    "pass_threshold",
    "workspace_base",
    "output_dir",
    "report_title",
    "open_report",
    # LLM-as-judge (usable from both cli-compare and model-compare configs)
    "judge_model",
    "judge_cli_path",
    "judge_api_key",
    "judge_weight",
}


def _passthrough(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in _PASSTHROUGH if k in raw}


# ---------------------------------------------------------------------------
# cli-compare
# ---------------------------------------------------------------------------


def load_cli_compare_config(config_path: str | Path) -> BenchConfig:
    raw = _read_yaml(config_path)
    # `comparison_label` is a reporting headline only (each runner keeps its own
    # model_id, which may differ). Accept the legacy `shared_model` key too.
    comparison_label = (
        raw.get("comparison_label") or raw.get("shared_model") or "cross-CLI comparison"
    )

    runners = raw.get("runners") or []
    if not runners:
        raise ValueError("cli-compare config must define at least one runner")

    targets = [make_cli_target(r, comparison_label=comparison_label) for r in runners]

    kwargs: dict[str, Any] = dict(
        mode=CompareMode.CLI_COMPARE,
        targets=targets,
        comparison_label=comparison_label,
        **_passthrough(raw),
    )
    # cli-compare is vibe-only.
    kwargs.setdefault("report_title", "kirobench cli-compare")
    kwargs["modes"] = [TaskMode.VIBE]
    try:
        return BenchConfig(**kwargs)
    except ValidationError as e:
        raise ValueError(f"Invalid cli-compare config: {e}") from e


# ---------------------------------------------------------------------------
# model-compare
# ---------------------------------------------------------------------------


def load_model_compare_config(config_path: str | Path) -> BenchConfig:
    raw = _read_yaml(config_path)

    models = raw.get("models")
    if not models:
        raise ValueError("model-compare config must define at least one model")

    kiro_cli_path = raw.get("kiro_cli_path", "kiro")
    # Optional global pricing applied to all Kiro targets (usd_per_credit).
    pricing = raw.get("pricing") or {}
    usd_per_credit = pricing.get("usd_per_credit")
    # Optional override of the native spec-mode args (default ["--mode","spec"]).
    spec_mode_args = raw.get("spec_mode_args")
    # Optional kas-proxy metrics integration (Phase 2 of the OpenRouter path).
    use_kas_metrics = bool(raw.get("kas_proxy_metrics", False))
    kas_metrics_file = raw.get("kas_proxy_metrics_file")
    kas_metrics_timeout = float(raw.get("kas_proxy_metrics_timeout_seconds", 5.0))

    targets = [
        make_kiro_target(
            m,
            default_cli_path=kiro_cli_path,
            usd_per_credit=usd_per_credit,
            spec_mode_args=spec_mode_args,
            use_kas_proxy_metrics=use_kas_metrics,
            kas_metrics_file=kas_metrics_file,
            kas_metrics_timeout_seconds=kas_metrics_timeout,
        )
        for m in models
    ]

    # API key: explicit value, else env var, else CLI login session.
    api_key = raw.get("kiro_api_key") or os.environ.get("KIRO_API_KEY", "")

    modes_raw = raw.get("modes")
    modes = [TaskMode(m) for m in modes_raw] if modes_raw else None

    kwargs: dict[str, Any] = dict(
        mode=CompareMode.MODEL_COMPARE,
        targets=targets,
        kiro_api_key=api_key,
        kiro_cli_path=kiro_cli_path,
        vibe_agent=raw.get("vibe_agent"),
        spec_driver_agent=raw.get("spec_driver_agent"),
        spec_executor_agent=raw.get("spec_executor_agent"),
        modes=modes,
        spec_prompt_via_stdin=raw.get("spec_prompt_via_stdin", False),
        spec_use_pty=raw.get("spec_use_pty", True),
        kas_proxy_metrics=use_kas_metrics,
        kas_proxy_metrics_file=kas_metrics_file,
        kas_proxy_metrics_timeout_seconds=kas_metrics_timeout,
        vibe_use_pty=bool(raw.get("vibe_use_pty", False)),
        **_passthrough(raw),
    )
    if "judge_weight" in raw:
        kwargs["judge_weight"] = raw["judge_weight"]
    kwargs.setdefault("report_title", "kirobench model-compare")
    try:
        return BenchConfig(**kwargs)
    except ValidationError as e:
        raise ValueError(f"Invalid model-compare config: {e}") from e


def load_config(config_path: str | Path, mode: CompareMode) -> BenchConfig:
    """Dispatch to the appropriate loader for the given mode."""
    if mode == CompareMode.CLI_COMPARE:
        return load_cli_compare_config(config_path)
    return load_model_compare_config(config_path)


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------


def discover_tasks(config: BenchConfig) -> list[TaskConfig]:
    """
    Walk the tasks directory and load all task.yaml files, applying task-id and
    mode filters. cli-compare silently skips spec-driven tasks (it is vibe-only).
    """
    tasks_root = Path(config.tasks_dir).expanduser().resolve()
    if not tasks_root.exists():
        raise FileNotFoundError(f"Tasks directory not found: {tasks_root}")

    task_configs: list[TaskConfig] = []
    for task_yaml_path in sorted(tasks_root.rglob("task.yaml")):
        try:
            tc = _load_task_config(task_yaml_path)
        except Exception as e:
            console.print(f"[yellow]⚠ Skipping {task_yaml_path}: {e}[/yellow]")
            continue

        # cli-compare is vibe-only — skip spec tasks rather than mis-run them.
        if config.mode == CompareMode.CLI_COMPARE and tc.mode == TaskMode.SPEC_DRIVEN:
            console.print(
                f"[yellow]⚠ Skipping spec-driven task '{tc.id}' (cli-compare is vibe-only)[/yellow]"
            )
            continue

        if config.task_ids and tc.id not in config.task_ids:
            continue
        if config.modes and tc.mode not in config.modes:
            continue

        task_configs.append(tc)

    if not task_configs:
        raise ValueError(
            f"No tasks found in '{tasks_root}' matching the configured filters. "
            "Check tasks_dir, task_ids, and modes."
        )
    return task_configs


def _load_task_config(task_yaml_path: Path) -> TaskConfig:
    with open(task_yaml_path) as f:
        raw = yaml.safe_load(f) or {}

    mode_raw = raw.get("mode", "vibe")

    scoring_raw = raw.pop("scoring", {})
    scoring = ScoringWeights(**scoring_raw) if scoring_raw else _default_scoring(mode_raw)

    spec_workflow_raw = raw.pop("spec_workflow", "requirements-first")
    spec_workflow = SpecWorkflow(spec_workflow_raw)

    tc = TaskConfig(
        scoring=scoring,
        spec_workflow=spec_workflow,
        **{k: v for k, v in raw.items() if k in TaskConfig.model_fields},
    )
    tc.task_dir = task_yaml_path.parent
    return tc


def _default_scoring(mode: str | None) -> ScoringWeights:
    """Sensible default scoring weights based on task mode."""
    if mode == TaskMode.SPEC_DRIVEN.value:
        return ScoringWeights(
            functional_tests=0.50,
            spec_artifact_quality=0.25,
            task_completion_rate=0.15,
            steering_adherence=0.10,
        )
    return ScoringWeights(
        functional_tests=1.0,
        spec_artifact_quality=0.0,
        task_completion_rate=0.0,
        steering_adherence=0.0,
    )
