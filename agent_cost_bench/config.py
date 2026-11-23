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
:mod:`agent_cost_bench.targets`.

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
    RepoSpec,
    ScoringWeights,
    SpecWorkflow,
    TaskConfig,
    TaskMode,
    TaskSourceSpec,
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
    "concurrency_per_target",
    "image_build_workers",
    "tasks_dir",
    "task_ids",
    "timeout_minutes",
    "task_timeout_minutes",
    "repeats",
    "transient_retries",
    "functional_pass_threshold",
    "pass_threshold",
    "workspace_base",
    "devin_permissions_file",
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


def _parse_task_sources(raw: dict[str, Any]) -> list[TaskSourceSpec]:
    """Parse the optional top-level ``task_sources:`` list into typed specs."""
    entries = raw.get("task_sources") or []
    if not isinstance(entries, list):
        raise ValueError("task_sources must be a list")
    specs: list[TaskSourceSpec] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"task_sources[{i}] must be a mapping with 'type' and 'path'")
        try:
            specs.append(TaskSourceSpec(**entry))
        except ValidationError as e:
            raise ValueError(f"Invalid task_sources[{i}]: {e}") from e
    return specs


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
        task_sources=_parse_task_sources(raw),
        **_passthrough(raw),
    )
    # cli-compare is vibe-only.
    kwargs.setdefault("report_title", "agent_cost_bench cli-compare")
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
        task_sources=_parse_task_sources(raw),
        **_passthrough(raw),
    )
    if "judge_weight" in raw:
        kwargs["judge_weight"] = raw["judge_weight"]
    kwargs.setdefault("report_title", "agent_cost_bench model-compare")
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


def _resolve_task_source_path(src: TaskSourceSpec, workspace_base: Path) -> str:
    """Return a local directory to import a task source from.

    * Git URL  → clone (cached under ``workspace_base/.repo_cache``) and return
      the local checkout, honoring ``subdir`` so a monorepo's ``tasks/`` folder
      imports cleanly. The clone is shared/cached across runs.
    * Local path → return it unchanged (``import_task_source`` expands/validates).
    """
    if not src.is_git_source:
        return src.path

    # Imported lazily to avoid pulling the sandbox (and its deps) at import time.
    from .sandbox import clone_repo

    repo = src.as_repo_spec()
    console.print(f"[dim]Cloning task source {repo.url} (ref={repo.ref})…[/dim]")
    local_root = clone_repo(repo, workspace_base)
    return str(local_root)


def materialize_task_sources(config: BenchConfig) -> Path | None:
    """Convert every enabled ``task_sources`` entry into native fixtures under a
    staging root, and return that root (or None when no sources are configured).

    The staging root lives under ``<workspace_base>/.imported-tasks`` so it is
    kept separate from the repo's own ``tasks/`` tree. Import failures for one
    source are reported and skipped rather than aborting the whole run.
    """
    sources = [s for s in getattr(config, "task_sources", []) if s.enabled]
    if not sources:
        return None

    from .importers import import_task_source
    from .importers.base import ImportError_

    workspace_base = Path(config.workspace_base).expanduser().resolve()
    staging = workspace_base / ".imported-tasks"
    # Rebuild staging from scratch each run so it reflects EXACTLY the sources
    # and per-source `tasks:` filters configured right now. Without this, tasks
    # imported by a previous (e.g. unfiltered) run linger under staging and get
    # discovered and run even though they're no longer selected. The upstream
    # clone cache (.repo_cache) is untouched, so this does not re-fetch anything.
    # robust_rmtree clears read-only git files so the wipe also works on Windows.
    if staging.exists():
        from .sandbox import robust_rmtree

        robust_rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    total = 0
    for src in sources:
        # Resolve the import source path. When `src.path` is a git URL, clone it
        # (cached under workspace_base/.repo_cache) and import from the local
        # checkout — so the user never has to download the tasks by hand. A plain
        # local path is used as-is.
        try:
            source_path = _resolve_task_source_path(src, workspace_base)
        except Exception as e:
            console.print(
                f"[yellow]⚠ Skipping task source '{src.type}' ({src.path}): {e}[/yellow]"
            )
            continue

        # Determine which tasks to import. An explicit `tasks:` filter always
        # wins. Otherwise, for a terminal-bench source, default to the built-in
        # Terminal-Bench 4.x task set so a default run (no tasks: given) runs
        # only tasks this host-agent harness can grade, instead of the full
        # suite where many tasks fail structurally.
        only = src.tasks or None
        used_default_allowlist = False
        if only is None and src.type.strip().lower() in ("terminal-bench", "tb"):
            from .importers.terminal_bench import terminal_bench_supported_tasks

            only = list(terminal_bench_supported_tasks())
            used_default_allowlist = True
            console.print(
                f"[dim]No tasks: filter given — defaulting to the "
                f"{len(only)} harness-compatible Terminal-Bench task(s). "
                f"Set tasks: explicitly to override.[/dim]"
            )

        try:
            imported = import_task_source(
                source_type=src.type,
                source_path=source_path,
                dest_root=staging,
                native_mode=src.mode,
                only=only,
                timeout_minutes=src.timeout_minutes,
                # A user's explicit tasks: is strict (typos error); the built-in
                # default allowlist is best-effort (skip an upstream-renamed entry).
                strict_only=not used_default_allowlist,
            )
        except ImportError_ as e:
            console.print(f"[yellow]⚠ Skipping task source '{src.type}' ({src.path}): {e}[/yellow]")
            continue
        total += len(imported)
        for rec in imported:
            for w in rec.warnings:
                console.print(f"[yellow]⚠ {rec.native_id}: {w}[/yellow]")
        console.print(
            f"[green]✓[/green] Imported {len(imported)} task(s) from {src.type} ({src.path})"
        )
    if total:
        console.print(f"[dim]Imported tasks staged under {staging}[/dim]")
    return staging


def _scan_task_root(root: Path, config: BenchConfig) -> list[TaskConfig]:
    """Load and filter every task.yaml under one root."""
    found: list[TaskConfig] = []
    for task_yaml_path in sorted(root.rglob("task.yaml")):
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

        # A threshold set in task.yaml wins. Otherwise the run-level
        # `functional_pass_threshold` from the config applies: without this the
        # config key was silently ignored and every such task fell back to
        # TaskConfig's own 0.99 default.
        if "functional_pass_threshold" not in tc.model_fields_set:
            tc.functional_pass_threshold = config.functional_pass_threshold

        found.append(tc)
    return found


def discover_tasks(config: BenchConfig) -> list[TaskConfig]:
    """
    Load all task.yaml files from the active task root and apply task-id and
    mode filters. cli-compare silently skips spec-driven tasks (it is vibe-only).

    When ``task_sources`` are configured (e.g. Terminal-Bench 4.x), they are
    converted to native fixtures and run *instead of* the repo's own ``tasks/``
    tree — configuring an external benchmark makes it the exclusive task set, so
    a Terminal-Bench run isn't diluted by this repo's sample tasks. Without any
    ``task_sources`` the local ``tasks_dir`` is used as before.
    """
    tasks_root = Path(config.tasks_dir).expanduser().resolve()

    # External benchmarks are exclusive: when any enabled task_source is
    # configured, run ONLY the imported tasks and skip tasks_dir. Otherwise fall
    # back to the repo's local tasks tree.
    has_sources = any(s.enabled for s in getattr(config, "task_sources", []))
    staging = materialize_task_sources(config)

    roots: list[Path] = []
    if has_sources:
        if staging is not None and staging.exists():
            roots.append(staging)
        else:
            raise ValueError(
                "task_sources are configured but no tasks could be imported from them. "
                "Check the source path/URL, ref, subdir, and task filters."
            )
    else:
        if tasks_root.exists():
            roots.append(tasks_root)
        if not roots:
            raise FileNotFoundError(
                f"Tasks directory not found: {tasks_root} (and no importable task_sources)"
            )

    task_configs: list[TaskConfig] = []
    seen_ids: set[str] = set()
    for root in roots:
        for tc in _scan_task_root(root, config):
            if tc.id in seen_ids:
                console.print(f"[yellow]⚠ Duplicate task id '{tc.id}' — keeping the first[/yellow]")
                continue
            seen_ids.add(tc.id)
            task_configs.append(tc)

    if not task_configs:
        where = "imported task_sources" if has_sources else f"'{tasks_root}'"
        raise ValueError(
            f"No tasks found in {where} matching the configured filters. "
            "Check task_ids and modes"
            + (" and the task_sources filters." if has_sources else " and tasks_dir.")
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
