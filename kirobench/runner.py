"""
Benchmark runner: orchestrates task × target × repeat execution and evaluation
for both modes (cli-compare and model-compare).

Parallel execution with a semaphore, ``.partial.json`` checkpoints, SIGINT/
SIGTERM handling, unavailable/no-op detection, a functional hard-gate plus a
score-threshold PASS/FAIL decision, and unified aggregations (USD + native cost,
repeat stats).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import discover_tasks
from .evaluator import (
    FunctionalEvaluator,
    SpecQualityEvaluator,
    SteeringAdherenceEvaluator,
    TaskCompletionEvaluator,
)
from .executor import CLIError, SpecDrivenExecutor, VibeExecutor
from .executor.spec import SpecCapabilityError
from .logger import RunLogger
from .models import (
    BenchConfig,
    BenchmarkRun,
    CompareMode,
    PhaseResult,
    RunResult,
    Target,
    TaskConfig,
    TaskMode,
    TaskStatus,
    _utcnow,
)
from .preflight import check_targets_available, docker_report, repo_report, validate_models
from .sandbox import Workspace

_IS_TTY = (
    sys.stdout.isatty()
    and os.environ.get("TERM", "dumb") not in ("dumb", "")
    and os.environ.get("NO_COLOR") is None
)

console = Console(highlight=False, force_terminal=_IS_TTY)


def _restore_terminal() -> None:
    """Undo interactive terminal modes a spawned CLI may have left enabled.

    Headless coding CLIs sometimes turn on xterm focus reporting (?1004) or
    bracketed paste (?2004) and exit without resetting them, which makes the
    shell echo raw focus events like ``^[[I`` / ``^[[O``. We defensively disable
    those modes (and re-show the cursor) when a run finishes. No-op when stdout
    is not a real terminal."""
    if not sys.stdout.isatty():
        return
    try:
        # ?1004l focus reporting off, ?2004l bracketed paste off, ?25h show cursor
        sys.stdout.write("\x1b[?1004l\x1b[?2004l\x1b[?25h")
        sys.stdout.flush()
    except Exception:
        pass


class BenchmarkRunner:
    def __init__(self, config: BenchConfig):
        self.config = config
        self.run_id = _utcnow().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self._logger = RunLogger(Path(config.output_dir), self.run_id)

    async def run(self) -> BenchmarkRun:
        tasks = discover_tasks(self.config)
        targets = self.config.enabled_targets()
        repeats = self.config.repeats

        try:
            self._preflight()
        except Exception:
            self._logger.close()
            try:
                self._logger.path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        self._cleanup_old_workspaces()
        self._docker_preflight(tasks)
        self._repo_preflight(tasks)

        bench_run = BenchmarkRun(run_id=self.run_id, config=self.config)
        total = len(tasks) * len(targets) * repeats
        repeat_note = f"  ×  {repeats} repeats" if repeats > 1 else ""
        unit = "CLIs" if self.config.is_cli_compare else "models"
        header_label = (
            f"  Comparison: {self.config.comparison_label}\n"
            if self.config.is_cli_compare and self.config.comparison_label
            else ""
        )
        console.print(
            f"\nkirobench {self.config.mode.value}  run {self.run_id}\n"
            f"{header_label}"
            f"  Tasks: {len(tasks)}  ×  {unit}: {len(targets)}{repeat_note}  =  {total} runs\n"
            f"  {unit}: {', '.join(t.label for t in targets)}\n"
            f"  Concurrency: {self.config.effective_workers(len(tasks))} parallel run(s)"
            f"  (strategy: {self.config.concurrency})\n"
        )
        await self._logger.log_event(
            f"run started ({self.config.mode.value}) — {len(tasks)} task(s) × "
            f"{len(targets)} target(s) × {repeats} repeat(s) = {total} runs"
        )

        semaphore = asyncio.Semaphore(self.config.effective_workers(len(tasks)))
        run_start = time.monotonic()
        checkpoint_path = Path(self.config.output_dir) / f"{self.run_id}.partial.json"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

        cancelled = False

        def _handle_signal(sig, frame):
            nonlocal cancelled
            if not cancelled:
                cancelled = True
                console.print("\n[yellow]Interrupted — cancelling and writing checkpoint…[/yellow]")
                loop = asyncio.get_event_loop()
                for t in asyncio.all_tasks(loop):
                    t.cancel()

        old_sigint = signal.signal(signal.SIGINT, _handle_signal)
        old_sigterm = signal.signal(signal.SIGTERM, _handle_signal)

        done = [0]

        def _log_done(result: RunResult) -> None:
            elapsed = time.monotonic() - run_start
            icon = {
                TaskStatus.PASSED: "✓",
                TaskStatus.FAILED: "✗",
                TaskStatus.TIMEOUT: "⏱",
            }.get(result.status, "⚠")
            rep = f" #{result.repeat}" if repeats > 1 else ""
            cost = f"  ${result.cost_usd:.4f}" if result.cost_usd is not None else ""
            cred = f"  {result.total_credits:.3f}cr" if result.total_credits else ""
            t = f"  {result.cli_reported_seconds or result.duration_seconds:.0f}s"
            console.print(
                f"  [{done[0]:>{len(str(total))}}/{total}]  {icon} {result.status.value:<7}  "
                f"{result.task_id} / {result.target}{rep}{cost}{cred}{t}  ({elapsed:.0f}s elapsed)"
            )

        async def run_one(task: TaskConfig, target: Target, repeat: int) -> RunResult:
            async with semaphore:
                rep = f" #{repeat}" if repeats > 1 else ""
                console.print(
                    f"  [dim]· running  {task.id} / {target.label}{rep} "
                    f"({task.mode.value}) …[/dim]"
                )
                result = await self._run_single(task, target, repeat)
                bench_run.results.append(result)
                self._write_checkpoint(bench_run, checkpoint_path)
                done[0] += 1
                _log_done(result)
                return result

        try:
            jobs = [
                run_one(task, target, rep)
                for task in tasks
                for target in targets
                for rep in range(1, repeats + 1)
            ]
            await asyncio.gather(*jobs, return_exceptions=True)
        except asyncio.CancelledError:
            console.print(
                f"\n[yellow]Run cancelled after {done[0]}/{total}. "
                f"Partial results → {checkpoint_path}[/yellow]"
            )
        finally:
            signal.signal(signal.SIGINT, old_sigint)
            signal.signal(signal.SIGTERM, old_sigterm)
            _restore_terminal()

        if not cancelled and checkpoint_path.exists():
            checkpoint_path.unlink(missing_ok=True)

        bench_run.finished_at = _utcnow()
        await self._logger.log_event(
            f"run finished — {bench_run.passed_runs}/{bench_run.total_runs} passed "
            f"({bench_run.pass_rate:.0%})  total_cost=${bench_run.total_cost_usd:.4f}  "
            f"total_credits={bench_run.total_credits:.3f}"
        )
        self._logger.close()
        console.print(f"[dim]run log → {self._logger.path}[/dim]")
        self._print_summary(bench_run)
        return bench_run

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def _preflight(self) -> None:
        missing = check_targets_available(self.config)
        if missing:
            console.print(
                f"\n  ERROR: CLI binaries not found: {', '.join(missing)}\n"
                f"  Install them or fix cli_path in your config. Aborting.\n"
            )
            raise ValueError(f"CLI binaries not found: {', '.join(missing)}")

        if self.config.mode == CompareMode.MODEL_COMPARE:
            valid, invalid = validate_models(self.config)
            if invalid:
                console.print(
                    f"\n  ERROR: unknown model id(s): {', '.join(invalid)}\n"
                    f"  An invalid model causes the Kiro CLI to hang silently.\n"
                    f"  Available: {', '.join(valid) or 'none queried'}\n"
                )
                raise ValueError(
                    f"Invalid model id(s): {', '.join(invalid)}. "
                    f"Available: {', '.join(valid) or 'none queried'}."
                )

    def _docker_preflight(self, tasks) -> None:
        """Non-fatal up-front check for Docker-verified tasks: confirm the daemon
        is up and the required images are present locally. Missing pieces are
        WARNED (those tasks will be reported as harness errors, not model
        failures) — non-Docker tasks still run."""
        rep = docker_report(tasks)
        if not rep["needs_docker"]:
            return
        imgs = ", ".join(rep["required"])
        if not rep["docker_ok"]:
            console.print(
                f"[yellow]⚠ {rep['tasks_blocked']} task(s) need Docker but the daemon "
                f"isn't reachable.[/yellow] They will be skipped as harness errors.\n"
                f"  Needed images: {imgs}\n"
                f"  Start Docker, then build images: ./tasks/docker/build-images.sh"
            )
        elif rep["missing_images"]:
            missing = ", ".join(rep["missing_images"])
            console.print(
                f"[yellow]⚠ Docker is up but these images are missing:[/yellow] {missing}\n"
                f"  {rep['tasks_blocked']} task(s) can't verify until you run: "
                f"./tasks/docker/build-images.sh"
            )
            # Show what this (subprocess) Docker actually sees — usually a
            # context mismatch between the shell and the harness.
            try:
                from .verify.docker_env import diagnostic
                console.print(f"[dim]{diagnostic(rep['missing_images'][0])}[/dim]")
            except Exception:
                pass
        else:
            console.print(f"[dim]Docker OK — verification images present: {imgs}[/dim]")

    def _repo_preflight(self, tasks) -> None:
        """Non-fatal up-front check for repo-based tasks."""
        rep = repo_report(tasks)
        if not rep["needs_git"]:
            return
        if not rep["git_ok"]:
            console.print(
                f"[yellow]⚠ {rep['tasks']} task(s) need 'git' but it was not found on PATH.[/yellow] "
                "Install git and re-run."
            )
        if rep["unpinned"]:
            console.print(
                f"[yellow]⚠ repo.ref is not a pinned SHA for:[/yellow] "
                f"{', '.join(rep['unpinned'])}  — results may differ as the branch advances."
            )
        if rep["git_ok"]:
            console.print(
                f"[dim]git OK — {rep['tasks']} repo task(s) will clone on first run "
                f"(cached in workspace_base/.repo_cache)[/dim]"
            )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    async def _run_single(self, task: TaskConfig, target: Target, repeat: int = 1) -> RunResult:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", target.name)
        run_id = f"{self.run_id}_{task.id}_{safe}"
        if self.config.repeats > 1:
            run_id += f"_r{repeat}"

        result = RunResult(
            task_id=task.id,
            target=target.label,
            repeat=repeat,
            mode=task.mode,
            status=TaskStatus.RUNNING,
        )
        await self._logger.log_event(
            f"START  {task.id}  target={target.label}  repeat={repeat}  mode={task.mode.value}"
        )

        workspace = Workspace(run_id, task, self.config)
        try:
            workspace.setup()
        except Exception as e:
            result.status = TaskStatus.ERROR
            result.error_message = f"Workspace setup failed: {e}"
            result.finished_at = _utcnow()
            return result
        result.workspace_path = str(workspace.path)

        try:
            task_timeout = self._task_timeout_seconds(task)
            phase_results = await asyncio.wait_for(
                self._execute(task, target, workspace.path), timeout=task_timeout
            )
            result.phase_results = phase_results
            all_phases_ok = all(p.success for p in phase_results)
            result.agent_stdout = "\n---\n".join(p.stdout for p in phase_results if p.stdout)
            result.agent_stderr = "\n---\n".join(p.stderr for p in phase_results if p.stderr)

            self._aggregate_usage(result, phase_results)
            result.transient_retries = sum(p.transient_retries for p in phase_results)

            # Service / infrastructure failures → UNAVAILABLE (not a model fault)
            unavailable_phases = [
                p for p in phase_results if p.model_unavailable or p.transient_error
            ]
            if unavailable_phases:
                result.status = TaskStatus.UNAVAILABLE
                result.error_message = (
                    f"'{target.label}' hit a service/infrastructure issue during "
                    f"{', '.join(p.phase for p in unavailable_phases)} phase(s). "
                    "Re-run when the service is stable."
                )
                return result

            # A phase that hit its timeout means the CLI never returned (e.g. an
            # interactive spec session that doesn't exit headless). That's a
            # TIMEOUT, not a pass — even if leftover files happen to satisfy the
            # verify suite. Skip the expensive evaluation in that case.
            timed_out_phases = [p for p in phase_results if p.timed_out]
            if timed_out_phases:
                result.status = TaskStatus.TIMEOUT
                result.error_message = timed_out_phases[0].error
                return result

            # Evaluate
            await self._evaluate(result, task, workspace.path)

            fr = result.functional_result
            if fr and fr.checkpoints.get("harness_error"):
                result.status = TaskStatus.ERROR
                result.error_message = fr.summary
            elif self._is_noop_run(result, all_phases_ok):
                result.status = TaskStatus.UNAVAILABLE
                result.error_message = (
                    f"'{target.label}' produced no output and consumed no credits — "
                    "it did not run (provisioning/availability issue)."
                )
            else:
                result.status = self._determine_status(result, task, all_phases_ok)

        except (asyncio.TimeoutError, TimeoutError) as e:
            result.status = TaskStatus.TIMEOUT
            result.error_message = str(e) or "Task exceeded total timeout"
        except SpecCapabilityError as e:
            result.status = TaskStatus.SKIPPED
            result.error_message = str(e)
        except CLIError as e:
            result.status = TaskStatus.ERROR
            result.error_message = str(e)
        except asyncio.CancelledError:
            result.status = TaskStatus.ERROR
            result.error_message = "Cancelled (Ctrl+C)"
            raise
        except Exception as e:
            result.status = TaskStatus.ERROR
            result.error_message = str(e)
        finally:
            result.finished_at = _utcnow()
            cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "n/a"
            await self._logger.log_event(
                f"END    {task.id}  target={target.label}  status={result.status.value}"
                f"  func={result.functional_score:.2f}  final={result.final_score:.2f}  cost={cost}"
            )
            workspace.teardown()
        return result

    async def _execute(
        self, task: TaskConfig, target: Target, workspace_path: Path
    ) -> list[PhaseResult]:
        if task.mode == TaskMode.VIBE:
            executor = VibeExecutor(self.config, task, workspace_path, target, logger=self._logger)
        elif task.mode == TaskMode.SPEC_DRIVEN:
            executor = SpecDrivenExecutor(
                self.config, task, workspace_path, target, logger=self._logger
            )
        else:
            raise ValueError(f"Unknown task mode: {task.mode}")
        return await executor.execute()

    @staticmethod
    def _aggregate_usage(result: RunResult, phases: list[PhaseResult]) -> None:
        cost_vals = [p.cost_usd for p in phases if p.cost_usd is not None]
        result.cost_usd = sum(cost_vals) if cost_vals else None

        credit_vals = [p.credits for p in phases if p.credits is not None]
        result.raw_credits = sum(credit_vals) if credit_vals else None
        result.total_credits = sum(credit_vals)

        prem_vals = [p.premium_requests for p in phases if p.premium_requests is not None]
        result.premium_requests = sum(prem_vals) if prem_vals else None

        in_vals = [p.input_tokens for p in phases if p.input_tokens is not None]
        out_vals = [p.output_tokens for p in phases if p.output_tokens is not None]
        result.input_tokens = sum(in_vals) if in_vals else None
        result.output_tokens = sum(out_vals) if out_vals else None

        result.cli_reported_seconds = sum(
            p.cli_reported_seconds for p in phases if p.cli_reported_seconds is not None
        )

    @staticmethod
    def _is_noop_run(result: RunResult, all_phases_ok: bool) -> bool:
        if not all_phases_ok:
            return False
        if result.total_credits and result.total_credits > 0:
            return False
        if result.cost_usd:
            return False
        fr = result.functional_result
        if fr is None:
            return False
        cp = fr.checkpoints.get("code_exists")
        if isinstance(cp, dict):
            return not cp.get("passed", True)
        if isinstance(cp, bool):
            return not cp
        return fr.score == 0.0 and not (result.agent_stdout or "").strip()

    def _determine_status(
        self, result: RunResult, task: TaskConfig, all_phases_ok: bool
    ) -> TaskStatus:
        has_functional_gate = task.scoring.functional_tests > 0
        if has_functional_gate:
            fr = result.functional_result
            score = fr.score if fr else 0.0
            return (
                TaskStatus.PASSED
                if score >= task.functional_pass_threshold
                else TaskStatus.FAILED
            )
        if not all_phases_ok:
            return TaskStatus.FAILED
        return (
            TaskStatus.PASSED
            if result.final_score >= self.config.pass_threshold
            else TaskStatus.FAILED
        )

    async def _evaluate(self, result: RunResult, task: TaskConfig, workspace_path: Path) -> None:
        weights = task.scoring
        evaluated: set[str] = set()

        func_result = await FunctionalEvaluator(
            task, workspace_path, logger=self._logger,
            config=self.config, model_label=result.target,
        ).evaluate()
        result.functional_result = func_result
        result.functional_score = func_result.score
        if weights.functional_tests > 0:
            evaluated.add("functional_tests")

        if task.mode == TaskMode.SPEC_DRIVEN and weights.spec_artifact_quality > 0:
            spec_scores = await SpecQualityEvaluator(
                task, workspace_path, self.config, logger=self._logger, model_label=result.target
            ).evaluate()
            result.spec_artifact_scores = spec_scores
            result.spec_artifact_score = spec_scores.overall
            evaluated.add("spec_artifact_quality")

        if task.mode == TaskMode.SPEC_DRIVEN and weights.task_completion_rate > 0:
            result.task_completion_rate = TaskCompletionEvaluator(task, workspace_path).evaluate()
            evaluated.add("task_completion_rate")

        if weights.steering_adherence > 0:
            score, details = await SteeringAdherenceEvaluator(
                task, workspace_path, self.config, logger=self._logger, model_label=result.target
            ).evaluate()
            if not details.get("not_applicable"):
                result.steering_adherence_score = score
                evaluated.add("steering_adherence")

        scores = {
            "functional_tests": result.functional_score,
            "spec_artifact_quality": result.spec_artifact_score,
            "task_completion_rate": result.task_completion_rate,
            "steering_adherence": result.steering_adherence_score,
        }
        weight_map = {
            "functional_tests": weights.functional_tests,
            "spec_artifact_quality": weights.spec_artifact_quality,
            "task_completion_rate": weights.task_completion_rate,
            "steering_adherence": weights.steering_adherence,
        }
        total_weight = sum(weight_map[k] for k in evaluated if weight_map[k] > 0)
        if total_weight > 0:
            result.final_score = (
                sum(scores[k] * weight_map[k] for k in evaluated if weight_map[k] > 0)
                / total_weight
            )
        else:
            result.final_score = result.functional_score

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _task_timeout_seconds(self, task: TaskConfig) -> float:
        if self.config.task_timeout_minutes is not None:
            return self.config.task_timeout_minutes * 60.0
        # Spec tasks have up to 4 phases; vibe just 1. Derive a sensible ceiling.
        phases = 4 if task.mode == TaskMode.SPEC_DRIVEN else 2
        return task.timeout_minutes * 60.0 * phases + 60.0

    def _cleanup_old_workspaces(self) -> None:
        base = Path(self.config.workspace_base)
        if not base.exists():
            return
        removed = 0
        for entry in base.iterdir():
            if not entry.is_dir() or entry.name.startswith(self.run_id):
                continue
            parts = entry.name.split("_")
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            try:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            except Exception:
                pass
        if removed:
            console.print(f"[dim]Cleaned up {removed} old workspace(s) from {base}[/dim]")

    @staticmethod
    def _write_checkpoint(run: BenchmarkRun, path: Path) -> None:
        try:
            payload = {
                "run_id": run.run_id,
                "mode": run.config.mode.value,
                "partial": True,
                "completed": len(run.results),
                "results": [r.to_dict() for r in run.results],
            }
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _print_summary(self, run: BenchmarkRun) -> None:
        console.print()
        unit = "CLI" if self.config.is_cli_compare else "Model"
        table = Table(title=f"Results — {run.run_id}", show_lines=True)
        table.add_column("Task", style="cyan")
        table.add_column(unit, style="yellow")
        table.add_column("Status")
        table.add_column("Functional", justify="right")
        if not self.config.is_cli_compare:
            table.add_column("Final", justify="right")
        table.add_column("Cost (USD)", justify="right", style="magenta")
        table.add_column("Credits", justify="right")
        table.add_column("Time", justify="right", style="dim")

        for r in run.results:
            status_disp = {
                TaskStatus.PASSED: "[green]✓ PASS[/green]",
                TaskStatus.FAILED: "[red]✗ FAIL[/red]",
                TaskStatus.ERROR: "[red]⚠ ERROR[/red]",
                TaskStatus.TIMEOUT: "[yellow]⏱ TIMEOUT[/yellow]",
                TaskStatus.SKIPPED: "[dim]⊘ SKIP[/dim]",
                TaskStatus.UNAVAILABLE: "[magenta]⊘ UNAVAIL[/magenta]",
            }.get(r.status, r.status.value)
            cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "—"
            cred = f"{r.native_credits:.3f}" if r.native_credits is not None else "—"
            secs = r.cli_reported_seconds or r.duration_seconds
            row = [r.task_id, r.target, status_disp, f"{r.functional_score:.0%}"]
            if not self.config.is_cli_compare:
                row.append(f"{r.final_score:.0%}")
            row += [cost, cred, f"{secs:.1f}s" if secs else "—"]
            table.add_row(*row)
        console.print(table)

        self._print_cost_summary(run)
        console.print(
            f"\n[bold]Pass rate:[/bold] {run.pass_rate:.0%}  "
            f"({run.passed_runs}/{run.total_runs})  "
            f"[dim]Total cost: ${run.total_cost_usd:.4f} · "
            f"Total credits: {run.total_credits:.3f} · "
            f"Total time: {run.duration_seconds:.1f}s[/dim]\n"
        )

    def _print_cost_summary(self, run: BenchmarkRun) -> None:
        stats = run.cost_stats_by_target()
        if not stats:
            return
        console.print()
        unit = "CLI" if self.config.is_cli_compare else "Model"
        title = "Cost Comparison"
        if self.config.is_cli_compare and self.config.comparison_label:
            title += f" — {self.config.comparison_label}"
        table = Table(title=title, show_lines=False)
        table.add_column(unit, style="yellow")
        table.add_column("Pass Rate", justify="right")
        table.add_column("Avg Cost/Run", justify="right", style="magenta")
        table.add_column("Cost/Success", justify="right", style="bold magenta")
        table.add_column("Avg Credits/Run", justify="right")
        table.add_column("Avg Latency", justify="right", style="dim")
        table.add_column("Runs", justify="right", style="dim")

        def _key(kv):
            cpp = kv[1]["cost_per_pass"]
            return cpp if cpp != float("inf") else 1e18

        for target, s in sorted(stats.items(), key=_key):
            cpp = s["cost_per_pass"]
            cpp_str = "n/a (0 pass)" if cpp == float("inf") else f"${cpp:.4f}"
            avg = f"${s['avg_cost_usd']:.4f}" if s["has_cost"] else "—"
            cred = f"{s['avg_credits']:.3f}" if s["has_credits"] else "—"
            table.add_row(
                target,
                f"{s['pass_rate']:.0%}",
                avg,
                cpp_str,
                cred,
                f"{s['avg_latency_seconds']:.1f}s",
                f"{int(s['passed'])}/{int(s['runs'])}",
            )
        console.print(table)
