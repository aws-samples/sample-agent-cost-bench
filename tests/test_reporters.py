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


# ---------------------------------------------------------------------------
# usage_events back-compat (issue #21)
# ---------------------------------------------------------------------------

# Every key results[].usage carried before usage_events existed, and its type
# for a populated pi_json result. 4.1: none of these may change.
_PRE_CHANGE_USAGE_TYPES = {
    "cost_usd": float, "input_tokens": int, "cached_input_tokens": int,
    "output_tokens": int, "reasoning_output_tokens": type(None),
    "cli_reported_seconds": float, "wall_clock_seconds": float,
    "raw_credits": type(None), "premium_requests": type(None),
    "total_credits": float, "native_credits": type(None), "cost_per_pass": float,
    "credits_per_pass": float,
}

# A results JSON as written by the code BEFORE this change: no usage_events.
_PRE_CHANGE_REPORT = {
    "run_id": "20260101_000000_pre", "mode": "cli-compare",
    "comparison_label": "pi before usage_events",
    "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:00+00:00",
    "summary": {},
    "results": [{
        "task_id": "task-001", "target": "pi", "repeat": 1, "mode": "vibe",
        "status": "passed", "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:30+00:00", "duration_seconds": 30.0,
        "scores": {"functional": 1.0, "spec_artifact": 0.0, "task_completion": 0.0,
                   "steering_adherence": 0.0, "final": 0.0},
        "usage": {"cost_usd": 0.028, "input_tokens": 13119, "cached_input_tokens": 13115,
                  "output_tokens": 86, "reasoning_output_tokens": None,
                  "cli_reported_seconds": 1.0, "wall_clock_seconds": 30.0,
                  "raw_credits": None, "premium_requests": None, "total_credits": 0.0,
                  "native_credits": None, "cost_per_pass": 0.028, "credits_per_pass": 0.0},
        "functional_result": {"passed": True, "exit_code": 0, "score": 1.0, "checkpoints": {},
                              "summary": "ok", "stdout": "", "stderr": ""},
        "spec_artifact_scores": None, "phase_results": [],
        "transcript": {"stdout": "", "stderr": ""},
        "error_message": None, "workspace_path": None,
    }],
}


def _pi_cli_run(with_events: bool):
    run = _cli_run()
    r = run.results[0]
    r.target = "pi"
    r.cost_usd = 0.028
    r.input_tokens, r.cached_input_tokens, r.output_tokens = 13119, 13115, 86
    r.raw_credits = None
    if with_events:
        r.usage_events = {
            "source": "pi_json", "cost_basis": "pi_reported", "completeness": "complete",
            "partial_reasons": [],
            "events": [{"seq": 0, "source": "turn_end", "provider": "amazon-bedrock",
                        "model": "m", "input": 4, "output": 86, "cache_read": 6512,
                        "cache_write": 6603, "cost_total": 0.028}],
        }
    return run


def _types(d):
    return {k: type(v) for k, v in d.items()}


def test_usage_events_compat_existing_keys_and_types_unchanged(tmp_path):
    before = json.loads(JSONReporter(tmp_path / "a").write(_pi_cli_run(False)).read_text())
    after = json.loads(JSONReporter(tmp_path / "b").write(_pi_cli_run(True)).read_text())
    r_after = after["results"][0]
    assert _types(r_after["usage"]) == _PRE_CHANGE_USAGE_TYPES
    assert _types(r_after["usage"]) == _types(before["results"][0]["usage"])
    assert set(after["summary"]) == set(before["summary"])
    for k in after["summary"]:
        assert type(after["summary"][k]) is type(before["summary"][k])
    assert set(r_after) - set(before["results"][0]) == {"usage_events"}
    assert r_after["usage_events"]["events"][0]["cache_write"] == 6603
    # The non-pi result (claude-code) carries no new key.
    assert "usage_events" not in after["results"][1]
    assert set(after["results"][1]) == set(before["results"][1])


def test_usage_events_compat_pre_change_report_loads_via_report_command(tmp_path):
    from click.testing import CliRunner

    from agent_cost_bench.cli import main

    for name, payload in (("pre.json", _PRE_CHANGE_REPORT), ("post.json", None)):
        if payload is None:
            src = JSONReporter(tmp_path / "w").write(_pi_cli_run(True))
            payload = json.loads(src.read_text())
            assert "usage_events" in payload["results"][0]
        path = tmp_path / name
        path.write_text(json.dumps(payload))
        out = tmp_path / f"out-{name}"
        result = CliRunner().invoke(main, ["report", str(path), "-o", str(out), "--no-open"])
        assert result.exit_code == 0, result.output
        assert any(p.suffix == ".html" for p in out.iterdir())
