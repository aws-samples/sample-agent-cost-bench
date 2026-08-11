"""Task 6 — evaluation core tests."""

from __future__ import annotations

import pytest

from agent_cost_bench.evaluator import (
    FunctionalEvaluator,
    SpecQualityEvaluator,
    SteeringAdherenceEvaluator,
    TaskCompletionEvaluator,
)
from agent_cost_bench.models import BenchConfig, CompareMode, ScoringWeights, TaskConfig, TaskMode
from agent_cost_bench.targets import make_kiro_target


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
    _write_verify(tmp_path, 'echo \'AGENT_COST_BENCH_RESULT: {"score": 0.7, "summary": "partial"}\'\nexit 1\n')
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
    from agent_cost_bench.evaluator.spec_paths import resolve_spec_dir

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
    from agent_cost_bench.evaluator.spec_paths import resolve_spec_dir

    specs = tmp_path / ".kiro" / "specs"
    (specs / "task-101").mkdir(parents=True)
    (specs / "task-101" / "requirements.md").write_text("# seeded\n")
    assert resolve_spec_dir(tmp_path, "task-101") == specs / "task-101"


def _rubric_evaluator(tmp_path, export_file="devin-usage.json"):
    from agent_cost_bench.evaluator.rubric import RubricEvaluator
    from agent_cost_bench.models import Pricing

    target = make_kiro_target("m")
    target.pricing = Pricing(devin_export_file=export_file)
    cfg = BenchConfig(mode=CompareMode.CLI_COMPARE, targets=[target])
    return RubricEvaluator(_task(tmp_path), tmp_path, cfg)


def test_rubric_diff_excludes_harness_artifacts(tmp_path):
    """The judge must never see workspace files the harness put there.

    Two of them scored a completed task 0.0 in a real run: the seeded
    .devin/config.json was graded as Devin's own work (while .kiro was already
    excluded — an asymmetry between runners), and the 127 KB --export usage file
    consumed the entire diff budget so the model's actual code changes were
    truncated away.
    """
    ev = _rubric_evaluator(tmp_path)
    diff = (
        "diff --git a/ws/.devin/config.json b/ws/.devin/config.json\n+seeded policy\n"
        "diff --git a/ws/devin-usage.json b/ws/devin-usage.json\n+huge export\n"
        "diff --git a/ws/lambdas/index.py b/ws/lambdas/index.py\n+bedrock_runtime = client()\n"
    )
    filtered = ev._filter_diff(diff)
    assert "bedrock_runtime" in filtered      # the model's real change survives
    assert "seeded policy" not in filtered
    assert "huge export" not in filtered


def test_rubric_skips_renamed_usage_export(tmp_path):
    """The export name is configurable, so it is read from config rather than
    hardcoded — renaming it must not re-expose it to the judge."""
    ev = _rubric_evaluator(tmp_path, export_file="custom-usage.json")
    diff = (
        "diff --git a/ws/custom-usage.json b/ws/custom-usage.json\n+huge export\n"
        "diff --git a/ws/main.py b/ws/main.py\n+real code\n"
    )
    filtered = ev._filter_diff(diff)
    assert "real code" in filtered
    assert "huge export" not in filtered


def test_rubric_diff_ignores_skip_words_in_the_host_prefix(tmp_path):
    """Skip matching must apply below the diff roots, not to the whole host path.

    git diff --no-index echoes absolute operands into the header, so a
    workspace_base such as ~/.cache/agent-cost-bench/build put 'build' and
    '.cache' in every path and the model's real change was dropped as vendored
    output — a 0.0 caused only by where the harness happened to put the run.
    """
    ev = _rubric_evaluator(tmp_path)
    roots = ["home/u/.cache/bench/build/base", "home/u/.cache/bench/build/ws"]
    diff = (
        "diff --git a/home/u/.cache/bench/build/base/app.py"
        " b/home/u/.cache/bench/build/ws/app.py\n+real code\n"
        "diff --git a/home/u/.cache/bench/build/ws/build/out.js"
        " b/home/u/.cache/bench/build/ws/build/out.js\n+generated\n"
    )
    filtered = ev._filter_diff(diff, roots=roots)
    assert "real code" in filtered       # survives despite 'build' in the prefix
    assert "generated" not in filtered   # a real build/ dir is still dropped


def test_rubric_collects_files_under_a_hidden_workspace_root(tmp_path):
    """Whole-file collection has the same root-relative requirement.

    With workspace_base under ~/.cache, every path contained a '.cache' segment,
    so _is_hidden_dir matched and the judge was told 'no files were produced'
    while notes_cli.py sat in the workspace — an observed silent 0.0.
    """
    ws = tmp_path / ".cache" / "bench" / "ws"
    (ws / ".devin").mkdir(parents=True)
    (ws / "notes_cli.py").write_text("print('real code')\n")
    (ws / ".devin" / "config.json").write_text("{}\n")
    collected = _rubric_evaluator(tmp_path)._collect_files(ws)
    assert "real code" in collected
    assert "config.json" not in collected  # hidden dirs inside the ws still skip
