"""
Shared JSON reporter: writes a machine-readable results file for both modes.

Cost is recorded both ways (USD + native units). The ``report`` command can
round-trip this file (or a ``.partial.json`` checkpoint) back into an HTML report.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import BenchmarkRun


class JSONReporter:
    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)

    def write(self, run: BenchmarkRun) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"{run.run_id}.json"

        cost_stats = {
            target: {
                k: (None if isinstance(v, float) and v == float("inf") else v)
                for k, v in s.items()
            }
            for target, s in run.cost_stats_by_target().items()
        }

        payload = {
            "run_id": run.run_id,
            "mode": run.config.mode.value,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "duration_seconds": run.duration_seconds,
            "comparison_label": run.config.comparison_label,
            "summary": {
                "total_runs": run.total_runs,
                "passed": run.passed_runs,
                "failed": run.failed_runs,
                "unavailable": run.unavailable_runs,
                "pass_rate": run.pass_rate,
                "total_cost_usd": run.total_cost_usd,
                "total_credits": run.total_credits,
                "avg_score_by_target": run.avg_score_by_target(),
                "cost_stats_by_target": cost_stats,
                "pass_at_k_by_target": {
                    target: s.to_dict() for target, s in run.repeat_stats_by_target().items()
                },
            },
            "config": {
                "mode": run.config.mode.value,
                "comparison_label": run.config.comparison_label,
                "targets": [
                    {
                        "name": t.name,
                        "label": t.label,
                        "model_id": t.model_id,
                        "cost_source": t.cost_source.value,
                        "supports_spec": t.capabilities.supports_spec,
                    }
                    for t in run.config.targets
                ],
                "tasks_dir": run.config.tasks_dir,
                "parallel_workers": run.config.effective_workers(),
                "repeats": run.config.repeats,
            },
            "results": [r.to_dict() for r in run.results],
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out_path
