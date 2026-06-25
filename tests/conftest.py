"""Shared pytest fixtures: a MockCLI-backed Target and config/task builders."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_cost_bench.models import (
    BenchConfig,
    CompareMode,
    CostSource,
    Pricing,
    ScoringWeights,
    Target,
    TargetCapabilities,
    TaskConfig,
    TaskMode,
)

MOCK_CLI = str(Path(__file__).parent / "mock_cli.py")


def mock_target(
    name: str = "mock",
    *,
    cost_source: CostSource = CostSource.KIRO_CREDITS,
    supports_spec: bool = True,
    model_id: str = "mock-model",
    pricing: Pricing | None = None,
) -> Target:
    """A Target that runs ``python3 tests/mock_cli.py --model=... <prompt>``."""
    return Target(
        name=name,
        cli_path=sys.executable or "python3",
        model_id=model_id,
        cli_base_args=[MOCK_CLI, "--model={model}"],
        cost_source=cost_source,
        pricing=pricing or Pricing(usd_per_credit=0.04),
        capabilities=TargetCapabilities(supports_spec=supports_spec, supports_agents=False),
    )


@pytest.fixture
def mock_cli_path() -> str:
    return MOCK_CLI


@pytest.fixture
def vibe_task(tmp_path) -> TaskConfig:
    """A minimal vibe task whose verify.sh passes when solution.py exists."""
    task_dir = tmp_path / "task-mock-vibe"
    task_dir.mkdir()
    verify = task_dir / "verify.sh"
    verify.write_text(
        "#!/bin/bash\n"
        'if [ -f "$WORKSPACE/solution.py" ]; then\n'
        '  echo \'AGENT_COST_BENCH_RESULT: {"score": 1.0, "summary": "file present"}\'\n'
        "  exit 0\n"
        "else\n"
        '  echo \'AGENT_COST_BENCH_RESULT: {"score": 0.0, "summary": "no file"}\'\n'
        "  exit 1\n"
        "fi\n"
    )
    verify.chmod(0o755)
    tc = TaskConfig(
        id="task-mock-vibe", mode=TaskMode.VIBE, description="mock vibe",
        timeout_minutes=1, prompt="Write a solution.",
    )
    tc.task_dir = task_dir
    return tc


@pytest.fixture
def spec_task(tmp_path) -> TaskConfig:
    """A minimal spec-driven task with a seeded requirements.md."""
    task_dir = tmp_path / "task-mock-spec"
    seed = task_dir / "seed"
    seed.mkdir(parents=True)
    (seed / "requirements.md").write_text("# Requirements\n\nWHEN run THE SYSTEM SHALL work.\n")
    verify = task_dir / "verify.sh"
    verify.write_text(
        "#!/bin/bash\n"
        'echo \'AGENT_COST_BENCH_RESULT: {"score": 1.0, "summary": "ok"}\'\n'
        "exit 0\n"
    )
    verify.chmod(0o755)
    tc = TaskConfig(
        id="task-mock-spec",
        mode=TaskMode.SPEC_DRIVEN,
        description="mock spec",
        timeout_minutes=1,
        scoring=ScoringWeights(
            functional_tests=0.5,
            spec_artifact_quality=0.25,
            task_completion_rate=0.15,
            steering_adherence=0.10,
        ),
    )
    tc.task_dir = task_dir
    return tc


def mock_config(mode: CompareMode, targets, tasks_dir: str, workspace_base: str) -> BenchConfig:
    return BenchConfig(
        mode=mode,
        targets=targets,
        tasks_dir=tasks_dir,
        workspace_base=workspace_base,
        parallel_workers=2,
        timeout_minutes=1,
        transient_retries=1,
        output_dir=str(Path(workspace_base) / "results"),
        open_report=False,
    )
