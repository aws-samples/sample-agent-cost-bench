"""Declarative verify spec: config parsing + evaluator routing (Approach A)."""

from __future__ import annotations

import textwrap

import pytest

from kirobench.config import _load_task_config
from kirobench.models import FunctionalTestResult, TaskConfig, TaskMode


def test_task_config_parses_verify_block(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "task.yaml").write_text(textwrap.dedent("""
        id: t-verify
        mode: vibe
        verify:
          image: kirobench-node:20
          parser: vitest-json
          workdir: src
          tests_subdir: verify/tests
          setup:
            - mkdir -p "$BUILD/src/tests"
          test_cmd: 'vitest run --reporter=json --outputFile="$RESULTS_DIR/vitest.json"'
          network: none
    """))
    tc = _load_task_config(d / "task.yaml")
    assert tc.verify is not None
    assert tc.verify.image == "kirobench-node:20"
    assert tc.verify.parser == "vitest-json"
    assert tc.verify.setup == ['mkdir -p "$BUILD/src/tests"']
    assert tc.verify.network == "none"


@pytest.mark.asyncio
async def test_functional_evaluator_routes_to_docker_runner(tmp_path, monkeypatch):
    from kirobench.evaluator.functional import FunctionalEvaluator
    import kirobench.verify as verify_pkg

    sentinel = FunctionalTestResult(passed=True, score=1.0, summary="from docker runner")

    class FakeRunner:
        def __init__(self, task, workspace, logger=None):
            pass

        async def run(self):
            return sentinel

    monkeypatch.setattr(verify_pkg, "DockerVerifyRunner", FakeRunner)

    from kirobench.models import VerifySpec

    tc = TaskConfig(id="t", mode=TaskMode.VIBE, description="d")
    tc.verify = VerifySpec(image="img", test_cmd="run", parser="exit-code")

    result = await FunctionalEvaluator(tc, tmp_path).evaluate()
    assert result is sentinel
    assert result.summary == "from docker runner"


def test_required_docker_images_uses_verify_image():
    from kirobench.models import VerifySpec
    from kirobench.preflight import required_docker_images

    t = TaskConfig(id="t", mode=TaskMode.VIBE, description="d")
    t.verify = VerifySpec(image="kirobench-dotnet:8.0", test_cmd="x", parser="trx")
    assert required_docker_images([t]) == {"kirobench-dotnet:8.0"}
