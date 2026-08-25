"""
Shared JSON reporter: writes a machine-readable results file for both modes.

Cost is recorded both ways (USD + native units). The ``report`` command can
round-trip this file (or a ``.partial.json`` checkpoint) back into an HTML report.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..models import BenchmarkRun


def _cli_versions(run: BenchmarkRun) -> dict:
    """Version and resolved path of every CLI this run invoked.

    Not recorded until now, so "which version produced this number" could only be
    answered by grepping a version string out of the run log — and only when the CLI
    happened to print one. It matters: the same command name resolved to two different
    installs of Kiro CLI on this machine (a Builder Toolbox wrapper and a standalone
    app) that behave differently, and a stale symlink earlier in PATH is enough to
    switch between them silently.
    """
    out: dict = {}
    for target in run.config.targets:
        exe = getattr(target, "cli_path", None)
        if not exe or exe in out:
            continue
        resolved = shutil.which(exe)
        entry = {"resolved_path": resolved, "version": None}
        if resolved:
            try:
                p = subprocess.run([resolved, "--version"], capture_output=True,
                                   text=True, timeout=20)
                entry["version"] = ((p.stdout or p.stderr) or "").strip().splitlines()[:1]
                entry["version"] = entry["version"][0] if entry["version"] else None
            except Exception as e:            # noqa: BLE001 — provenance, never fatal
                entry["version"] = f"(could not read: {e})"
        out[exe] = entry
    return out


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
            "cli_versions": _cli_versions(run),
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
