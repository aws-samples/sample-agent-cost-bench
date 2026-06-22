"""
Centralized local (host) verify runner.

Replaces the per-task ``verify.sh`` boilerplate: the framework creates an
isolated virtualenv in the workspace, installs the task's declared ``deps``
(plus the model's ``requirements.txt`` if present), then runs the task's
``score`` script, which prints a ``KIROBENCH_RESULT`` marker. The marker is
parsed into a graduated FunctionalTestResult by the shared ``build_marker_result``.

Selected when a task's ``verify:`` block has ``runner: local`` (the default when
no Docker ``image`` is set). The task ships only ``verify/score.py`` and a small
``verify:`` block — no shell.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import time
from pathlib import Path

from ..models import FunctionalTestResult, TaskConfig
from .functional import _STANDARD_ENV, build_marker_result


class ScriptVerifyRunner:
    def __init__(self, task: TaskConfig, workspace: Path, logger=None):
        self.task = task
        self.workspace = Path(workspace)
        self._logger = logger

    async def run(self) -> FunctionalTestResult:
        spec = self.task.verify
        start = time.monotonic()

        if self.task.task_dir is None or not spec.score:
            return self._harness_error("local verify requires a 'score' script and a task dir")
        scorer = self.task.task_dir / spec.score
        if not scorer.exists():
            return self._harness_error(f"verify score script not found: {spec.score}")

        py = await self._ensure_venv(list(spec.deps))
        if py is None:
            return self._harness_error(
                "could not create an isolated verify venv — harness error, not a model failure"
            )

        env = os.environ.copy()
        env.update(_STANDARD_ENV)
        env["WORKSPACE"] = str(self.workspace)
        env["TASK_DIR"] = str(self.task.task_dir)
        # Put the venv's bin first on PATH so console scripts (uvicorn, pytest,
        # alembic, …) and `python` resolve to the isolated verify venv.
        venv_bin = str(Path(py).parent)
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(Path(py).parent.parent)

        # Scorers read the workspace as argv[1]; some also read the task dir as
        # argv[2] (e.g. to diff a seed file). Passing both is harmless to the rest.
        cmd = [py, str(scorer), str(self.workspace), str(self.task.task_dir)]
        timeout = min(self.task.timeout_minutes * 60, 300)

        if self._logger:
            await self._logger.log_event(
                f"VERIFY START  {self.task.id}  (local)\n"
                f"    score: {spec.score}\n    deps: {', '.join(spec.deps) or '(none)'}"
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=env,
                start_new_session=True,
            )
            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(), timeout=float(timeout)
                )
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                await proc.communicate()
                return FunctionalTestResult(
                    passed=False, exit_code=-1, score=0.0,
                    summary=f"Verification timed out after {timeout}s",
                    duration_seconds=time.monotonic() - start,
                    stderr=f"Verification timed out after {timeout}s",
                )

            exit_code = proc.returncode or 0
            stdout = out_b.decode("utf-8", errors="replace")
            stderr = err_b.decode("utf-8", errors="replace")
            duration = time.monotonic() - start

            if self._logger:
                await self._logger.log_call(
                    task_id=self.task.id, target="verify", phase="verify",
                    command=cmd, prompt=f"WORKSPACE={self.workspace}",
                    stdout=stdout, stderr=stderr, exit_code=exit_code, duration_seconds=duration,
                )
            return build_marker_result(exit_code, stdout, stderr, duration)

        except Exception as e:
            return self._harness_error(f"Error running local verification: {e}")

    # ------------------------------------------------------------------

    async def _ensure_venv(self, deps: list[str]) -> str | None:
        venv_dir = self.workspace / ".venv-verify"
        py = venv_dir / "bin" / "python"
        ready = venv_dir / ".ready"
        if ready.exists() and py.exists():
            return str(py)

        have_uv = shutil.which("uv") is not None
        if have_uv:
            ok = await self._sh(["uv", "venv", "--system-site-packages", str(venv_dir)])
        else:
            ok = await self._sh(["python3", "-m", "venv", "--system-site-packages", str(venv_dir)])
        if not ok or not py.exists():
            return None

        req = self.workspace / "requirements.txt"
        if req.exists():
            await self._pip(have_uv, str(py), ["-r", str(req)])
        if deps:
            await self._pip(have_uv, str(py), deps)

        ready.write_text("ok", encoding="utf-8")
        return str(py)

    async def _pip(self, have_uv: bool, py: str, args: list[str]) -> bool:
        if have_uv:
            return await self._sh(["uv", "pip", "install", "-q", "--python", py, *args])
        return await self._sh([py, "-m", "pip", "install", "-q", "--timeout", "60", *args])

    async def _sh(self, cmd, timeout: float = 300.0) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.workspace),
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return False
            return proc.returncode == 0
        except Exception:
            return False

    def _harness_error(self, message: str) -> FunctionalTestResult:
        return FunctionalTestResult(
            passed=False, exit_code=-1, score=0.0,
            checkpoints={"harness_error": True},
            summary=message, stderr=message,
        )
