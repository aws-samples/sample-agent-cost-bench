"""Tasks 4 & 5 — unified execution core + spec phases (via MockCLI)."""

from __future__ import annotations

import asyncio

import pytest

from kirobench.executor import SpecDrivenExecutor, VibeExecutor
from kirobench.executor.spec import SpecCapabilityError
from kirobench.models import BenchConfig, CompareMode, CostSource
from tests.conftest import mock_target


def _cfg(targets, tmp_path, **kw):
    return BenchConfig(
        mode=CompareMode.MODEL_COMPARE,
        targets=targets,
        workspace_base=str(tmp_path / "ws"),
        timeout_minutes=1,
        transient_retries=kw.get("transient_retries", 0),
        output_dir=str(tmp_path / "results"),
        open_report=False,
    )


def test_build_command_from_templates(tmp_path, vibe_task):
    t = mock_target()
    cfg = _cfg([t], tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    ex = VibeExecutor(cfg, vibe_task, ws, t)
    cmd = ex._build_command("PROMPT", agent=None)
    assert cmd[0] == t.cli_path
    assert "--model=mock-model" in cmd
    assert cmd[-1] == "PROMPT"  # appended because no {prompt} in base args


def test_effort_substituted_in_command(tmp_path, vibe_task):
    t = mock_target()
    # Runner-style: {effort} inline in cli_base_args (as in the cli-compare config).
    t.cli_base_args = [t.cli_base_args[0], "--model={model}", "--effort={effort}"]
    cfg = _cfg([t], tmp_path)
    cfg.effort = "low"
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    cmd = VibeExecutor(cfg, vibe_task, ws, t)._build_command("PROMPT")
    assert "--effort=low" in cmd


@pytest.mark.asyncio
async def test_vibe_execution_yields_usage(tmp_path, vibe_task, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.07")
    t = mock_target(pricing=None)
    cfg = _cfg([t], tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    phases = await VibeExecutor(cfg, vibe_task, ws, t).execute()
    assert len(phases) == 1
    p = phases[0]
    assert p.success
    assert abs(p.credits - 0.07) < 1e-9
    assert abs(p.cost_usd - 0.07 * 0.04) < 1e-9
    # The mock created solution.py in the workspace.
    assert (ws / "solution.py").exists()


@pytest.mark.asyncio
async def test_transient_error_retries_then_surfaces(tmp_path, vibe_task, monkeypatch):
    monkeypatch.setenv("MOCK_TRANSIENT", "1")
    # avoid real backoff sleeps (capture the real sleep to avoid self-recursion)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    t = mock_target()
    cfg = _cfg([t], tmp_path, transient_retries=2)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    phases = await VibeExecutor(cfg, vibe_task, ws, t).execute()
    p = phases[0]
    assert not p.success
    assert p.transient_error
    assert p.transient_retries == 2  # exhausted retries


@pytest.mark.asyncio
async def test_spec_runs_native_single_phase(tmp_path, spec_task, monkeypatch):
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.10")
    t = mock_target(supports_spec=True)
    t.spec_mode_args = ["--mode", "spec"]
    cfg = _cfg([t], tmp_path)
    ws = tmp_path / "ws"
    (ws / ".kiro" / "specs" / spec_task.id).mkdir(parents=True)
    (ws / ".kiro" / "specs" / spec_task.id / "requirements.md").write_text("# r\n")
    phases = await SpecDrivenExecutor(cfg, spec_task, ws, t).execute()
    # Native spec mode is a single CLI invocation.
    assert [p.phase for p in phases] == ["spec"]
    assert phases[0].success
    assert abs(phases[0].credits - 0.10) < 1e-9


def test_spec_mode_args_injected_into_command(tmp_path, spec_task):
    t = mock_target(supports_spec=True)
    t.spec_mode_args = ["--mode", "spec"]
    cfg = _cfg([t], tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    cmd = SpecDrivenExecutor(cfg, spec_task, ws, t)._build_command(
        "PROMPT", extra_args=t.spec_mode_args
    )
    assert "--mode" in cmd and "spec" in cmd


@pytest.mark.asyncio
async def test_spec_on_non_spec_target_raises(tmp_path, spec_task):
    t = mock_target(supports_spec=False)
    cfg = _cfg([t], tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    with pytest.raises(SpecCapabilityError):
        await SpecDrivenExecutor(cfg, spec_task, ws, t).execute()


@pytest.mark.asyncio
async def test_pty_execution_runs_and_captures(tmp_path, vibe_task, monkeypatch):
    # The PTY path must run the CLI, capture its output, and create files —
    # confirming the pseudo-terminal plumbing works headlessly.
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    monkeypatch.setenv("MOCK_CREDITS", "0.05")
    t = mock_target()
    cfg = _cfg([t], tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    phase = await VibeExecutor(cfg, vibe_task, ws, t).run_phase("vibe", "PROMPT", use_pty=True)
    assert phase.success
    assert "[MockCLI] handled prompt" in phase.stdout
    assert (ws / "solution.py").exists()


@pytest.mark.asyncio
async def test_timeout_captures_partial_output(tmp_path, vibe_task, monkeypatch):
    # A hung CLI prints partial output then blocks; the harness must kill it on
    # timeout AND retain what it printed (for diagnosis).
    monkeypatch.setenv("MOCK_HANG", "1")
    monkeypatch.setenv("MOCK_HANG_SECONDS", "30")
    t = mock_target()
    cfg = _cfg([t], tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    ex = VibeExecutor(cfg, vibe_task, ws, t)
    phase = await ex.run_phase("vibe", "PROMPT", timeout_seconds=1)
    assert phase.timed_out is True
    assert "PARTIAL_OUTPUT_BEFORE_HANG" in phase.stdout
    assert "starting work" in phase.stderr


@pytest.mark.asyncio
async def test_spec_prompt_via_stdin(tmp_path, spec_task, monkeypatch):
    # When spec_prompt_via_stdin is set, the prompt is piped to the CLI's stdin
    # and NOT passed as a positional arg. MockCLI echoes stdin via MOCK_ECHO_STDIN.
    monkeypatch.setenv("MOCK_COST_MODE", "kiro")
    t = mock_target(supports_spec=True)
    t.spec_mode_args = ["--v3", "--mode", "spec"]
    cfg = _cfg([t], tmp_path)
    cfg.spec_prompt_via_stdin = True
    ws = tmp_path / "ws"
    (ws / ".kiro" / "specs" / spec_task.id).mkdir(parents=True)
    (ws / ".kiro" / "specs" / spec_task.id / "requirements.md").write_text("# r\n")
    phases = await SpecDrivenExecutor(cfg, spec_task, ws, t).execute()
    assert phases[0].success
    # The resolved prompt must NOT be appended as a positional arg (it goes to stdin).
    cmd = SpecDrivenExecutor(cfg, spec_task, ws, t)._build_command("", extra_args=t.spec_mode_args)
    assert "--mode" in cmd and "spec" in cmd
    assert not any("Implement the feature" in part for part in cmd)
