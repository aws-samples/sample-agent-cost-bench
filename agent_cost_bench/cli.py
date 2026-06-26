"""
agent_cost_bench CLI entry point.

Two command groups share one execution/reporting core:

  agent_cost_bench cli-compare   run|validate|list-tasks  config.yaml ...
  agent_cost_bench model-compare run|validate|list-tasks  config.yaml ...

Plus shared top-level commands:

  agent_cost_bench report   results/<run>.json
  agent_cost_bench new-task TASK_ID --mode vibe|spec-driven
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import discover_tasks, load_cli_compare_config, load_model_compare_config
from .models import CompareMode
from .preflight import check_targets_available
from .reporter import HTMLReporter, JSONReporter
from .runner import BenchmarkRunner

console = Console()

# Template written by `new-task --with-tests`. A failing test so an unfinished
# task is never a silent false PASS (score would be 0 / 1 = 0.0).
_SCORE_TEMPLATE = '''"""Harness tests for this task.

Run by the centralized pytest runner in an isolated venv.
WORKSPACE env var points to the model\'s output directory.
Score = fraction of test functions that pass.
"""
import os
from pathlib import Path
import pytest

WS = Path(os.environ.get("WORKSPACE", "."))


def test_todo_implement_me():
    """Replace with real assertions against WS (the model\'s output directory)."""
    pytest.fail("verify/test_task.py not implemented yet — add real tests")
'''


@click.group()
@click.version_option(package_name="agent_cost_bench")
def main():
    """agent_cost_bench — unified CLI/model benchmark framework (cli-compare + model-compare)."""
    pass


# ---------------------------------------------------------------------------
# Shared implementations (parameterised by mode)
# ---------------------------------------------------------------------------


def _load_or_exit(config_path: str, mode: CompareMode):
    try:
        if mode == CompareMode.CLI_COMPARE:
            return load_cli_compare_config(config_path)
        return load_model_compare_config(config_path)
    except Exception as e:
        console.print(f"[red]Config error: {e}[/red]")
        sys.exit(1)


def _do_run(cfg, no_open: bool) -> None:
    runner_obj = BenchmarkRunner(cfg)
    try:
        bench_run = asyncio.run(runner_obj.run())
    except ValueError as e:
        console.print(f"[red]Aborted: {e}[/red]")
        sys.exit(2)

    output_path = Path(cfg.output_dir)
    json_path = JSONReporter(output_path).write(bench_run)
    console.print(f"[dim]JSON results → {json_path}[/dim]")
    html_path = HTMLReporter(output_path, title=cfg.report_title, mode=cfg.mode).write(bench_run)
    console.print(f"[bold green]HTML report → {html_path}[/bold green]")

    if cfg.open_report and not no_open:
        _open_file(html_path)
    sys.exit(0 if bench_run.pass_rate == 1.0 else 1)


def _do_validate(cfg) -> None:
    console.print(f"[green]✓[/green] Config loaded ({cfg.mode.value})")
    if cfg.is_cli_compare and cfg.comparison_label:
        console.print(f"  Comparison: {cfg.comparison_label}")
    unit = "Runners" if cfg.is_cli_compare else "Models"
    console.print(f"  {unit}:")
    for t in cfg.targets:
        state = "" if t.enabled else " [dim](disabled)[/dim]"
        console.print(
            f"    - {t.label}{state}: {t.cli_path} "
            f"(model={t.model_id}, cost={t.cost_source.value}, spec={t.capabilities.supports_spec})"
        )
    missing = check_targets_available(cfg)
    if missing:
        console.print(f"  [yellow]⚠ CLI binaries not found:[/yellow] {', '.join(missing)}")
    else:
        console.print("  [green]✓[/green] all target binaries resolved on PATH")

    try:
        tasks = discover_tasks(cfg)
        console.print(f"[green]✓[/green] {len(tasks)} task(s) discovered")
        for t in tasks:
            issues = _validate_task(t, cfg)
            if issues:
                for issue in issues:
                    console.print(f"  [yellow]⚠[/yellow] {t.id}: {issue}")
            else:
                console.print(f"  [green]✓[/green] {t.id} ({t.mode.value})")
    except Exception as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    _validate_docker(tasks)
    console.print("\n[bold green]Validation passed.[/bold green]")


def _do_list_tasks(cfg) -> None:
    try:
        tasks = discover_tasks(cfg)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)
    table = Table(title=f"Tasks in '{cfg.tasks_dir}'", show_lines=True)
    table.add_column("ID", style="cyan")
    table.add_column("Mode")
    table.add_column("Description")
    table.add_column("Timeout")
    for t in tasks:
        table.add_row(
            t.id,
            t.mode.value,
            t.description[:60] + ("…" if len(t.description) > 60 else ""),
            f"{t.timeout_minutes}m",
        )
    console.print(table)
    console.print(f"\n[dim]Total: {len(tasks)} task(s)[/dim]")


# ---------------------------------------------------------------------------
# cli-compare group
# ---------------------------------------------------------------------------


@main.group("cli-compare")
def cli_compare():
    """Compare the same model across coding CLIs on cost (vibe tasks)."""
    pass


@cli_compare.command("run")
@click.argument("config", type=click.Path(exists=True))
@click.option("--task", "-t", multiple=True, help="Run specific task IDs only")
@click.option("--runner", "-r", multiple=True, help="Run only these runner names")
@click.option("--output-dir", "-o", default=None)
@click.option("--no-open", is_flag=True, default=False)
@click.option("--repeats", default=None, type=int)
@click.option("--timeout", default=None, type=int)
def cli_compare_run(config, task, runner, output_dir, no_open, repeats, timeout):
    """Run the cost benchmark defined in CONFIG."""
    cfg = _load_or_exit(config, CompareMode.CLI_COMPARE)
    if task:
        cfg.task_ids = list(task)
    if runner:
        wanted = set(runner)
        for t in cfg.targets:
            t.enabled = t.name in wanted or t.label in wanted
        if not cfg.enabled_targets():
            console.print(f"[red]No runners match: {', '.join(runner)}[/red]")
            sys.exit(1)
    if output_dir:
        cfg.output_dir = output_dir
    if repeats is not None:
        cfg.repeats = repeats
    if timeout is not None:
        cfg.timeout_minutes = timeout
    _do_run(cfg, no_open)


@cli_compare.command("validate")
@click.argument("config", type=click.Path(exists=True))
def cli_compare_validate(config):
    """Validate a cli-compare CONFIG and its task fixtures."""
    _do_validate(_load_or_exit(config, CompareMode.CLI_COMPARE))


@cli_compare.command("list-tasks")
@click.argument("config", type=click.Path(exists=True))
def cli_compare_list_tasks(config):
    """List tasks discovered from a cli-compare CONFIG."""
    _do_list_tasks(_load_or_exit(config, CompareMode.CLI_COMPARE))


# ---------------------------------------------------------------------------
# model-compare group
# ---------------------------------------------------------------------------


@main.group("model-compare")
def model_compare():
    """Compare models inside the Kiro CLI on quality + cost (vibe + spec)."""
    pass


@model_compare.command("run")
@click.argument("config", type=click.Path(exists=True))
@click.option("--task", "-t", multiple=True, help="Run specific task IDs only")
@click.option("--model", "-m", multiple=True, help="Override models from config")
@click.option("--mode", type=click.Choice(["vibe", "spec-driven"]), help="Filter by task mode")
@click.option("--output-dir", "-o", default=None)
@click.option("--no-open", is_flag=True, default=False)
@click.option("--repeats", default=None, type=int)
@click.option("--timeout", default=None, type=int)
@click.option("--judge-model", default=None, help="Enable LLM judge with this model id")
@click.option("--judge-weight", default=None, type=float, help="Judge blend weight (0-1)")
def model_compare_run(
    config, task, model, mode, output_dir, no_open, repeats, timeout, judge_model, judge_weight
):
    """Run the quality+cost benchmark defined in CONFIG."""
    from .models import TaskMode
    from .targets import make_kiro_target

    cfg = _load_or_exit(config, CompareMode.MODEL_COMPARE)
    if task:
        cfg.task_ids = list(task)
    if model:
        usd = cfg.targets[0].pricing.usd_per_credit if cfg.targets else None
        cfg.targets = [
            make_kiro_target(m, default_cli_path=cfg.kiro_cli_path, usd_per_credit=usd)
            for m in model
        ]
    if mode:
        cfg.modes = [TaskMode(mode)]
    if output_dir:
        cfg.output_dir = output_dir
    if repeats is not None:
        cfg.repeats = repeats
    if timeout is not None:
        cfg.timeout_minutes = timeout
    if judge_model is not None:
        cfg.judge_model = judge_model
    if judge_weight is not None:
        cfg.judge_weight = judge_weight
    _do_run(cfg, no_open)


@model_compare.command("validate")
@click.argument("config", type=click.Path(exists=True))
def model_compare_validate(config):
    """Validate a model-compare CONFIG and its task fixtures."""
    _do_validate(_load_or_exit(config, CompareMode.MODEL_COMPARE))


@model_compare.command("list-tasks")
@click.argument("config", type=click.Path(exists=True))
def model_compare_list_tasks(config):
    """List tasks discovered from a model-compare CONFIG."""
    _do_list_tasks(_load_or_exit(config, CompareMode.MODEL_COMPARE))


# ---------------------------------------------------------------------------
# Shared top-level commands
# ---------------------------------------------------------------------------


@main.command("report")
@click.argument("results_json", type=click.Path(exists=True))
@click.option("--output-dir", "-o", default="results")
@click.option("--no-open", is_flag=True, default=False)
def report(results_json, output_dir, no_open):
    """Generate an HTML report from a results or .partial.json file (mode auto-detected)."""
    import json as _json

    from .models import (
        BenchConfig,
        BenchmarkRun,
        FunctionalTestResult,
        RunResult,
        Target,
        TaskMode,
        TaskStatus,
        _utcnow,
    )

    try:
        data = _json.loads(Path(results_json).read_text())
    except Exception as e:
        console.print(f"[red]Could not read {results_json}: {e}[/red]")
        sys.exit(1)

    mode = CompareMode(data.get("mode", "model-compare"))
    cfg = BenchConfig(
        mode=mode,
        comparison_label=data.get("comparison_label", data.get("shared_model") or "cross-CLI comparison"),
        targets=[Target(name="unknown", cli_path="unknown", model_id="unknown")],
    )
    run_obj = BenchmarkRun(run_id=data.get("run_id", "partial"), config=cfg)
    run_obj.finished_at = _utcnow()

    for r in data.get("results", []):
        try:
            scores = r.get("scores", {})
            usage = r.get("usage", {})
            tr = RunResult(
                task_id=r["task_id"],
                target=r.get("target") or r.get("runner") or r.get("model", "unknown"),
                mode=TaskMode(r.get("mode", "vibe")),
                status=TaskStatus(r["status"]),
                repeat=r.get("repeat", 1),
            )
            tr.functional_score = scores.get("functional", r.get("functional_score", 0.0))
            tr.spec_artifact_score = scores.get("spec_artifact", 0.0)
            tr.task_completion_rate = scores.get("task_completion", 0.0)
            tr.final_score = scores.get("final", 0.0)
            tr.cost_usd = usage.get("cost_usd")
            tr.input_tokens = usage.get("input_tokens")
            tr.cached_input_tokens = usage.get("cached_input_tokens")
            tr.output_tokens = usage.get("output_tokens")
            tr.reasoning_output_tokens = usage.get("reasoning_output_tokens")
            tr.cli_reported_seconds = usage.get("cli_reported_seconds", 0.0) or 0.0
            tr.raw_credits = usage.get("raw_credits")
            tr.premium_requests = usage.get("premium_requests")
            tr.total_credits = usage.get("total_credits", 0.0) or 0.0
            fr = r.get("functional_result")
            if fr:
                tr.functional_result = FunctionalTestResult(
                    passed=fr.get("passed", False),
                    exit_code=fr.get("exit_code", -1),
                    score=fr.get("score", 0.0),
                    checkpoints=fr.get("checkpoints", {}) or {},
                    summary=fr.get("summary", ""),
                    stdout=fr.get("stdout", ""),
                    stderr=fr.get("stderr", ""),
                )
            transcript = r.get("transcript", {}) or {}
            tr.agent_stdout = transcript.get("stdout", "") or ""
            tr.agent_stderr = transcript.get("stderr", "") or ""
            tr.error_message = r.get("error_message")
            run_obj.results.append(tr)
        except Exception:
            pass

    out = Path(output_dir)
    title = f"agent_cost_bench {mode.value} (report) — {run_obj.run_id}"
    html_path = HTMLReporter(out, title=title, mode=mode).write(run_obj)
    console.print(f"[bold green]HTML report → {html_path}[/bold green]")
    console.print(f"  {len(run_obj.results)} result(s) loaded")
    if not no_open:
        _open_file(html_path)


@main.command("new-task")
@click.argument("task_id")
@click.option("--mode", type=click.Choice(["vibe", "spec-driven"]), default="vibe")
@click.option("--tasks-dir", default="tasks", help="Root tasks directory")
@click.option(
    "--with-tests",
    is_flag=True,
    default=False,
    help="Scaffold a code-verification skeleton (verify/) instead of a no-code rubric.",
)
@click.option(
    "--repo",
    default=None,
    help="Seed the task with a repo: block (provide the GitHub URL).",
)
def new_task(task_id, mode, tasks_dir, with_tests, repo):
    """Scaffold a new task fixture directory (mode-aware).

    By default this scaffolds a NO-CODE task graded by an LLM-judge checklist
    (quality.rubric) so non-programmers can author a task with just a prompt and
    a list of acceptance criteria. Use --with-tests for the code-verification
    skeleton: a verify/score.py scorer run by the framework's centralized local
    verifier (no per-task shell). We never scaffold something that silently
    scores 100%.
    """
    task_dir = Path(tasks_dir) / mode / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    task_yaml = task_dir / "task.yaml"
    prompt_block = (
        "prompt: |\n"
        "  TODO: write the vibe coding prompt here.\n"
    )
    rubric_block = (
        "\n# No-code quality grading: the LLM judge checks each criterion against\n"
        "# the produced files (met/unmet). Score = fraction met. Requires\n"
        "# judge_model in your model-compare config. Edit these to fit your task.\n"
        "quality:\n"
        "  rubric:\n"
        '    - "TODO: first acceptance criterion (e.g. exposes GET /items)"\n'
        '    - "TODO: second acceptance criterion (e.g. returns 404 for missing items)"\n'
        "  # reference_solution: ref/   # optional golden solution to anchor grading\n"
    )
    verify_block = (
        "\n# Centralized pytest verification: the framework creates an isolated venv,\n"
        "# installs `deps`, then runs verify/test_*.py. Score = fraction of tests passing.\n"
        "# No per-task shell script needed.\n"
        "verify:\n"
        "  runner: pytest\n"
        '  deps: []          # e.g. ["pyyaml==6.0.2"]\n'
    )
    repo_block = ""
    if repo:
        repo_block = (
            "\n# GitHub repo cloned into workspace/src/ before the model runs.\n"
            "# Pin ref to a full 40-char commit SHA for reproducibility.\n"
            "repo:\n"
            f"  url: {repo}\n"
            "  ref: main   # TODO: replace with a pinned SHA\n"
            "  # subdir: src   # optional: sparse-checkout a subdirectory\n"
            "  # depth: 1      # shallow clone (fastest)\n"
        )
    if not task_yaml.exists():
        body = (
            f"id: {task_id}\n"
            f"mode: {mode}\n"
            f'description: "TODO: describe this task"\n'
            f"timeout_minutes: 15\n"
        )
        if mode == "vibe":
            body += prompt_block
        body += verify_block if with_tests else rubric_block
        if repo_block:
            body += repo_block
        task_yaml.write_text(body)

    if mode == "spec-driven":
        seed_dir = task_dir / "seed"
        seed_dir.mkdir(exist_ok=True)
        req = seed_dir / "requirements.md"
        if not req.exists():
            req.write_text("# Requirements\n\nTODO: write EARS-style requirements.\n")

    if with_tests:
        # Pytest-based verification skeleton. The framework discovers test_*.py
        # files in verify/ automatically and runs them in an isolated venv.
        verify_dir = task_dir / "verify"
        verify_dir.mkdir(exist_ok=True)
        scorer = verify_dir / "test_task.py"
        if not scorer.exists():
            scorer.write_text(_SCORE_TEMPLATE)

    kind = "code-verification (verify/test_task.py)" if with_tests else "no-code rubric"
    if repo:
        kind += " + repo clone"
    console.print(
        f"[green]✓[/green] Scaffolded {kind} task at [cyan]{task_dir}[/cyan]"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_task(task, cfg=None) -> list[str]:
    issues = []
    task_dir = task.task_dir
    if task_dir is None:
        return ["task_dir not set"]

    prompt_text = task.prompt.strip() if getattr(task, "prompt", "") else ""

    if task.mode.value == "vibe":
        if not prompt_text:
            issues.append("missing prompt (set 'prompt:' in task.yaml)")
    elif task.mode.value == "spec-driven":
        # Native spec mode is driven by a prompt; a seeded requirements.md is an
        # alternative. Require at least one.
        has_seed_req = (task_dir / "seed" / "requirements.md").exists()
        if not (prompt_text or has_seed_req):
            issues.append("spec task needs a 'prompt:' in task.yaml or seed/requirements.md")

    has_rubric = task.quality is not None and bool(task.quality.rubric)
    verify_script = _find_task_verify_script(task_dir)
    has_pytest = (task_dir / "verify").exists() and any((task_dir / "verify").glob("test_*.py"))
    has_verify = any([
        task.verify is not None,
        verify_script is not None,
        has_pytest,
    ])

    if not has_verify and not has_rubric:
        issues.append(
            "no quality signal (add a quality.rubric, a verify: block "
            "(runner: local/docker), or verify/test_*.py)"
        )

    # --- Lint: trivial scaffold stub that silently scores every run 100% ---
    if verify_script is not None and _is_stub_verify(verify_script):
        issues.append(
            f"{verify_script.name} looks like the scaffold stub (exits 0 without "
            "running checks) — it will score EVERY run 100%. Replace it with real "
            "checks or add a quality.rubric."
        )

    # --- Lint: both code verification and a rubric → rubric is ignored ---
    if has_rubric and has_verify:
        issues.append(
            "both code verification and quality.rubric are present; code verification "
            "takes precedence and the rubric will be IGNORED."
        )

    # --- Lint: rubric quality checks ---
    if has_rubric:
        if cfg is not None and not getattr(cfg, "judge_model", None):
            issues.append(
                "quality.rubric is set but no judge_model is configured — rubric tasks "
                "cannot be graded. Set judge_model (or pass --judge-model)."
            )
        ref = task.quality.reference_solution
        if ref and not (task_dir / ref).exists():
            issues.append(f"quality.reference_solution path not found: {ref}")

    # --- Lint: repo spec checks ---
    if task.repo is not None:
        if not task.repo.url:
            issues.append("repo.url is required")
        elif not task.repo.is_sha_pinned:
            issues.append(
                f"repo.ref='{task.repo.ref}' is a branch/tag, not a pinned SHA — "
                "results will differ as the branch advances. "
                "Pin to a 40-char commit SHA for reproducibility."
            )
        import shutil as _shutil
        if not _shutil.which("git"):
            issues.append("repo task requires 'git' on PATH but git was not found")

        # Private-repo token checks.
        token_env = task.repo.token_env
        if token_env:
            # Heuristic: catch a literal token pasted where the *variable name*
            # belongs (would otherwise be committed to task.yaml).
            looks_like_token = (
                len(token_env) > 40
                or token_env.startswith(("ghp_", "github_pat_", "glpat-", "gho_"))
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env)
            )
            if looks_like_token:
                issues.append(
                    "repo.token_env should be the NAME of an environment variable "
                    "(e.g. 'GITHUB_TOKEN'), not the token itself — never commit a "
                    "token to task.yaml."
                )
            elif not os.environ.get(token_env):
                issues.append(
                    f"repo.token_env='{token_env}' is set but that environment variable "
                    f"is not exported — the private clone will fail. Run: export {token_env}=<token>"
                )
            if task.repo.url and not task.repo.url.startswith(("https://", "http://")):
                issues.append(
                    "repo.token_env is set but repo.url is not HTTP(S) — token auth "
                    "only applies to HTTPS clones (SSH uses keys)."
                )

    # --- Lint: prompt mentions files the verification never references ---
    if prompt_text and (has_verify or has_rubric):
        unref = _prompt_files_unverified(prompt_text, task, task_dir)
        for fname in unref[:5]:
            issues.append(
                f"prompt asks for '{fname}' but no verification/rubric references it "
                "(it will not be scored)"
            )

    return issues


# Filenames that are generic enough to skip in the prompt/verify mismatch lint.
_COMMON_FILES = {
    "requirements.txt", "package.json", "go.mod", "cargo.toml", "dockerfile",
    "readme.md", ".env", "tsconfig.json", "pyproject.toml",
}


def _find_task_verify_script(task_dir: Path):
    for c in (
        task_dir / "verify" / "verify.sh",
        task_dir / "verify.sh",
        task_dir / "verify" / "verify.py",
        task_dir / "verify.py",
    ):
        if c.exists():
            return c
    return None


def _is_stub_verify(script: Path) -> bool:
    """True when a verify script has no real logic — only comments, a shebang,
    an optional `cd`, and an `exit 0`. Such a stub scores every run 100%."""
    try:
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False
    meaningful = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("cd ") or line in ("set -e", "set -eu", "set -euo pipefail"):
            continue
        meaningful.append(line)
    if not meaningful:
        return True
    # Only an `exit 0` (or `sys.exit(0)`) remains → stub.
    return all(
        ln in ("exit 0", "exit", "sys.exit(0)", "pass") or ln.startswith("exit 0")
        for ln in meaningful
    )


def _prompt_files_unverified(prompt_text: str, task, task_dir: Path) -> list[str]:
    """Filenames the prompt asks the model to produce that are not referenced by
    any verification artifact or rubric criterion — i.e. likely unscored."""
    candidates = set(re.findall(r"[\w./-]+\.[A-Za-z][A-Za-z0-9]{0,4}", prompt_text))
    candidates = {
        c.split("/")[-1] for c in candidates
        if c.split("/")[-1].lower() not in _COMMON_FILES and "." in c.split("/")[-1]
    }
    if not candidates:
        return []

    verify_text = _gather_verification_text(task, task_dir)
    unref = [c for c in sorted(candidates) if c not in verify_text]
    return unref


def _gather_verification_text(task, task_dir: Path) -> str:
    parts: list[str] = []
    # Rubric criteria
    if task.quality is not None and task.quality.rubric:
        parts.extend(task.quality.rubric)
    # Declarative verify: block
    if task.verify is not None:
        parts.append(task.verify.test_cmd or "")
        parts.extend(task.verify.setup or [])
        parts.append(task.verify.tests_subdir or "")
    # Any files under verify/ plus verify.sh/.py at the task root
    verify_files = []
    if (task_dir / "verify").exists():
        verify_files.extend((task_dir / "verify").rglob("*"))
    verify_files.extend([task_dir / "verify.sh", task_dir / "verify.py"])
    for p in verify_files:
        try:
            if p.is_file():
                parts.append(p.name)
                if p.suffix in (".py", ".sh", ".txt", ".md", ".json", ".yaml", ".yml"):
                    parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass
    return "\n".join(parts)


def _validate_docker(tasks) -> None:
    """Report Docker daemon + required-image status for Docker-verified tasks."""
    from .preflight import docker_report

    rep = docker_report(tasks)
    if not rep["needs_docker"]:
        return
    imgs = ", ".join(rep["required"])
    console.print(f"  Docker-verified tasks need image(s): {imgs}")
    if not rep["docker_ok"]:
        console.print(
            "  [yellow]⚠ Docker daemon not reachable[/yellow] — start Docker and run "
            "./tasks/docker/build-images.sh"
        )
    elif rep["missing_images"]:
        console.print(
            f"  [yellow]⚠ missing images:[/yellow] {', '.join(rep['missing_images'])} "
            "— run ./tasks/docker/build-images.sh"
        )
    else:
        console.print("  [green]✓[/green] Docker up and all required images present")


def _open_file(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            # Security: "open" is a static binary; path is a local file we just wrote.
            subprocess.run(["open", str(path)], check=False)  # noqa: S603
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(path)], check=False)  # noqa: S603
        elif sys.platform == "win32":
            # os.startfile avoids spawning a shell (no shell=True), so a crafted
            # path can't be interpreted as a command.
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606  (Windows-only)
    except Exception:
        pass
