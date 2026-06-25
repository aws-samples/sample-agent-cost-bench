"""Task 10 — full mock-CLI integration for both modes."""

from __future__ import annotations

import pytest

from agent_cost_bench.models import BenchConfig, CompareMode, CostSource, TaskStatus
from agent_cost_bench.reporter import HTMLReporter, JSONReporter
from agent_cost_bench.runner import BenchmarkRunner
from tests.conftest import mock_target


def _vibe_task(root):
    td = root / "tasks" / "vibe" / "t1"
    td.mkdir(parents=True)
    (td / "task.yaml").write_text("id: t1\nmode: vibe\ndescription: d\ntimeout_minutes: 1\nprompt: do it\n")
    v = td / "verify.sh"
    v.write_text(
        "#!/bin/bash\n"
        'if [ -f "$WORKSPACE/solution.py" ]; then echo \'AGENT_COST_BENCH_RESULT: {"score":1.0}\'; exit 0;'
        ' else echo \'AGENT_COST_BENCH_RESULT: {"score":0.0}\'; exit 1; fi\n'
    )
    v.chmod(0o755)


def _spec_task(root):
    td = root / "tasks" / "spec" / "s1"
    seed = td / "seed"
    seed.mkdir(parents=True)
    (td / "task.yaml").write_text(
        "id: s1\nmode: spec-driven\ndescription: d\ntimeout_minutes: 1\n"
        "scoring: {functional_tests: 0.5, spec_artifact_quality: 0.25, "
        "task_completion_rate: 0.15, steering_adherence: 0.1}\n"
    )
    (seed / "requirements.md").write_text("WHEN A THE SYSTEM SHALL B. " * 8)
    v = td / "verify.sh"
    v.write_text("#!/bin/bash\necho 'AGENT_COST_BENCH_RESULT: {\"score\":1.0}'\nexit 0\n")
    v.chmod(0o755)


@pytest.mark.asyncio
async def test_cli_compare_integration(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.05")
    _vibe_task(tmp_path)
    cfg = BenchConfig(
        mode=CompareMode.CLI_COMPARE,
        shared_model="mock",
        targets=[
            mock_target("mock-a", model_id="m-a"),
            mock_target("mock-b", model_id="m-b"),
        ],
        tasks_dir=str(tmp_path / "tasks"),
        workspace_base=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "results"),
        open_report=False,
        timeout_minutes=1,
    )
    run = await BenchmarkRunner(cfg).run()
    assert run.total_runs == 2
    assert all(r.status == TaskStatus.PASSED for r in run.results)
    json_path = JSONReporter(tmp_path / "results").write(run)
    html_path = HTMLReporter(tmp_path / "results", mode=CompareMode.CLI_COMPARE).write(run)
    assert json_path.exists() and html_path.exists()


@pytest.mark.asyncio
async def test_model_compare_integration_both_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.05")
    _vibe_task(tmp_path)
    _spec_task(tmp_path)
    cfg = BenchConfig(
        mode=CompareMode.MODEL_COMPARE,
        targets=[mock_target("sonnet"), mock_target("haiku")],
        tasks_dir=str(tmp_path / "tasks"),
        workspace_base=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "results"),
        open_report=False,
        timeout_minutes=1,
        parallel_workers=2,
    )
    run = await BenchmarkRunner(cfg).run()
    # 2 tasks (vibe + spec) × 2 models = 4 runs
    assert run.total_runs == 4
    modes = {r.mode.value for r in run.results}
    assert modes == {"vibe", "spec-driven"}
    html_path = HTMLReporter(tmp_path / "results", mode=CompareMode.MODEL_COMPARE).write(run)
    assert html_path.exists()
