"""
Functional correctness evaluator: runs verify.sh / verify.py in the workspace,
or a centralised pytest suite (verify/test_*.py).

Scoring modes:

1. **Graduated (preferred).** The verify script prints one line::

       KIROBENCH_RESULT: {"score": 0.7, "checkpoints": {...}, "summary": "..."}

   ``score`` (0.0–1.0) becomes the functional score; checkpoints + summary are
   surfaced in the report. Legacy markers ``KIRO_BENCH_RESULT`` and
   ``CLI_BENCH_RESULT`` are also accepted so existing fixtures keep working.

2. **Binary (fallback).** No marker → exit 0 = score 1.0, non-zero = 0.0.

Every verify run gets an isolated, fully-configured environment with common
secrets pre-set so secure implementations that *require* configuration boot
cleanly (no "env not set" false failures).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from ..models import FunctionalTestResult, TaskConfig

# Markers the verify script may print (current + legacy from both harnesses).
_RESULT_MARKERS = ("KIROBENCH_RESULT:", "KIRO_BENCH_RESULT:", "CLI_BENCH_RESULT:")
_RESULT_RE = re.compile(
    r"^(?:KIROBENCH_RESULT|KIRO_BENCH_RESULT|CLI_BENCH_RESULT):\s*(\{.*\})\s*$",
    re.MULTILINE,
)

_STANDARD_ENV = {
    "JWT_SECRET_KEY": "kirobench-test-secret-key-not-for-production",
    "SECRET_KEY": "kirobench-test-secret-key-not-for-production",
    "JWT_SECRET": "kirobench-test-secret-key-not-for-production",
    "JWT_ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "15",
    "REFRESH_TOKEN_EXPIRE_DAYS": "7",
    "DATABASE_URL": "sqlite:///./kirobench_test.db",
    "ASYNC_DATABASE_URL": "sqlite+aiosqlite:///./kirobench_test.db",
    "SYNC_DATABASE_URL": "sqlite:///./kirobench_test.db",
    "TEST_DATABASE_URL": "sqlite:///./kirobench_test.db",
    "ENVIRONMENT": "test",
    "TESTING": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class FunctionalEvaluator:
    """Runs the task's verification and returns a FunctionalTestResult."""

    def __init__(self, task: TaskConfig, workspace_path: Path, logger=None, config=None, model_label: str = ""):
        self.task = task
        self.workspace = workspace_path
        self._logger = logger
        self._config = config
        self._model_label = model_label

    async def evaluate(self) -> FunctionalTestResult:
        # Declarative verification takes priority when configured. Dispatch by
        # runner: 'docker' runs tests in a container; 'local' runs a scorer in a
        # framework-managed venv (replaces the per-task verify.sh boilerplate).
        if self.task.verify is not None:
            if self.task.verify.runner == "local":
                from .script_runner import ScriptVerifyRunner

                return await ScriptVerifyRunner(
                    self.task, self.workspace, logger=self._logger
                ).run()
            if self.task.verify.runner == "pytest":
                from .pytest_runner import PytestSuiteRunner

                pytest_dir = self._find_pytest_dir()
                if pytest_dir is not None:
                    return await PytestSuiteRunner(
                        self.task, self.workspace, pytest_dir,
                        extra_deps=self.task.verify.deps,
                        logger=self._logger,
                    ).run()
                return FunctionalTestResult(
                    passed=False, exit_code=-1, score=0.0,
                    summary="verify.runner=pytest but no test_*.py found in verify/ — "
                            "harness misconfiguration, not a model failure",
                    checkpoints={"harness_error": True},
                    stderr="No test_*.py files found in verify/",
                )
            from ..verify import DockerVerifyRunner

            return await DockerVerifyRunner(self.task, self.workspace, logger=self._logger).run()

        verify_script = self._find_verify_script()
        if verify_script is None:
            pytest_dir = self._find_pytest_dir()
            if pytest_dir is not None:
                from .pytest_runner import PytestSuiteRunner

                return await PytestSuiteRunner(
                    self.task, self.workspace, pytest_dir,
                    logger=self._logger,
                ).run()

            # No code-based verification — fall back to no-code rubric grading
            # (LLM-judge checklist) when the task defines one.
            if self.task.quality is not None and self.task.quality.rubric:
                from .rubric import RubricEvaluator

                if self._config is None:
                    return FunctionalTestResult(
                        passed=False, exit_code=-1, score=0.0,
                        summary="Rubric task could not be graded: no config passed to the "
                                "functional evaluator (harness misconfiguration).",
                        checkpoints={"harness_error": True},
                        stderr="RubricEvaluator requires a BenchConfig",
                    )
                return await RubricEvaluator(
                    self.task, self.workspace, self._config,
                    logger=self._logger, model_label=self._model_label,
                ).evaluate()

            return FunctionalTestResult(
                passed=False,
                exit_code=-1,
                score=0.0,
                summary="No verification found (verify.sh, verify.py, verify/test_*.py, "
                        "or a quality.rubric) — harness misconfiguration, not a model failure",
                checkpoints={"harness_error": True},
                stderr="No verification script, pytest suite, or rubric found",
            )

        start = time.monotonic()
        try:
            if verify_script.suffix == ".py":
                cmd = ["python3", str(verify_script)]
            else:
                cmd = ["bash", str(verify_script)]

            env = os.environ.copy()
            env.update(_STANDARD_ENV)
            env["WORKSPACE"] = str(self.workspace)
            # Note: we deliberately do NOT set DOCKER_HOST here. Docker resolves
            # its active context from ~/.docker/config.json (file-based, not the
            # shell profile), so the verify script reaches the SAME daemon used
            # to build the images. verify.sh handles socket fallback if needed.

            if self._logger:
                await self._logger.log_event(
                    f"VERIFY START  {self.task.id}\n"
                    f"    cmd: {' '.join(cmd)}\n"
                    f"    workspace: {self.workspace}"
                )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=env,
                start_new_session=True,
            )

            verify_timeout = min(self.task.timeout_minutes * 60, 300)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=float(verify_timeout)
                )
            except asyncio.TimeoutError:
                try:
                    import signal

                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                await proc.communicate()
                return FunctionalTestResult(
                    passed=False, exit_code=-1, score=0.0,
                    summary=f"Verification timed out after {verify_timeout}s",
                    duration_seconds=time.monotonic() - start,
                    stderr=f"Verification timed out after {verify_timeout}s",
                )

            exit_code = proc.returncode or 0
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            duration = time.monotonic() - start

            if self._logger:
                await self._logger.log_call(
                    task_id=self.task.id,
                    target="verify",
                    phase="verify",
                    command=cmd,
                    prompt=f"WORKSPACE={self.workspace}",
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                    duration_seconds=duration,
                )

            return self._build_result(exit_code, stdout, stderr, duration)

        except Exception as e:
            return FunctionalTestResult(
                passed=False, exit_code=-1, score=0.0,
                summary=f"Error running verification: {e}",
                duration_seconds=time.monotonic() - start,
                stderr=f"Error running verification: {e}",
            )

    # ------------------------------------------------------------------

    def _build_result(self, exit_code, stdout, stderr, duration) -> FunctionalTestResult:
        return build_marker_result(exit_code, stdout, stderr, duration)

    @staticmethod
    def _parse_marker(text: str) -> dict | None:
        if not text or not any(m in text for m in _RESULT_MARKERS):
            return None
        matches = _RESULT_RE.findall(text)
        if not matches:
            return None
        for raw in reversed(matches):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _auto_summary(checkpoints: dict, score: float) -> str:
        if not checkpoints:
            return f"Functional score: {score:.0%}"
        passed = [k for k, v in checkpoints.items() if _is_passed(v)]
        failed = [k for k, v in checkpoints.items() if not _is_passed(v)]
        parts = [f"{score:.0%} functional"]
        if passed:
            parts.append(f"passed: {', '.join(passed)}")
        if failed:
            parts.append(f"failed: {', '.join(failed)}")
        return " · ".join(parts)

    @staticmethod
    def _extract_failure_line(stdout: str, stderr: str) -> str:
        combined = f"{stdout}\n{stderr}"
        for line in combined.splitlines():
            if line.strip().startswith("FAIL:"):
                return line.strip()
        for line in combined.splitlines():
            if "failed" in line and ("passed" in line or "error" in line):
                return line.strip()
        tail = [ln for ln in stderr.splitlines() if ln.strip()]
        if tail:
            return tail[-1].strip()
        return "Verification failed (exit code nonzero)"

    def _find_verify_script(self) -> Path | None:
        task_dir = self.task.task_dir
        if task_dir is None:
            return None
        candidates = [
            task_dir / "verify" / "verify.sh",
            task_dir / "verify.sh",
            task_dir / "verify" / "verify.py",
            task_dir / "verify.py",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _find_pytest_dir(self) -> Path | None:
        task_dir = self.task.task_dir
        if task_dir is None:
            return None
        for d in (task_dir / "verify", task_dir):
            if d.exists() and any(d.glob("test_*.py")):
                return d
        return None


def _is_passed(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("passed", False))
    return bool(value)


def build_marker_result(
    exit_code: int, stdout: str, stderr: str, duration: float = 0.0
) -> FunctionalTestResult:
    """Turn a verification's output into a FunctionalTestResult.

    Prefers a structured ``KIROBENCH_RESULT`` marker (graduated score +
    checkpoints); falls back to a binary score from the exit code. Shared by the
    verify-script path (FunctionalEvaluator) and the centralized local runner
    (ScriptVerifyRunner)."""
    marker = FunctionalEvaluator._parse_marker(stdout) or FunctionalEvaluator._parse_marker(stderr)
    if marker is not None:
        score = max(0.0, min(1.0, float(marker.get("score", 0.0))))
        checkpoints = marker.get("checkpoints", {}) or {}
        summary = marker.get("summary", "") or FunctionalEvaluator._auto_summary(checkpoints, score)
        return FunctionalTestResult(
            passed=score >= 1.0, exit_code=exit_code, score=score,
            checkpoints=checkpoints, summary=summary,
            stdout=stdout, stderr=stderr, duration_seconds=duration,
        )

    passed = exit_code == 0
    summary = (
        FunctionalEvaluator._extract_failure_line(stdout, stderr)
        if not passed else "All checks passed"
    )
    return FunctionalTestResult(
        passed=passed, exit_code=exit_code, score=1.0 if passed else 0.0,
        checkpoints={}, summary=summary,
        stdout=stdout, stderr=stderr, duration_seconds=duration,
    )
