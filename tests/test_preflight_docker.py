"""Docker preflight checks (required images, daemon/image presence aggregation)."""

from __future__ import annotations

from kirobench import preflight
from kirobench.models import TaskConfig, TaskMode


def _task(tid, image=None):
    return TaskConfig(id=tid, mode=TaskMode.VIBE, description="d", docker_image=image)


def test_required_docker_images_collects_only_declared():
    tasks = [_task("a"), _task("b", "kirobench-dotnet:8.0"), _task("c", "kirobench-java:17")]
    assert preflight.required_docker_images(tasks) == {"kirobench-dotnet:8.0", "kirobench-java:17"}


def test_docker_report_no_docker_tasks_is_noop():
    rep = preflight.docker_report([_task("a"), _task("b")])
    assert rep["needs_docker"] is False
    assert rep["docker_ok"] is True
    assert rep["missing_images"] == []
    assert rep["tasks_blocked"] == 0


def test_docker_report_all_present(monkeypatch):
    monkeypatch.setattr(preflight, "docker_available", lambda: True)
    monkeypatch.setattr(preflight, "missing_docker_images", lambda imgs: [])
    tasks = [_task("a", "kirobench-dotnet:8.0"), _task("b", "kirobench-node:20")]
    rep = preflight.docker_report(tasks)
    assert rep["needs_docker"] and rep["docker_ok"]
    assert rep["missing_images"] == []
    assert rep["tasks_blocked"] == 0


def test_docker_report_daemon_down_blocks_all_docker_tasks(monkeypatch):
    monkeypatch.setattr(preflight, "docker_available", lambda: False)
    tasks = [_task("a", "kirobench-dotnet:8.0"), _task("b"), _task("c", "kirobench-node:20")]
    rep = preflight.docker_report(tasks)
    assert rep["docker_ok"] is False
    assert set(rep["missing_images"]) == {"kirobench-dotnet:8.0", "kirobench-node:20"}
    assert rep["tasks_blocked"] == 2  # the two docker tasks; the plain one is unaffected


def test_docker_report_one_image_missing(monkeypatch):
    monkeypatch.setattr(preflight, "docker_available", lambda: True)
    monkeypatch.setattr(preflight, "missing_docker_images", lambda imgs: ["kirobench-node:20"])
    tasks = [_task("a", "kirobench-dotnet:8.0"), _task("b", "kirobench-node:20")]
    rep = preflight.docker_report(tasks)
    assert rep["missing_images"] == ["kirobench-node:20"]
    assert rep["tasks_blocked"] == 1


def test_task_config_parses_docker_image(tmp_path):
    import yaml

    from kirobench.config import _load_task_config

    d = tmp_path / "t"
    d.mkdir()
    (d / "task.yaml").write_text(
        yaml.safe_dump({"id": "t", "mode": "vibe", "docker_image": "kirobench-java:17"})
    )
    tc = _load_task_config(d / "task.yaml")
    assert tc.docker_image == "kirobench-java:17"
