"""Task 7 — runner orchestration end-to-end (via MockCLI)."""

from __future__ import annotations

import pytest

from agent_cost_bench.models import BenchConfig, CompareMode, TaskStatus
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


def test_autobuild_images_builds_missing_image(tmp_path, monkeypatch):
    """A task with verify.build_context triggers an on-demand image build."""
    from pathlib import Path

    from agent_cost_bench.models import TaskConfig, VerifySpec
    import agent_cost_bench.verify.docker_build as db

    # A task whose Docker image ships as a build context.
    task_dir = tmp_path / "tasks" / "vibe" / "imported"
    (task_dir / "verify" / "environment").mkdir(parents=True)
    (task_dir / "verify" / "environment" / "Dockerfile").write_text("FROM scratch\n")
    task = TaskConfig(
        id="imported",
        description="d",
        prompt="do it",
        verify=VerifySpec(
            image="tb-imported:imported",
            build_context="verify/environment",
            test_cmd="true",
            parser="reward-file",
        ),
    )
    task.task_dir = task_dir

    calls = []

    class _Res:
        ok = True
        built = True
        detail = "built"

    def fake_ensure(image, context, **kw):
        calls.append((image, Path(context)))
        return _Res()

    monkeypatch.setattr("agent_cost_bench.verify.docker_build.ensure_image", fake_ensure)

    cfg = _cfg(tmp_path, [mock_target()], str(tmp_path / "tasks"))
    runner = BenchmarkRunner(cfg)
    runner._ensure_images([task])

    assert len(calls) == 1
    image, context = calls[0]
    assert image == "tb-imported:imported"
    assert context == (task_dir / "verify" / "environment").resolve()


def test_ensure_images_pulls_prebuilt_image(tmp_path, monkeypatch):
    """A task referencing a prebuilt image with NO build_context triggers a
    docker pull (not a build)."""
    from agent_cost_bench.models import TaskConfig, VerifySpec
    import agent_cost_bench.verify.docker_build as db

    # Prebuilt, externally-hosted image (e.g. a Terminal-Bench task image).
    task = TaskConfig(
        id="tb-prebuilt",
        description="d",
        prompt="do it",
        verify=VerifySpec(
            image="alexgshaw/adaptive-rejection-sampler:20251031",
            test_cmd="true",
            parser="reward-file",
        ),
    )

    pulled = []
    built = []

    class _Res:
        ok = True
        built = True
        detail = "pulled"

    def fake_pull(image, **kw):
        pulled.append(image)
        return _Res()

    def fake_build(image, context, **kw):
        built.append(image)
        return _Res()

    monkeypatch.setattr("agent_cost_bench.verify.docker_build.ensure_image_pulled", fake_pull)
    monkeypatch.setattr("agent_cost_bench.verify.docker_build.ensure_image", fake_build)

    cfg = _cfg(tmp_path, [mock_target()], str(tmp_path / "tasks"))
    runner = BenchmarkRunner(cfg)
    runner._ensure_images([task])

    # Pulled, never built — no build context means there's nothing to build.
    assert pulled == ["alexgshaw/adaptive-rejection-sampler:20251031"]
    assert built == []


def _multi_task_tree(root, n: int) -> str:
    for i in range(n):
        td = root / "tasks" / "vibe" / f"t{i}"
        td.mkdir(parents=True)
        (td / "task.yaml").write_text(
            f"id: t{i}\nmode: vibe\ndescription: d\ntimeout_minutes: 1\nprompt: do it\n"
        )
    return str(root / "tasks")


def _lane_cfg(tmp_path, targets, tasks_dir, **kw) -> BenchConfig:
    return BenchConfig(
        mode=CompareMode.MODEL_COMPARE,
        targets=targets,
        tasks_dir=tasks_dir,
        workspace_base=str(tmp_path / "ws"),
        output_dir=str(tmp_path / "results"),
        timeout_minutes=1,
        open_report=False,
        **kw,
    )


async def _record_schedule(runner, monkeypatch, delays: dict[str, float]):
    """Replace _run_single with a fake that sleeps per target and records how
    many jobs of each target run at the same time."""
    import asyncio

    from agent_cost_bench.models import RunResult, TaskMode

    active: dict[str, int] = {}
    peak: dict[str, int] = {}
    order: list[str] = []

    async def fake(task, target, repeat=1):
        active[target.name] = active.get(target.name, 0) + 1
        peak[target.name] = max(peak.get(target.name, 0), active[target.name])
        order.append(f"{target.name}:{task.id}")
        await asyncio.sleep(delays[target.name])
        active[target.name] -= 1
        return RunResult(task_id=task.id, target=target.label, mode=TaskMode.VIBE,
                         status=TaskStatus.PASSED, repeat=repeat)

    monkeypatch.setattr(runner, "_run_single", fake)
    await runner.run()
    return peak, order


@pytest.mark.asyncio
async def test_per_target_lanes_keep_fast_target_busy(tmp_path, monkeypatch):
    """A slow target must not take the fast target's slot. With one shared
    semaphore the fast target's later tasks waited behind the slow one."""
    tasks_dir = _multi_task_tree(tmp_path, 4)
    targets = [mock_target("fast"), mock_target("slow")]
    runner = BenchmarkRunner(_lane_cfg(tmp_path, targets, tasks_dir))
    peak, order = await _record_schedule(runner, monkeypatch, {"fast": 0.01, "slow": 0.2})
    # Each target runs one job at a time…
    assert peak == {"fast": 1, "slow": 1}
    # …and the fast target finishes all 4 tasks before the slow one starts its 2nd.
    assert order.index("slow:t1") > order.index("fast:t3")


@pytest.mark.asyncio
async def test_concurrency_per_target_allows_parallel_tasks(tmp_path, monkeypatch):
    tasks_dir = _multi_task_tree(tmp_path, 4)
    runner = BenchmarkRunner(
        _lane_cfg(tmp_path, [mock_target("only")], tasks_dir, concurrency_per_target=3)
    )
    assert runner.config.effective_workers(4) == 3
    peak, _ = await _record_schedule(runner, monkeypatch, {"only": 0.05})
    assert peak == {"only": 3}


def test_ensure_images_builds_in_parallel(tmp_path, monkeypatch):
    """Several missing images are built concurrently, each exactly once."""
    import threading
    import time

    from agent_cost_bench.models import TaskConfig, VerifySpec
    from agent_cost_bench.verify import docker_build

    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "built": []}

    class _Res:
        ok = True
        built = True
        detail = ""

    def fake_ensure_image(image, context, **kw):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
            state["built"].append(image)
        return _Res()

    monkeypatch.setattr(docker_build, "ensure_image", fake_ensure_image)
    tasks = [
        TaskConfig(
            id=f"t{i}", prompt="p", task_dir=tmp_path,
            verify=VerifySpec(image=f"img{i % 3}:x", build_context="ctx",
                              test_cmd="true", parser="reward-file"),
        )
        for i in range(6)
    ]
    cfg = _cfg(tmp_path, [mock_target()], str(tmp_path / "tasks"))
    cfg.image_build_workers = 3
    BenchmarkRunner(cfg)._ensure_images(tasks)
    assert sorted(state["built"]) == ["img0:x", "img1:x", "img2:x"]  # 3 unique images
    assert state["peak"] == 3
