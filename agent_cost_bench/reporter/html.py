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
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..models import BenchmarkRun, CompareMode, TaskMode, TaskStatus, _clip_transcript


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
        result_rows = self._cli_result_rows(run)

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
                lat_avg = _avg([(r.cli_reported_seconds or r.duration_seconds) for r in runs])
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
            "total_runs": run.total_runs,
            "passed_runs": run.passed_runs,
            "failed_runs": run.failed_runs,
            "unavailable_runs": run.unavailable_runs,
            "pass_rate": f"{run.pass_rate:.0%}",
            "total_cost": f"{run.total_cost_usd:.4f}",
            "targets": [t.label for t in run.config.targets],
            "target_stats": target_stats,
            "has_cost": has_cost,
            "repeats": run.repeats,
            "pass_at_k_rows": pass_at_k_rows,
            "result_rows": result_rows,
            "chart_labels": json.dumps(labels),
            "chart_avg_cost": json.dumps([round(s["avg_cost"], 5) for s in cost_rows]),
            "chart_cost_per_pass": json.dumps(
                [round(s["cost_per_pass"], 5) if s["cost_per_pass"] is not None else None for s in cost_rows]
            ),
            "chart_pass_rate": json.dumps([round(s["pass_rate"] * 100, 1) for s in cost_rows]),
            "chart_latency": json.dumps([round(s["avg_latency"], 1) for s in cost_rows]),
            "chart_task_labels": json.dumps(task_labels),
            "chart_task_cost": json.dumps(per_task_cost),
            "chart_task_latency": json.dumps(per_task_latency),
        }

    def _cli_result_rows(self, run: BenchmarkRun) -> list[dict]:
        rows = []
        for r in run.results:
            rows.append({
                "task_id": r.task_id,
                "target": r.target,
                "status": r.status.value,
                "status_class": self._status_class(r.status),
                "functional_score": r.functional_score,
                "cost_usd": r.cost_usd,
                "credits": r.native_credits,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cli_time": r.cli_reported_seconds or r.duration_seconds,
                "repeat": r.repeat,
                "error_message": r.error_message or "",
                "functional_summary": r.functional_result.summary if r.functional_result else "",
                "functional_checkpoints": r.functional_result.checkpoints if r.functional_result else {},
                "functional_stdout": (r.functional_result.stdout[:1500] if r.functional_result else ""),
                "functional_stderr": (r.functional_result.stderr[:1500] if r.functional_result else ""),
                "agent_stdout": _clip_transcript(r.agent_stdout),
                "agent_stderr": _clip_transcript(r.agent_stderr),
                "workspace_path": r.workspace_path or "",
            })
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
                "functional_stdout": (r.functional_result.stdout[:1000] if r.functional_result else ""),
                "functional_stderr": (r.functional_result.stderr[:1000] if r.functional_result else ""),
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
