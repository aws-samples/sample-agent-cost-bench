"""Task 6 — evaluation core tests."""

from __future__ import annotations

import pytest

from kirobench.evaluator import (
    FunctionalEvaluator,
    SpecQualityEvaluator,
    SteeringAdherenceEvaluator,
    TaskCompletionEvaluator,
)
from kirobench.models import BenchConfig, CompareMode, ScoringWeights, TaskConfig, TaskMode
from kirobench.targets import make_kiro_target


def _task(tmp_path, mode=TaskMode.VIBE):
    tc = TaskConfig(id="t", mode=mode, description="d", timeout_minutes=1)
    tc.task_dir = tmp_path
    return tc


def _write_verify(tmp_path, body):
    v = tmp_path / "verify.sh"
    v.write_text("#!/bin/bash\n" + body)
    v.chmod(0o755)


@pytest.mark.asyncio
async def test_graduated_marker_parsed(tmp_path):
    _write_verify(tmp_path, 'echo \'KIROBENCH_RESULT: {"score": 0.7, "summary": "partial"}\'\nexit 1\n')
    res = await FunctionalEvaluator(_task(tmp_path), tmp_path).evaluate()
    assert abs(res.score - 0.7) < 1e-9
    assert res.passed is False  # graduated <1.0 does not pass the hard gate
    assert res.summary == "partial"


@pytest.mark.asyncio
async def test_legacy_markers_accepted(tmp_path):
    _write_verify(tmp_path, 'echo \'KIRO_BENCH_RESULT: {"score": 1.0}\'\nexit 0\n')
    res = await FunctionalEvaluator(_task(tmp_path), tmp_path).evaluate()
    assert res.score == 1.0 and res.passed is True


@pytest.mark.asyncio
async def test_binary_fallback_exit_code(tmp_path):
    _write_verify(tmp_path, "echo nothing-structured\nexit 0\n")
    res = await FunctionalEvaluator(_task(tmp_path), tmp_path).evaluate()
    assert res.score == 1.0 and res.passed is True
    _write_verify(tmp_path, "echo fail\nexit 3\n")
    res2 = await FunctionalEvaluator(_task(tmp_path), tmp_path).evaluate()
    assert res2.score == 0.0 and res2.passed is False


@pytest.mark.asyncio
async def test_missing_verification_is_harness_error(tmp_path):
    res = await FunctionalEvaluator(_task(tmp_path), tmp_path).evaluate()
    assert res.checkpoints.get("harness_error") is True


def test_task_completion_counts_checkboxes(tmp_path):
    specs = tmp_path / ".kiro" / "specs" / "t"
    specs.mkdir(parents=True)
    (specs / "tasks.md").write_text("- [x] one\n- [x] two\n- [ ] three\n")
    rate = TaskCompletionEvaluator(_task(tmp_path), tmp_path).evaluate()
    assert abs(rate - 2 / 3) < 1e-9


@pytest.mark.asyncio
async def test_spec_quality_rule_scores(tmp_path):
    specs = tmp_path / ".kiro" / "specs" / "t"
    specs.mkdir(parents=True)
    (specs / "requirements.md").write_text(
        "As a user, I want X. WHEN A THE SYSTEM SHALL B. Acceptance Criteria: yes. " * 5
    )
    cfg = BenchConfig(
        mode=CompareMode.MODEL_COMPARE,
        targets=[make_kiro_target("m")],
    )
    scores = await SpecQualityEvaluator(_task(tmp_path, TaskMode.SPEC_DRIVEN), tmp_path, cfg).evaluate()
    assert scores.requirements_score > 0.0
    assert scores.details["artifacts_present"]["requirements.md"] is True


@pytest.mark.asyncio
async def test_judge_failure_falls_back_to_rules(tmp_path, monkeypatch):
    specs = tmp_path / ".kiro" / "specs" / "t"
    specs.mkdir(parents=True)
    (specs / "requirements.md").write_text("WHEN A THE SYSTEM SHALL B. " * 10)
    # judge_model set but the judge CLI does not exist -> judge.score returns ok=False
    cfg = BenchConfig(
        mode=CompareMode.MODEL_COMPARE,
        targets=[make_kiro_target("m")],
        judge_model="claude-sonnet-4",
        judge_cli_path="/nonexistent/kiro-binary-xyz",
        judge_weight=0.6,
    )
    ev = SpecQualityEvaluator(_task(tmp_path, TaskMode.SPEC_DRIVEN), tmp_path, cfg)
    scores = await ev.evaluate()
    # Falls back to rule-only; the judge detail records the failure.
    jd = scores.details.get("llm_judge", {}).get("requirements", {})
    assert jd.get("ok") is False
    assert jd.get("used") == "rule_only"


@pytest.mark.asyncio
async def test_steering_not_applicable_without_docs(tmp_path):
    cfg = BenchConfig(mode=CompareMode.MODEL_COMPARE, targets=[make_kiro_target("m")])
    score, details = await SteeringAdherenceEvaluator(_task(tmp_path), tmp_path, cfg).evaluate()
    assert details.get("not_applicable") is True
    assert score == 1.0


def test_resolve_spec_dir_ignores_empty_preferred(tmp_path):
    from kirobench.evaluator.spec_paths import resolve_spec_dir

    specs = tmp_path / ".kiro" / "specs"
    # Harness pre-created an EMPTY dir named after the task id.
    (specs / "task-100").mkdir(parents=True)
    # Native spec mode wrote artifacts to a feature-named dir.
    feature = specs / "shopping-cart-pricing"
    feature.mkdir()
    (feature / "requirements.md").write_text("# r\n")
    resolved = resolve_spec_dir(tmp_path, "task-100")
    assert resolved == feature  # not the empty preferred dir


def test_resolve_spec_dir_prefers_seeded_task_id(tmp_path):
    from kirobench.evaluator.spec_paths import resolve_spec_dir

    specs = tmp_path / ".kiro" / "specs"
    (specs / "task-101").mkdir(parents=True)
    (specs / "task-101" / "requirements.md").write_text("# seeded\n")
    assert resolve_spec_dir(tmp_path, "task-101") == specs / "task-101"
