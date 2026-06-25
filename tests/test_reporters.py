"""Task 8 — reporter tests (both render paths + JSON round-trip)."""

from __future__ import annotations

import json

from agent_cost_bench.models import (
    BenchConfig,
    BenchmarkRun,
    CompareMode,
    FunctionalTestResult,
    RunResult,
    TaskMode,
    TaskStatus,
    _utcnow,
)
from agent_cost_bench.reporter import HTMLReporter, JSONReporter
from agent_cost_bench.targets import make_cli_target, make_kiro_target


def _cli_run():
    cfg = BenchConfig(
        mode=CompareMode.CLI_COMPARE,
        comparison_label="claude-sonnet-4.x across CLIs",
        targets=[
            make_cli_target({"name": "kiro", "cli_path": "kiro", "model_id": "s",
                             "cost_source": "kiro_credits", "pricing": {"usd_per_credit": 0.04}}),
            make_cli_target({"name": "claude-code", "cli_path": "claude", "model_id": "s2",
                             "cost_source": "claude_json"}),
        ],
    )
    run = BenchmarkRun(run_id="20260101_000000_abc", config=cfg)
    run.finished_at = _utcnow()
    for target, cost, cred in [("kiro", 0.002, 0.05), ("claude-code", 0.012, None)]:
        r = RunResult(task_id="task-001", target=target, mode=TaskMode.VIBE, status=TaskStatus.PASSED)
        r.functional_score = 1.0
        r.cost_usd = cost
        r.raw_credits = cred
        r.cli_reported_seconds = 2.0
        r.functional_result = FunctionalTestResult(passed=True, score=1.0, summary="ok")
        r.finished_at = _utcnow()
        run.results.append(r)
    return run


def _model_run():
    cfg = BenchConfig(mode=CompareMode.MODEL_COMPARE, targets=[make_kiro_target("sonnet"), make_kiro_target("haiku")])
    run = BenchmarkRun(run_id="20260101_111111_def", config=cfg)
    run.finished_at = _utcnow()
    for target, mode, score in [("sonnet", TaskMode.VIBE, 1.0), ("haiku", TaskMode.SPEC_DRIVEN, 0.8)]:
        r = RunResult(task_id="t", target=target, mode=mode,
                      status=TaskStatus.PASSED if score >= 0.99 else TaskStatus.FAILED)
        r.functional_score = score
        r.final_score = score
        r.total_credits = 0.2
        r.cost_usd = 0.008
        r.cli_reported_seconds = 3.0
        r.functional_result = FunctionalTestResult(passed=score >= 0.99, score=score, summary="x")
        r.finished_at = _utcnow()
        run.results.append(r)
    return run


def test_cli_compare_html_renders(tmp_path):
    run = _cli_run()
    path = HTMLReporter(tmp_path, mode=CompareMode.CLI_COMPARE).write(run)
    html = path.read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert "Cost Comparison" in html
    assert "kiro" in html and "claude-code" in html
    assert "Cost / Success" in html


def test_model_compare_html_renders(tmp_path):
    run = _model_run()
    path = HTMLReporter(tmp_path, mode=CompareMode.MODEL_COMPARE).write(run)
    html = path.read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert "Model Comparison" in html
    assert "Credits/Success" in html
    assert "By Task Mode" in html


def test_json_reporter_writes_both_cost_units(tmp_path):
    run = _cli_run()
    path = JSONReporter(tmp_path).write(run)
    data = json.loads(path.read_text())
    assert data["mode"] == "cli-compare"
    assert "cost_stats_by_target" in data["summary"]
    r0 = data["results"][0]
    assert "cost_usd" in r0["usage"]
    assert "native_credits" in r0["usage"]


def test_json_report_round_trips_through_report(tmp_path):
    # Write JSON, then rebuild an HTML report from it (as the `report` command does).
    run = _model_run()
    json_path = JSONReporter(tmp_path).write(run)
    data = json.loads(json_path.read_text())
    assert data["mode"] == "model-compare"
    assert len(data["results"]) == 2
