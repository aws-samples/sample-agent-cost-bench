"""
Generic Docker verify runner: executes a task's declarative ``VerifySpec`` and
returns a ``FunctionalTestResult``.

It resolves a daemon that has the image, mounts the model's ``src/`` (read-only)
and the task's authoritative tests (read-only), copies them into an ephemeral
``/build`` inside the container, runs ``setup`` then ``test_cmd`` (writing reports
to ``$RESULTS_DIR``), then parses the report on the host with the named parser.
Nothing is written to the host workspace, so no root-owned files leak out.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from ..models import FunctionalTestResult, TaskConfig
from .docker_env import docker_available, resolve_docker_env
from .parsers import parse_results

_CONTAINER_SRC = "/src-ro"
_CONTAINER_TESTS = "/tests-ro"
_CONTAINER_BUILD = "/build"
_CONTAINER_RESULTS = "/out"


class DockerVerifyRunner:
    def __init__(self, task: TaskConfig, workspace: Path, logger=None):
        self.task = task
        self.workspace = Path(workspace)
        self._logger = logger

    def _wrapper_script(self) -> str:
        spec = self.task.verify
        setup = "\n".join(spec.setup)
        workdir = spec.workdir or ""
        # Copy the model's src into a writable build dir; run setup; run tests.
        return f"""set -u
mkdir -p "{_CONTAINER_BUILD}/src"
cp -r {_CONTAINER_SRC}/. "{_CONTAINER_BUILD}/src" 2>/dev/null || true
cd "{_CONTAINER_BUILD}" || exit 97
{setup}
cd "{_CONTAINER_BUILD}/{workdir}" 2>/dev/null || cd "{_CONTAINER_BUILD}"
{spec.test_cmd}
ec=$?
chmod -R a+rwX "{_CONTAINER_RESULTS}" 2>/dev/null || true
exit $ec
"""

    async def run(self) -> FunctionalTestResult:
        spec = self.task.verify
        start = time.monotonic()

        if shutil.which("docker") is None:
            return self._harness_error("Docker not installed on host (required for this task)")

        env = resolve_docker_env(spec.image)
        if env is None:
            if docker_available():
                msg = (
                    f"Image {spec.image} not found on any reachable Docker daemon — "
                    f"run tasks/docker/build-images.sh"
                )
            else:
                msg = "Docker daemon not reachable from the verify subprocess"
            return self._harness_error(msg)

        src_dir = self.workspace / "src"
        if not src_dir.is_dir():
            return self._harness_error("No src/ in workspace — model produced no code")

        tests_host = None
        if spec.tests_subdir and self.task.task_dir is not None:
            candidate = self.task.task_dir / spec.tests_subdir
            if candidate.exists():
                tests_host = candidate

        results_dir = Path(tempfile.mkdtemp(prefix="agent_cost_bench-verify-"))
        timeout = spec.timeout_seconds or min(self.task.timeout_minutes * 60, 600)

        cmd = [
            "docker", "run", "--rm", f"--network={spec.network}",
            "-v", f"{src_dir}:{_CONTAINER_SRC}:ro",
            "-v", f"{results_dir}:{_CONTAINER_RESULTS}",
            "-e", f"SRC_RO={_CONTAINER_SRC}",
            "-e", f"TESTS_RO={_CONTAINER_TESTS}",
            "-e", f"BUILD={_CONTAINER_BUILD}",
            "-e", f"RESULTS_DIR={_CONTAINER_RESULTS}",
        ]
        if tests_host is not None:
            cmd[6:6] = ["-v", f"{tests_host}:{_CONTAINER_TESTS}:ro"]
        cmd += [spec.image, "bash", "-c", self._wrapper_script()]

        if self._logger:
            await self._logger.log_event(
                f"VERIFY START  {self.task.id}  image={spec.image}  parser={spec.parser}\n"
                f"    test_cmd: {spec.test_cmd}"
            )

        try:
            # Security: cmd is ["docker", "run", ...] with image name from
            # task.yaml (operator-owned) and static docker flags. No shell.
            proc = await asyncio.create_subprocess_exec(  # noqa: S603
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return self._harness_error(
                    f"Verification timed out after {timeout}s", duration=time.monotonic() - start
                )

            exit_code = proc.returncode or 0
            stdout = out_b.decode("utf-8", errors="replace")
            stderr = err_b.decode("utf-8", errors="replace")
            duration = time.monotonic() - start

            result = self._build_result(spec, results_dir, stdout, stderr, exit_code, duration)

            if self._logger:
                await self._logger.log_call(
                    task_id=self.task.id, target="verify", phase="verify",
                    command=cmd[:-1] + ["<wrapper>"], prompt=f"image={spec.image}",
                    stdout=stdout, stderr=stderr, exit_code=exit_code, duration_seconds=duration,
                )
            return result
        except Exception as e:  # pragma: no cover - defensive
            return self._harness_error(f"Verification error: {e}", duration=time.monotonic() - start)
        finally:
            shutil.rmtree(results_dir, ignore_errors=True)

    # ------------------------------------------------------------------

    def _build_result(self, spec, results_dir, stdout, stderr, exit_code, duration):
        pr = parse_results(spec.parser, results_dir, stdout, stderr, exit_code, spec)
        if not pr.ran and pr.total == 0:
            # No results produced — almost always a compile/build failure.
            tail = (stderr or stdout)[-600:].replace("\n", " ")
            return FunctionalTestResult(
                passed=False, exit_code=exit_code, score=0.0,
                checkpoints={"compiles": False, "tests": {"passed": 0, "total": 0}},
                summary=f"no test results produced ({pr.detail}): {tail}",
                stdout=stdout, stderr=stderr, duration_seconds=duration,
            )
        return FunctionalTestResult(
            passed=(pr.total > 0 and pr.passed == pr.total),
            exit_code=exit_code,
            score=pr.score,
            checkpoints={
                "compiles": True,
                "tests": {"passed": pr.passed, "total": pr.total, "failed": pr.failed_names},
            },
            summary=pr.detail,
            stdout=stdout, stderr=stderr, duration_seconds=duration,
        )

    def _harness_error(self, message: str, duration: float = 0.0) -> FunctionalTestResult:
        return FunctionalTestResult(
            passed=False, exit_code=-1, score=0.0,
            checkpoints={"harness_error": True},
            summary=message, stderr=message, duration_seconds=duration,
        )
