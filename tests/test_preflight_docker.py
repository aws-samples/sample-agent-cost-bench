"""Docker preflight checks (required images, daemon/image presence aggregation)."""

from __future__ import annotations

from agent_cost_bench import preflight
from agent_cost_bench.models import TaskConfig, TaskMode


def _task(tid, image=None):
    return TaskConfig(id=tid, mode=TaskMode.VIBE, description="d", docker_image=image)


def test_required_docker_images_collects_only_declared():
    tasks = [_task("a"), _task("b", "agent_cost_bench-dotnet:8.0"), _task("c", "agent_cost_bench-java:17")]
    assert preflight.required_docker_images(tasks) == {"agent_cost_bench-dotnet:8.0", "agent_cost_bench-java:17"}


def test_docker_report_no_docker_tasks_is_noop():
    rep = preflight.docker_report([_task("a"), _task("b")])
    assert rep["needs_docker"] is False
    assert rep["docker_ok"] is True
    assert rep["missing_images"] == []
    assert rep["tasks_blocked"] == 0


def test_docker_report_all_present(monkeypatch):
    monkeypatch.setattr(preflight, "docker_available", lambda: True)
    monkeypatch.setattr(preflight, "missing_docker_images", lambda imgs: [])
    tasks = [_task("a", "agent_cost_bench-dotnet:8.0"), _task("b", "agent_cost_bench-node:20")]
    rep = preflight.docker_report(tasks)
    assert rep["needs_docker"] and rep["docker_ok"]
    assert rep["missing_images"] == []
    assert rep["tasks_blocked"] == 0


def test_docker_report_daemon_down_blocks_all_docker_tasks(monkeypatch):
    monkeypatch.setattr(preflight, "docker_available", lambda: False)
    tasks = [_task("a", "agent_cost_bench-dotnet:8.0"), _task("b"), _task("c", "agent_cost_bench-node:20")]
    rep = preflight.docker_report(tasks)
    assert rep["docker_ok"] is False
    assert set(rep["missing_images"]) == {"agent_cost_bench-dotnet:8.0", "agent_cost_bench-node:20"}
    assert rep["tasks_blocked"] == 2  # the two docker tasks; the plain one is unaffected


def test_docker_report_one_image_missing(monkeypatch):
    monkeypatch.setattr(preflight, "docker_available", lambda: True)
    monkeypatch.setattr(preflight, "missing_docker_images", lambda imgs: ["agent_cost_bench-node:20"])
    tasks = [_task("a", "agent_cost_bench-dotnet:8.0"), _task("b", "agent_cost_bench-node:20")]
    rep = preflight.docker_report(tasks)
    assert rep["missing_images"] == ["agent_cost_bench-node:20"]
    assert rep["tasks_blocked"] == 1


def test_task_config_parses_docker_image(tmp_path):
    import yaml

    from agent_cost_bench.config import _load_task_config

    d = tmp_path / "t"
    d.mkdir()
    (d / "task.yaml").write_text(
        yaml.safe_dump({"id": "t", "mode": "vibe", "docker_image": "agent_cost_bench-java:17"})
    )
    tc = _load_task_config(d / "task.yaml")
    assert tc.docker_image == "agent_cost_bench-java:17"
