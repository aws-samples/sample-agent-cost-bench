"""Task 9 — CLI entrypoint tests (click runner + MockCLI)."""

from __future__ import annotations

import sys

from click.testing import CliRunner

from kirobench.cli import main
from tests.conftest import MOCK_CLI


def _vibe_tasks(root):
    td = root / "tasks" / "vibe" / "t1"
    td.mkdir(parents=True)
    (td / "task.yaml").write_text("id: t1\nmode: vibe\ndescription: d\ntimeout_minutes: 1\nprompt: do it\n")
    v = td / "verify.sh"
    v.write_text(
        "#!/bin/bash\n"
        'if [ -f "$WORKSPACE/solution.py" ]; then echo \'KIROBENCH_RESULT: {"score":1.0}\'; exit 0;'
        ' else echo \'KIROBENCH_RESULT: {"score":0.0}\'; exit 1; fi\n'
    )
    v.chmod(0o755)
    return str(root / "tasks")


def _cli_compare_cfg(root, tasks_dir):
    py = sys.executable or "python3"
    cfg = root / "cli.yaml"
    cfg.write_text(
        f"shared_model: mock-model\n"
        f"runners:\n"
        f"  - name: mock\n"
        f"    cli_path: {py}\n"
        f"    model_id: mock-model\n"
        f"    cost_source: kiro_credits\n"
        f"    pricing: {{usd_per_credit: 0.04}}\n"
        f'    cli_base_args: ["{MOCK_CLI}", "--model={{model}}"]\n'
        f"tasks_dir: {tasks_dir}\n"
        f"workspace_base: {root / 'ws'}\n"
        f"output_dir: {root / 'results'}\n"
        f"open_report: false\n"
    )
    return str(cfg)


def test_validate_command(tmp_path):
    tasks_dir = _vibe_tasks(tmp_path)
    cfg = _cli_compare_cfg(tmp_path, tasks_dir)
    result = CliRunner().invoke(main, ["cli-compare", "validate", cfg])
    assert result.exit_code == 0, result.output
    assert "Validation passed" in result.output


def test_list_tasks_command(tmp_path):
    tasks_dir = _vibe_tasks(tmp_path)
    cfg = _cli_compare_cfg(tmp_path, tasks_dir)
    result = CliRunner().invoke(main, ["cli-compare", "list-tasks", cfg])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output


def test_cli_compare_run_produces_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.05")
    tasks_dir = _vibe_tasks(tmp_path)
    cfg = _cli_compare_cfg(tmp_path, tasks_dir)
    result = CliRunner().invoke(main, ["cli-compare", "run", cfg, "--no-open"])
    # All passed -> exit 0
    assert result.exit_code == 0, result.output
    results_dir = tmp_path / "results"
    assert any(p.suffix == ".html" for p in results_dir.iterdir())
    assert any(p.suffix == ".json" for p in results_dir.iterdir())


def test_report_command_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    tasks_dir = _vibe_tasks(tmp_path)
    cfg = _cli_compare_cfg(tmp_path, tasks_dir)
    CliRunner().invoke(main, ["cli-compare", "run", cfg, "--no-open"])
    results_dir = tmp_path / "results"
    json_file = next(p for p in results_dir.iterdir() if p.suffix == ".json")
    out = tmp_path / "report-out"
    result = CliRunner().invoke(
        main, ["report", str(json_file), "-o", str(out), "--no-open"]
    )
    assert result.exit_code == 0, result.output
    assert any(p.suffix == ".html" for p in out.iterdir())


def test_new_task_scaffold(tmp_path):
    result = CliRunner().invoke(
        main, ["new-task", "task-xyz", "--mode", "spec-driven", "--tasks-dir", str(tmp_path / "tasks")]
    )
    assert result.exit_code == 0, result.output
    td = tmp_path / "tasks" / "spec-driven" / "task-xyz"
    assert (td / "task.yaml").exists()
    assert (td / "seed" / "requirements.md").exists()
