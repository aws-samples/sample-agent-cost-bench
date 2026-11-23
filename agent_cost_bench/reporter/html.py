"""
HTML reporter with two render paths sharing common templates and styling:

* cli-compare   — cost-per-success (cheapest first), duration, per-target Model
  column, charts, transcripts.
* model-compare — pass rate, 4-dimension quality table, cost + credits, pass@k,
  mode breakdown, drill-downs.

Cost columns always show USD and native units (credits / premium requests).
Charts use a bundled Chart.js (no external CDN dependency).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..models import BenchmarkRun, CompareMode, TaskMode, TaskStatus, _clip_transcript


def _tail(text: str | None, limit: int) -> str:
    """Keep the END of verifier output: test runners print their failure
    summary last, so the start of a long log rarely says what failed."""
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


class HTMLReporter:
    def __init__(self, output_dir: str | Path, title: str = "agent_cost_bench results", mode: CompareMode | None = None):
        self.output_dir = Path(output_dir)
        self.title = title
        self._mode = mode
        self._template_dir = Path(__file__).parent / "templates"

    def write(self, run: BenchmarkRun) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"{run.run_id}.html"
        mode = self._mode or run.config.mode
        if mode == CompareMode.CLI_COMPARE:
            context = self._build_cli_context(run)
            template = "report_cli_compare.html"
        else:
            context = self._build_model_context(run)
            template = "report_model_compare.html"
        html = self._render(template, context)
        out_path.write_text(html, encoding="utf-8")
        return out_path

    # ------------------------------------------------------------------
    # cli-compare context
    # ------------------------------------------------------------------

    def _build_cli_context(self, run: BenchmarkRun) -> dict:
        by_target = run.results_by_target()
        cost_stats = run.cost_stats_by_target()
        has_cost = any(s["has_cost"] for s in cost_stats.values())

        target_stats = []
        for target, results in by_target.items():
            eligible = [r for r in results if r.status != TaskStatus.UNAVAILABLE]
            denom = len(eligible)
            passed = sum(1 for r in eligible if r.status == TaskStatus.PASSED)
            avg_func = sum(r.functional_score for r in eligible) / denom if denom else 0.0
            cs = cost_stats.get(target, {})
            cpp = cs.get("cost_per_pass")
            target_stats.append({
                "target": target,
                "total": denom,
                "passed": passed,
                "pass_rate": passed / denom if denom else 0,
                "avg_functional": avg_func,
                "avg_cost": cs.get("avg_cost_usd", 0.0),
                "cost_per_pass": None if cpp is None or cpp == float("inf") else cpp,
                "avg_latency": cs.get("avg_latency_seconds", 0.0),
                "total_cost": cs.get("total_cost_usd", 0.0),
                "avg_credits": cs.get("avg_credits", 0.0),
                "has_credits": bool(cs.get("has_credits", 0.0)),
                "total_input_tokens": int(cs.get("total_input_tokens", 0)),
                "total_output_tokens": int(cs.get("total_output_tokens", 0)),
                "has_cost": bool(cs.get("has_cost", 0.0)),
            })

        cost_rows = sorted(
            target_stats, key=lambda s: (s["cost_per_pass"] is None, s["cost_per_pass"] or 0.0)
        )
        labels = [s["target"] for s in cost_rows]

        pass_at_k_rows = self._pass_at_k_rows(run)
        column_order = self._target_column_order(run)
        result_rows = self._cli_result_rows(run, column_order)
        task_matrix = self._task_matrix(result_rows, column_order)

        task_labels = sorted({r.task_id for r in run.results})
        target_order = [s["target"] for s in cost_rows]

        def _avg(values):
            vals = [v for v in values if v is not None]
            return sum(vals) / len(vals) if vals else None

        per_task_cost, per_task_latency = [], []
        for target in target_order:
            cost_series, lat_series = [], []
            for task_id in task_labels:
                runs = [r for r in run.results if r.task_id == task_id and r.target == target]
                cost_avg = _avg([r.cost_usd for r in runs])
                lat_avg = _avg([r.latency_seconds for r in runs])
                cost_series.append(round(cost_avg, 5) if cost_avg is not None else None)
                lat_series.append(round(lat_avg, 1) if lat_avg is not None else None)
            per_task_cost.append({"label": target, "data": cost_series})
            per_task_latency.append({"label": target, "data": lat_series})

        return {
            "title": self.title,
            "run_id": run.run_id,
            "comparison_label": run.config.comparison_label or "(per-target models)",
            "started_at": run.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "duration": f"{run.duration_seconds:.1f}s",
            "is_harbor": any(r.is_harbor for r in run.results),
            "total_runs": run.total_runs,
            "passed_runs": run.passed_runs,
            "failed_runs": run.failed_runs,
            "unavailable_runs": run.unavailable_runs,
            "pass_rate": f"{run.pass_rate:.0%}",
            "total_cost": f"{run.total_cost_usd:.4f}",
            "unknown_cost_runs": run.unknown_cost_runs,
            "any_cost_recorded": any(r.cost_usd is not None for r in run.results),
            "targets": [t.label for t in run.config.targets],
            "target_stats": target_stats,
            "has_cost": has_cost,
            "repeats": run.repeats,
            "pass_at_k_rows": pass_at_k_rows,
            "result_rows": result_rows,
            "task_matrix": task_matrix,
            "matrix_targets": column_order,
            "chart_labels": json.dumps(labels),
            "chart_avg_cost": json.dumps([
                round(s["avg_cost"], 5) if s["has_cost"] else None for s in cost_rows
            ]),
            "chart_cost_per_pass": json.dumps(
                [round(s["cost_per_pass"], 5) if s["cost_per_pass"] is not None else None for s in cost_rows]
            ),
            "chart_pass_rate": json.dumps([round(s["pass_rate"] * 100, 1) for s in cost_rows]),
            "chart_latency": json.dumps([
                round(s["avg_latency"], 1) if s["avg_latency"] is not None else None
                for s in cost_rows
            ]),
            "chart_task_labels": json.dumps(task_labels),
            "chart_task_cost": json.dumps(per_task_cost),
            "chart_task_latency": json.dumps(per_task_latency),
        }

    @staticmethod
    def _target_column_order(run: BenchmarkRun) -> list[str]:
        """CLI labels in config order, then any others found in the results."""
        order = [t.label for t in run.config.targets]
        for r in run.results:
            if r.target not in order:
                order.append(r.target)
        present = {r.target for r in run.results}
        return [t for t in order if t in present]

    @staticmethod
    def _checks_label(fr) -> str:
        """Short check count for a run, e.g. "11/13", or "" when unknown."""
        if fr is None:
            return ""
        tests = (fr.checkpoints or {}).get("tests")
        if not isinstance(tests, dict):
            return ""
        detail = str(tests.get("detail") or "")
        m = re.match(r"(\d+)/(\d+) checks", detail)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        total, passed = tests.get("total"), tests.get("passed")
        # 1000 is the reward-file parser's fixed scale for a graduated reward,
        # not a real number of tests.
        if isinstance(total, int) and isinstance(passed, int) and total and total != 1000:
            return f"{passed}/{total}"
        return ""

    _STATUS_SYMBOL = {
        "pass": "✓", "fail": "✗", "error": "!", "timeout": "⏱",
        "unavailable": "–", "skip": "–",
    }

    @staticmethod
    def _shared_prefix(names: list[str]) -> str:
        """The prefix every task id shares, cut at a '-' boundary (e.g.
        "terminal-bench-"), so the grid can show short task names."""
        if len(names) < 2:
            return ""
        prefix = names[0]
        for n in names[1:]:
            while not n.startswith(prefix):
                prefix = prefix[:-1]
        cut = prefix.rfind("-")
        return prefix[: cut + 1] if cut > 0 else ""

    def _task_matrix(self, rows: list[dict], targets: list[str]) -> dict:
        """The per-task grid: one row per task with a cell per CLI, so a task's
        results can be compared side by side, plus a per-CLI total row.
        Repeats of the same task × CLI share a cell."""
        by_task: dict[str, dict[str, list[dict]]] = {}
        for row in rows:
            by_task.setdefault(row["task_id"], {}).setdefault(row["target"], []).append(row)
        prefix = self._shared_prefix(sorted(by_task))
        totals = {t: {"passed": 0, "scored": 0, "cost": 0.0, "has_cost": False} for t in targets}

        matrix = []
        for task_id in sorted(by_task):
            cells, passed_by, counted = [], [], []
            for target in targets:
                runs = by_task[task_id].get(target, [])
                if not runs:
                    cells.append(None)
                    continue
                n_pass = sum(1 for r in runs if r["status"] == TaskStatus.PASSED.value)
                scored = [r for r in runs if r["status"] != TaskStatus.UNAVAILABLE.value]
                if scored:
                    counted.append(target)
                    if n_pass:
                        passed_by.append(target)
                first = runs[0]
                costs = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
                latencies = [r["cli_time"] for r in runs if r["cli_time"] is not None]
                tot = totals[target]
                tot["scored"] += len(scored)
                tot["passed"] += n_pass
                tot["cost"] += sum(costs)
                tot["has_cost"] = tot["has_cost"] or bool(costs)
                if len(runs) == 1:
                    status, status_class = first["status"], first["status_class"]
                    label = first["checks"] or status
                else:
                    status = f"{n_pass}/{len(runs)} passed"
                    status_class = (
                        "pass" if n_pass == len(runs) else "fail" if n_pass == 0 else "timeout"
                    )
                    label = f"{n_pass}/{len(runs)}"
                cells.append({
                    "status": status,
                    "status_class": status_class,
                    "symbol": self._STATUS_SYMBOL.get(status_class, ""),
                    "label": label,
                    "checks": first["checks"] if len(runs) == 1 else "",
                    "cost_usd": sum(costs) / len(costs) if costs else None,
                    "cli_time": sum(latencies) / len(latencies) if latencies else None,
                    "anchor": first["anchor"],
                    "title": f"{target}: {status}"
                             + (f", {first['checks']} checks" if len(runs) == 1 and first["checks"]
                                else ""),
                })
            n_counted, n_passed = len(counted), len(passed_by)
            if not counted:
                outcome, outcome_class = "no scored runs", "unavailable"
            elif n_passed == n_counted:
                outcome, outcome_class = f"{n_passed}/{n_counted} passed", "pass"
            elif not passed_by:
                outcome, outcome_class = f"0/{n_counted} passed", "fail"
            else:
                outcome, outcome_class = f"{n_passed}/{n_counted} passed", "timeout"
            matrix.append({
                "task_id": task_id,
                "short_name": task_id[len(prefix):] if prefix else task_id,
                "cells": cells,
                "outcome": outcome,
                "outcome_class": outcome_class,
                "outcome_title": (
                    "Passed: " + ", ".join(passed_by) if passed_by else "No CLI passed"
                ),
            })
        footer = [
            {
                "passed": totals[t]["passed"],
                "scored": totals[t]["scored"],
                "cost": totals[t]["cost"] if totals[t]["has_cost"] else None,
            }
            for t in targets
        ]
        return {"rows": matrix, "prefix": prefix, "footer": footer}

    def _cli_result_rows(self, run: BenchmarkRun, targets: list[str] | None = None) -> list[dict]:
        """Detailed rows sorted by task, then CLI (config order), then repeat,
        so every run of one task sits together. Each row gets an anchor the
        per-task grid links to."""
        order = {t: i for i, t in enumerate(targets or [])}
        ordered = sorted(
            run.results,
            key=lambda r: (r.task_id, order.get(r.target, len(order)), r.target, r.repeat),
        )
        rows = []
        prev_task = None
        for i, r in enumerate(ordered):
            rows.append({
                "anchor": f"run-{i}",
                "first_of_task": r.task_id != prev_task,
                "checks": self._checks_label(r.functional_result),
                "task_id": r.task_id,
                "target": r.target,
                "status": r.status.value,
                "status_class": self._status_class(r.status),
                "functional_score": r.functional_score,
                "cost_usd": r.cost_usd,
                "credits": r.native_credits,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cli_time": r.latency_seconds,
                "wall_time": r.duration_seconds,
                "repeat": r.repeat,
                "error_message": r.error_message or "",
                "functional_summary": r.functional_result.summary if r.functional_result else "",
                "functional_checkpoints": r.functional_result.checkpoints if r.functional_result else {},
                "functional_stdout": (
                    _tail(r.functional_result.stdout, 1500) if r.functional_result else ""
                ),
                "functional_stderr": (
                    _tail(r.functional_result.stderr, 1500) if r.functional_result else ""
                ),
                "agent_stdout": _clip_transcript(r.agent_stdout),
                "agent_stderr": _clip_transcript(r.agent_stderr),
                "workspace_path": r.workspace_path or "",
            })
            prev_task = r.task_id
        return rows

    # ------------------------------------------------------------------
    # model-compare context
    # ------------------------------------------------------------------

    def _build_model_context(self, run: BenchmarkRun) -> dict:
        by_target = run.results_by_target()
        by_mode = run.results_by_mode()
        cost_stats = run.cost_stats_by_target()
        has_credit_data = any(s["total_credits"] for s in cost_stats.values())

        model_stats = []
        for target, results in by_target.items():
            eligible = [r for r in results if r.status != TaskStatus.UNAVAILABLE]
            unavailable = len(results) - len(eligible)
            denom = len(eligible)
            passed = sum(1 for r in eligible if r.status == TaskStatus.PASSED)
            avg_final = sum(r.final_score for r in eligible) / denom if denom else 0.0
            avg_func = sum(r.functional_score for r in eligible) / denom if denom else 0.0
            spec_results = [r for r in eligible if r.mode == TaskMode.SPEC_DRIVEN]
            avg_spec = (
                sum(r.spec_artifact_score for r in spec_results) / len(spec_results)
                if spec_results else None
            )
            avg_tc = (
                sum(r.task_completion_rate for r in spec_results) / len(spec_results)
                if spec_results else None
            )
            cs = cost_stats.get(target, {})
            cpp = cs.get("credits_per_pass")
            cpp_usd = cs.get("cost_per_pass")
            model_stats.append({
                "model": target,
                "total": denom,
                "unavailable": unavailable,
                "passed": passed,
                "pass_rate": passed / denom if denom else 0,
                "avg_final": avg_final,
                "avg_functional": avg_func,
                "avg_spec_quality": avg_spec,
                "avg_task_completion": avg_tc,
                "avg_credits": cs.get("avg_credits", 0.0),
                "avg_cost_usd": cs.get("avg_cost_usd", 0.0),
                "credits_per_pass": None if cpp is None or cpp == float("inf") else cpp,
                "cost_per_pass_usd": None if cpp_usd is None or cpp_usd == float("inf") else cpp_usd,
                "avg_latency": cs.get("avg_latency_seconds", 0.0),
                "has_cost": bool(cs.get("has_cost", 0.0)),
            })

        mode_stats = []
        for mode, results in by_mode.items():
            eligible = [r for r in results if r.status != TaskStatus.UNAVAILABLE]
            denom = len(eligible)
            passed = sum(1 for r in eligible if r.status == TaskStatus.PASSED)
            mode_stats.append({
                "mode": mode,
                "total": denom,
                "passed": passed,
                "pass_rate": passed / denom if denom else 0,
                "avg_score": sum(r.final_score for r in eligible) / denom if denom else 0,
            })

        pass_at_k_rows = self._pass_at_k_rows(run)
        result_rows = self._model_result_rows(run)

        chart_labels = [s["model"] for s in model_stats]
        return {
            "title": self.title,
            "run_id": run.run_id,
            "started_at": run.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "duration": f"{run.duration_seconds:.1f}s",
            "total_tasks": run.total_runs,
            "passed_tasks": run.passed_runs,
            "failed_tasks": run.failed_runs,
            "unavailable_tasks": run.unavailable_runs,
            "pass_rate": f"{run.pass_rate:.0%}",
            "total_credits": f"{run.total_credits:.3f}",
            "total_cost": f"{run.total_cost_usd:.4f}",
            "models": [t.label for t in run.config.targets],
            "model_stats": model_stats,
            "mode_stats": mode_stats,
            "has_credit_data": has_credit_data,
            "repeats": run.repeats,
            "pass_at_k_rows": pass_at_k_rows,
            "result_rows": result_rows,
            "chart_labels": json.dumps(chart_labels),
            "chart_pass_rates": json.dumps([round(s["pass_rate"] * 100, 1) for s in model_stats]),
            "chart_avg_scores": json.dumps([round(s["avg_final"] * 100, 1) for s in model_stats]),
            "chart_functional": json.dumps([round(s["avg_functional"] * 100, 1) for s in model_stats]),
            "chart_credits": json.dumps([round(s["avg_credits"], 3) for s in model_stats]),
            "chart_cost": json.dumps([round(s["avg_cost_usd"], 5) for s in model_stats]),
            "kas_proxy_metrics": run.config.kas_proxy_metrics,
        }

    def _model_result_rows(self, run: BenchmarkRun) -> list[dict]:
        rows = []
        for r in run.results:
            phases = [
                {
                    "phase": p.phase,
                    "success": p.success,
                    "duration": f"{p.duration_seconds:.1f}s",
                    "cli_time": f"{p.cli_reported_seconds:.0f}s" if p.cli_reported_seconds else "—",
                    "error": p.error or "",
                    "retries": p.transient_retries,
                }
                for p in r.phase_results
            ]
            rows.append({
                "task_id": r.task_id,
                "model": r.target,
                "mode": r.mode.value,
                "status": r.status.value,
                "status_class": self._status_class(r.status),
                "functional_score": r.functional_score,
                "spec_artifact_score": r.spec_artifact_score if r.mode == TaskMode.SPEC_DRIVEN else None,
                "task_completion_rate": r.task_completion_rate if r.mode == TaskMode.SPEC_DRIVEN else None,
                "steering_score": r.steering_adherence_score,
                "final_score": r.final_score,
                "credits": r.total_credits,
                "cost_usd": r.cost_usd,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cached_input_tokens": r.cached_input_tokens,
                "cli_time": r.cli_reported_seconds or r.duration_seconds,
                "repeat": r.repeat,
                "retries": r.transient_retries,
                "phases": phases,
                "error_message": r.error_message or "",
                "functional_summary": r.functional_result.summary if r.functional_result else "",
                "functional_checkpoints": r.functional_result.checkpoints if r.functional_result else {},
                "functional_stdout": (
                    _tail(r.functional_result.stdout, 1000) if r.functional_result else ""
                ),
                "functional_stderr": (
                    _tail(r.functional_result.stderr, 1000) if r.functional_result else ""
                ),
                "spec_details": json.dumps(
                    r.spec_artifact_scores.details if r.spec_artifact_scores else {}, indent=2
                ),
                "workspace_path": r.workspace_path or "",
            })
        return rows

    # ------------------------------------------------------------------

    def _pass_at_k_rows(self, run: BenchmarkRun) -> list[dict]:
        if run.repeats <= 1:
            return []
        rows = []
        for target, s in sorted(
            run.repeat_stats_by_target().items(), key=lambda kv: kv[1].pass_at_1, reverse=True
        ):
            rows.append({
                "target": target,
                "instances": s.instances,
                "pass_at_1": s.pass_at_1,
                "pass_at_k": s.pass_at_k_unbiased,
                "pass_all_k": s.pass_all_k,
                "stddev": s.pass_rate_stddev,
                "ci_low": s.ci_low,
                "ci_high": s.ci_high,
            })
        return rows

    def _render(self, template: str, context: dict) -> str:
        env = Environment(
            loader=FileSystemLoader(str(self._template_dir)),
            autoescape=select_autoescape(["html"]),
        )
        # Inline Chart.js so reports are self-contained (no external CDN).
        chartjs_path = self._template_dir / "chart.umd.min.js"
        if chartjs_path.exists():
            context["chartjs_inline"] = chartjs_path.read_text(encoding="utf-8")
        else:
            context["chartjs_inline"] = ""
        return env.get_template(template).render(**context)

    @staticmethod
    def _status_class(status: TaskStatus) -> str:
        return {
            TaskStatus.PASSED: "pass",
            TaskStatus.FAILED: "fail",
            TaskStatus.ERROR: "error",
            TaskStatus.TIMEOUT: "timeout",
            TaskStatus.SKIPPED: "skip",
            TaskStatus.UNAVAILABLE: "unavailable",
        }.get(status, "pending")
