"""Centralized local verify runner (ScriptVerifyRunner) tests.

The venv build is mocked to the current interpreter so these stay fast and need
no pip/network — they exercise the run + marker-parse + env-wiring + routing.
"""

from __future__ import annotations

import sys

import pytest

from agent_cost_bench.evaluator.script_runner import ScriptVerifyRunner
from agent_cost_bench.models import FunctionalTestResult, TaskConfig, TaskMode, VerifySpec


def _local_task(tmp_path, score_body: str, *, score_rel: str = "verify/score.py",
                deps=None, create: bool = True) -> TaskConfig:
    task_dir = tmp_path / "task"
    (task_dir / "verify").mkdir(parents=True, exist_ok=True)
    if create:
        (task_dir / score_rel).write_text(score_body, encoding="utf-8")
    tc = TaskConfig(id="t-local", mode=TaskMode.VIBE, description="d", timeout_minutes=1)
    tc.verify = VerifySpec(runner="local", score=score_rel, deps=deps or [])
    tc.task_dir = task_dir
    return tc


def _skip_venv(runner: ScriptVerifyRunner, monkeypatch) -> None:
    """Make _ensure_venv a no-op that returns the current interpreter."""
    async def _fake(deps):
        return sys.executable

    monkeypatch.setattr(runner, "_ensure_venv", _fake)


@pytest.mark.asyncio
async def test_local_runner_parses_graduated_marker(tmp_path, monkeypatch):
    score = (
        "import json\n"
        "print('AGENT_COST_BENCH_RESULT: ' + json.dumps({'score': 0.5, 'summary': 'half'}))\n"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = ScriptVerifyRunner(_local_task(tmp_path, score), ws)
    _skip_venv(runner, monkeypatch)

    res = await runner.run()
    assert isinstance(res, FunctionalTestResult)
    assert abs(res.score - 0.5) < 1e-9
    assert res.passed is False          # graduated < 1.0 does not pass the hard gate
    assert res.summary == "half"


@pytest.mark.asyncio
async def test_local_runner_full_pass(tmp_path, monkeypatch):
    score = (
        "import json\n"
        "print('AGENT_COST_BENCH_RESULT: ' + json.dumps({'score': 1.0, 'summary': 'ok'}))\n"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = ScriptVerifyRunner(_local_task(tmp_path, score), ws)
    _skip_venv(runner, monkeypatch)

    res = await runner.run()
    assert res.score == 1.0 and res.passed is True


@pytest.mark.asyncio
async def test_local_runner_passes_workspace_and_task_dir(tmp_path, monkeypatch):
    # Scorer scores 1.0 only if argv[1] (workspace) has solution.py AND argv[2]
    # (task_dir) exists — proving both args + the env are wired correctly.
    score = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "ws = Path(sys.argv[1]); td = Path(sys.argv[2])\n"
        "ok = (ws / 'solution.py').exists() and td.is_dir() and 'WORKSPACE' in __import__('os').environ\n"
        "print('AGENT_COST_BENCH_RESULT: ' + json.dumps({'score': 1.0 if ok else 0.0, 'summary': str(ok)}))\n"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "solution.py").write_text("x = 1\n")
    runner = ScriptVerifyRunner(_local_task(tmp_path, score), ws)
    _skip_venv(runner, monkeypatch)

    res = await runner.run()
    assert res.score == 1.0


@pytest.mark.asyncio
async def test_local_runner_missing_score_script_is_harness_error(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    # Declare a score path but don't create the file.
    task = _local_task(tmp_path, "", score_rel="verify/score.py", create=False)
    runner = ScriptVerifyRunner(task, ws)
    _skip_venv(runner, monkeypatch)

    res = await runner.run()
    assert res.checkpoints.get("harness_error") is True
    assert res.score == 0.0


@pytest.mark.asyncio
async def test_local_runner_venv_failure_is_harness_error(tmp_path, monkeypatch):
    score = "print('AGENT_COST_BENCH_RESULT: {\"score\": 1.0}')\n"
    ws = tmp_path / "ws"
    ws.mkdir()
    runner = ScriptVerifyRunner(_local_task(tmp_path, score), ws)

    async def _fail(deps):
        return None  # venv could not be created

    monkeypatch.setattr(runner, "_ensure_venv", _fail)

    res = await runner.run()
    assert res.checkpoints.get("harness_error") is True


@pytest.mark.asyncio
async def test_functional_evaluator_routes_to_script_runner(tmp_path, monkeypatch):
    import agent_cost_bench.evaluator.script_runner as sr_mod
    from agent_cost_bench.evaluator.functional import FunctionalEvaluator

    sentinel = FunctionalTestResult(passed=True, score=1.0, summary="from local runner")

    class FakeRunner:
        def __init__(self, task, workspace, logger=None):
            pass

        async def run(self):
            return sentinel

    monkeypatch.setattr(sr_mod, "ScriptVerifyRunner", FakeRunner)

    tc = TaskConfig(id="t", mode=TaskMode.VIBE, description="d")
    tc.verify = VerifySpec(runner="local", score="verify/score.py")
    tc.task_dir = tmp_path

    res = await FunctionalEvaluator(tc, tmp_path).evaluate()
    assert res is sentinel


@pytest.mark.asyncio
async def test_functional_evaluator_routes_to_pytest_runner(tmp_path, monkeypatch):
    """runner:pytest routes to PytestSuiteRunner (with extra_deps from verify.deps)."""
    import agent_cost_bench.evaluator.pytest_runner as pr_mod
    from agent_cost_bench.evaluator.functional import FunctionalEvaluator

    sentinel = FunctionalTestResult(passed=True, score=1.0, summary="from pytest runner")
    captured: dict = {}

    class FakeRunner:
        def __init__(self, task, workspace, verify_dir, extra_deps=None, logger=None):
            captured["extra_deps"] = extra_deps

        async def run(self):
            return sentinel

    monkeypatch.setattr(pr_mod, "PytestSuiteRunner", FakeRunner)

    # Create a test_*.py so _find_pytest_dir() returns a path.
    verify_dir = tmp_path / "verify"
    verify_dir.mkdir()
    (verify_dir / "test_stub.py").write_text("def test_ok(): pass\n")

    tc = TaskConfig(id="t", mode=TaskMode.VIBE, description="d")
    tc.verify = VerifySpec(runner="pytest", deps=["pyyaml==6.0.2"])
    tc.task_dir = tmp_path

    res = await FunctionalEvaluator(tc, tmp_path).evaluate()
    assert res is sentinel
    assert captured["extra_deps"] == ["pyyaml==6.0.2"]


@pytest.mark.asyncio
async def test_pytest_runner_missing_tests_is_harness_error(tmp_path):
    """runner:pytest with no test_*.py in verify/ produces a harness_error."""
    from agent_cost_bench.evaluator.functional import FunctionalEvaluator

    tc = TaskConfig(id="t", mode=TaskMode.VIBE, description="d")
    tc.verify = VerifySpec(runner="pytest", deps=[])
    tc.task_dir = tmp_path
    # No test files created.

    res = await FunctionalEvaluator(tc, tmp_path).evaluate()
    assert res.checkpoints.get("harness_error") is True
