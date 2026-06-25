"""Task 7 — runner orchestration end-to-end (via MockCLI)."""

from __future__ import annotations

import pytest

from agent_cost_bench.models import BenchConfig, CompareMode, CostSource, TaskStatus
from agent_cost_bench.runner import BenchmarkRunner
from tests.conftest import mock_target


def _tasks_tree(root, verify_body: str, mode: str = "vibe"):
    td = root / "tasks" / mode / "t1"
    td.mkdir(parents=True)
    (td / "task.yaml").write_text(
        f"id: t1\nmode: {mode}\ndescription: d\ntimeout_minutes: 1\nprompt: do it\n"
    )
    v = td / "verify.sh"
    v.write_text("#!/bin/bash\n" + verify_body)
    v.chmod(0o755)
    return str(root / "tasks")


_PASS_VERIFY = (
    'if [ -f "$WORKSPACE/solution.py" ]; then\n'
    '  echo \'AGENT_COST_BENCH_RESULT: {"score": 1.0, "summary": "ok"}\'\n'
    "  exit 0\nelse\n"
    '  echo \'AGENT_COST_BENCH_RESULT: {"score": 0.0, "summary": "missing"}\'\n'
    "  exit 1\nfi\n"
)


def _cfg(tmp_path, targets, tasks_dir, **kw):
    return BenchConfig(
        mode=CompareMode.MODEL_COMPARE,
        targets=targets,
        tasks_dir=tasks_dir,
        workspace_base=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "results"),
        parallel_workers=2,
        timeout_minutes=1,
        repeats=kw.get("repeats", 1),
        open_report=False,
    )


@pytest.mark.asyncio
async def test_end_to_end_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.05")
    tasks_dir = _tasks_tree(tmp_path, _PASS_VERIFY)
    cfg = _cfg(tmp_path, [mock_target()], tasks_dir)
    run = await BenchmarkRunner(cfg).run()
    assert run.total_runs == 1
    assert run.results[0].status == TaskStatus.PASSED
    assert run.results[0].cost_usd is not None
    assert run.results[0].native_credits is not None


@pytest.mark.asyncio
async def test_noop_classified_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_NOOP", "1")  # no file, no credits, exit 0
    verify = (
        'echo \'AGENT_COST_BENCH_RESULT: {"score": 0.0, "checkpoints": '
        '{"code_exists": {"passed": false}}, "summary": "nothing"}\'\n'
        "exit 1\n"
    )
    tasks_dir = _tasks_tree(tmp_path, verify)
    cfg = _cfg(tmp_path, [mock_target()], tasks_dir)
    run = await BenchmarkRunner(cfg).run()
    assert run.results[0].status == TaskStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_unavailable_on_model_banner(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_UNAVAILABLE", "1")
    tasks_dir = _tasks_tree(tmp_path, _PASS_VERIFY)
    cfg = _cfg(tmp_path, [mock_target()], tasks_dir)
    run = await BenchmarkRunner(cfg).run()
    assert run.results[0].status == TaskStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_gating_fail_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_WRITE_FILE", "wrongname.py")  # verify wants solution.py
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    tasks_dir = _tasks_tree(tmp_path, _PASS_VERIFY)
    cfg = _cfg(tmp_path, [mock_target()], tasks_dir)
    run = await BenchmarkRunner(cfg).run()
    assert run.results[0].status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_repeats_and_aggregation(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.05")
    tasks_dir = _tasks_tree(tmp_path, _PASS_VERIFY)
    cfg = _cfg(tmp_path, [mock_target()], tasks_dir, repeats=3)
    run = await BenchmarkRunner(cfg).run()
    assert run.total_runs == 3
    stats = run.cost_stats_by_target()
    assert stats[run.results[0].target]["runs"] == 3.0
