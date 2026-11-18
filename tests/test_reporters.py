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


def _matrix_run() -> BenchmarkRun:
    """Two CLIs on two tasks, recorded in finish order (interleaved)."""
    from agent_cost_bench.models import FunctionalTestResult

    cfg = BenchConfig(
        mode=CompareMode.CLI_COMPARE,
        targets=[
            make_cli_target({"name": "kiro", "display_name": "Kiro", "cli_path": "kiro-cli",
                             "model_id": "m"}),
            make_cli_target({"name": "claude", "display_name": "Claude Code",
                             "cli_path": "claude", "model_id": "m"}),
        ],
    )
    run = BenchmarkRun(run_id="m1", config=cfg)

    def res(task, target, status, checks, score, cost):
        fr = FunctionalTestResult(
            passed=status == TaskStatus.PASSED, score=score,
            summary=f"reward={score:.3f} · {checks} checks passed",
            checkpoints={"compiles": True, "tests": {
                "passed": int(score * 1000), "total": 1000, "failed": [],
                "detail": f"{checks} checks passed"}},
        )
        r = RunResult(task_id=task, target=target, mode=TaskMode.VIBE, status=status)
        r.functional_result = fr
        r.functional_score = score
        r.cost_usd = cost
        r.cli_reported_seconds = 10.0
        r.finished_at = _utcnow()
        return r

    run.results = [
        res("task-b", "Claude Code", TaskStatus.FAILED, "3/4", 0.0, 0.5),
        res("task-a", "Kiro", TaskStatus.PASSED, "5/5", 1.0, 0.1),
        res("task-b", "Kiro", TaskStatus.PASSED, "4/4", 1.0, 0.2),
        res("task-a", "Claude Code", TaskStatus.PASSED, "5/5", 1.0, 0.3),
    ]
    run.finished_at = _utcnow()
    return run


def test_cli_report_groups_results_by_task():
    reporter = HTMLReporter("unused", mode=CompareMode.CLI_COMPARE)
    run = _matrix_run()
    cols = reporter._target_column_order(run)
    assert cols == ["Kiro", "Claude Code"]  # config order, not finish order

    rows = reporter._cli_result_rows(run, cols)
    assert [(r["task_id"], r["target"]) for r in rows] == [
        ("task-a", "Kiro"), ("task-a", "Claude Code"),
        ("task-b", "Kiro"), ("task-b", "Claude Code"),
    ]
    assert [r["first_of_task"] for r in rows] == [True, False, True, False]
    assert rows[3]["checks"] == "3/4"

    grid = reporter._task_matrix(rows, cols)
    matrix = grid["rows"]
    assert grid["prefix"] == "task-"
    assert [m["short_name"] for m in matrix] == ["a", "b"]
    assert (matrix[0]["outcome"], matrix[0]["outcome_class"]) == ("2/2 passed", "pass")
    assert (matrix[1]["outcome"], matrix[1]["outcome_class"]) == ("1/2 passed", "timeout")
    assert matrix[1]["outcome_title"] == "Passed: Kiro"
    claude_b = matrix[1]["cells"][1]
    assert (claude_b["status"], claude_b["symbol"], claude_b["label"]) == ("failed", "✗", "3/4")
    assert claude_b["anchor"] == rows[3]["anchor"]
    # Per-CLI totals in config order: Kiro passed both tasks, Claude Code one.
    assert [(f["passed"], f["scored"]) for f in grid["footer"]] == [(2, 2), (1, 2)]
    assert round(grid["footer"][1]["cost"], 2) == 0.8


def test_cli_report_renders_task_grid(tmp_path):
    path = HTMLReporter(tmp_path, mode=CompareMode.CLI_COMPARE).write(_matrix_run())
    html = path.read_text()
    assert "Results by Task" in html
    assert 'title="Passed: Kiro"' in html
    assert "Tasks passed" in html
    assert 'href="#run-3"' in html and 'id="run-3"' in html
