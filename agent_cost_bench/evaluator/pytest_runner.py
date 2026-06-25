"""
Centralised, robust pytest-based functional verification.

A functional task can ship a pytest file at ``verify/test_*.py``. This module
runs that suite against the model's workspace and turns the result into a
graduated 0.0–1.0 score (fraction of harness tests that pass), with all of the
"make it robust so only real model failures show up" machinery in one place:

  * Isolated virtualenv per run (inherits system site-packages for breadth;
    venv-local installs win for pinned harness-critical packages).
  * Alembic schema provisioning when migrations are present.
  * Database-driver retry across a few DB configs (sync sqlite, async sqlite,
    model default) so an app wired to the conventional DATABASE_URL still boots.
  * Workspace-scoped app discovery (the test files reject out-of-workspace
    modules).

Tasks needing bespoke scoring can still ship verify.sh / verify.py printing a
AGENT_COST_BENCH_RESULT marker — the FunctionalEvaluator prefers that when present.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from ..models import FunctionalTestResult, TaskConfig

_STANDARD_ENV = {
    "JWT_SECRET_KEY": "agent_cost_bench-test-secret-key-not-for-production",
    "SECRET_KEY": "agent_cost_bench-test-secret-key-not-for-production",
    "JWT_SECRET": "agent_cost_bench-test-secret-key-not-for-production",
    "JWT_ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "15",
    "REFRESH_TOKEN_EXPIRE_DAYS": "7",
    "ENVIRONMENT": "test",
    "TESTING": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}

_HARNESS_DEPS = [
    "fastapi", "uvicorn", "starlette", "httpx",
    "pydantic", "pydantic-settings",
    "sqlalchemy", "aiosqlite",
    "passlib[bcrypt]", "bcrypt<4.1", "python-jose[cryptography]",
    "pytest>=8,<9", "pytest-asyncio", "pytest-json-report", "anyio", "trio",
    "alembic",
]

_DB_CONFIGS = [
    {
        "name": "sqlite-sync+async",
        "set": {
            "DATABASE_URL": "sqlite:///./agent_cost_bench.db",
            "ASYNC_DATABASE_URL": "sqlite+aiosqlite:///./agent_cost_bench.db",
            "SYNC_DATABASE_URL": "sqlite:///./agent_cost_bench.db",
            "TEST_DATABASE_URL": "sqlite:///./agent_cost_bench.db",
        },
    },
    {
        "name": "async-as-default",
        "set": {
            "DATABASE_URL": "sqlite+aiosqlite:///./agent_cost_bench.db",
            "ASYNC_DATABASE_URL": "sqlite+aiosqlite:///./agent_cost_bench.db",
        },
    },
    {"name": "model-default", "unset": [
        "DATABASE_URL", "ASYNC_DATABASE_URL", "SYNC_DATABASE_URL", "TEST_DATABASE_URL"]},
]


class PytestSuiteRunner:
    def __init__(
        self,
        task: TaskConfig,
        workspace: Path,
        verify_dir: Path,
        extra_deps: list[str] | None = None,
        logger=None,
    ):
        self.task = task
        self.workspace = workspace
        self.verify_dir = verify_dir
        # Per-task pinned deps added on top of _HARNESS_DEPS (installed last so
        # they override the unpinned harness defaults).
        self.extra_deps: list[str] = extra_deps or []
        self._logger = logger

    # File extensions considered "source" — anything the model could produce
    # beyond Python. Harness files (test_*.py, harness_*.py) are excluded below.
    _SOURCE_EXTS = {
        ".py", ".yaml", ".yml", ".tf", ".json", ".js", ".ts", ".tsx", ".jsx",
        ".java", ".cs", ".go", ".rb", ".sh", ".html", ".css", ".sql",
        ".toml", ".ini", ".cfg", ".dockerfile",
    }
    _SOURCE_NAMES = {"Dockerfile", "Makefile"}

    def _code_exists(self) -> bool:
        skip = {".kiro", ".agent_cost_bench_venv", "__pycache__", ".git", ".venv", "node_modules"}
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in skip]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if f in self._SOURCE_NAMES:
                    return True
                if ext not in self._SOURCE_EXTS:
                    continue
                # Skip harness/test files
                if f.startswith(("test_", "harness_")) or f in ("score.py",):
                    continue
                # Skip lock files
                if f in ("package-lock.json", "yarn.lock", "go.sum", "poetry.lock"):
                    continue
                return True
        return False

    async def run(self) -> FunctionalTestResult:
        start = time.monotonic()
        test_files = sorted(str(p) for p in self.verify_dir.glob("test_*.py"))
        deps_note = f"  deps: {self.extra_deps}" if self.extra_deps else ""
        if self._logger:
            await self._logger.log_event(
                f"VERIFY START  {self.task.id}  (pytest)\n"
                f"    tests: {[str(Path(f).name) for f in test_files]}\n"
                f"    verify_dir: {self.verify_dir}{deps_note}"
            )
        py = await self._ensure_venv()
        if py is None:
            return FunctionalTestResult(
                passed=False, exit_code=-1, score=0.0,
                summary="Could not create an isolated environment for verification "
                        "— harness error, not a model failure",
                checkpoints={"harness_error": True},
                stderr="venv/uv setup failed",
                duration_seconds=time.monotonic() - start,
            )

        best: dict | None = None
        for cfg in _DB_CONFIGS:
            attempt = await self._run_suite(py, cfg)
            if best is None or attempt["passed"] > best["passed"]:
                best = attempt
            if attempt["boot_ok"] and (cfg is _DB_CONFIGS[0] or attempt["passed"] == attempt["total"]):
                break

        result = self._build_result(best, self._code_exists(), time.monotonic() - start)
        if self._logger:
            await self._logger.log_event(
                f"VERIFY END    {self.task.id}  (pytest)  "
                f"score={result.score:.2f}  passed={result.passed}  "
                f"summary={result.summary[:120]}"
            )
        return result

    async def _ensure_venv(self) -> str | None:
        venv_dir = self.workspace / ".agent_cost_bench_venv"
        py = venv_dir / "bin" / "python"
        marker = venv_dir / ".ready"
        if marker.exists() and py.exists():
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
        await self._pip(have_uv, str(py), _HARNESS_DEPS)
        # Per-task overrides installed last so pinned versions win.
        if self.extra_deps:
            await self._pip(have_uv, str(py), self.extra_deps)

        marker.write_text("ok", encoding="utf-8")
        return str(py)

    async def _pip(self, have_uv: bool, py: str, args: list[str]) -> bool:
        if have_uv:
            return await self._sh(["uv", "pip", "install", "-q", "--python", py, *args])
        return await self._sh([py, "-m", "pip", "install", "-q", "--timeout", "60", *args])

    async def _sh(self, cmd, cwd=None, env=None, timeout: float = 300.0) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd or self.workspace),
                env=env,
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

    def _build_env(self, cfg: dict) -> dict[str, str]:
        env = os.environ.copy()
        env.update(_STANDARD_ENV)
        env["WORKSPACE"] = str(self.workspace)
        # Allow test files to `import main` (or any model module) without path
        # manipulation — the workspace is prepended to PYTHONPATH.
        env["PYTHONPATH"] = str(self.workspace) + os.pathsep + env.get("PYTHONPATH", "")
        # Expose the task directory so test files can locate fixture data (seed
        # files, sample logs, etc.) via TASK_DIR.
        if self.task.task_dir is not None:
            env["TASK_DIR"] = str(self.task.task_dir)
        for k, v in cfg.get("set", {}).items():
            env[k] = v
        for k in cfg.get("unset", []):
            env.pop(k, None)
        return env

    async def _run_suite(self, py: str, cfg: dict) -> dict:
        env = self._build_env(cfg)
        for db in self.workspace.glob("*.db"):
            try:
                db.unlink()
            except OSError:
                pass

        if any((self.workspace / n).exists() for n in ("alembic.ini", "migrations", "alembic")):
            await self._sh([py, "-m", "alembic", "upgrade", "head"], env=env, timeout=90.0)

        report_file = Path(tempfile.mkdtemp()) / "report.json"
        test_files = sorted(str(p) for p in self.verify_dir.glob("test_*.py"))
        cmd = [
            py, "-m", "pytest", "-q", "-p", "no:anyio", "--no-header",
            "--json-report", f"--json-report-file={report_file}",
            *test_files,
        ]
        await self._sh(cmd, env=env, timeout=240.0)
        return self._parse_report(report_file)

    @staticmethod
    def _parse_report(report_file: Path) -> dict:
        passed = total = 0
        failed: list[str] = []
        passed_names: list[str] = []
        boot_ok = False
        detail = "suite did not run"
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
            tests = data.get("tests", [])
            non_error = 0
            for t in tests:
                outcome = t.get("outcome")
                name = t.get("nodeid", "").split("::")[-1]
                total += 1
                if outcome == "passed":
                    passed += 1
                    non_error += 1
                    passed_names.append(name)
                else:
                    failed.append(name)
                    if outcome != "error":
                        non_error += 1
            boot_ok = total > 0 and non_error > 0
            if total == 0:
                collectors = data.get("collectors", [])
                errs = [c.get("longrepr", "") for c in collectors if c.get("outcome") == "failed"]
                detail = errs[0][:300] if errs else "no tests collected (app failed to import)"
            else:
                detail = "app imported and tests ran" if boot_ok else "app failed to import / start"
        except Exception as e:
            detail = f"could not read pytest report: {e}"
        return {
            "passed": passed, "total": total,
            "failed": failed, "passed_names": passed_names,
            "boot_ok": boot_ok, "detail": detail,
        }

    def _build_result(self, best, code_exists, duration) -> FunctionalTestResult:
        if not best:
            return FunctionalTestResult(
                passed=False, exit_code=-1, score=0.0,
                summary="Verification did not run", duration_seconds=duration,
                checkpoints={"code_exists": {"passed": code_exists,
                                             "detail": "source files present" if code_exists else "no source files produced"}},
            )
        total = best["total"]
        passed = best["passed"]
        boot_ok = best["boot_ok"]
        score = round(passed / total, 4) if total else 0.0

        checkpoints = {
            "code_exists": {
                "passed": code_exists,
                "detail": "source files present" if code_exists else "no source files produced",
            },
            "app_boots": {"passed": boot_ok, "detail": best["detail"]},
            "tests": {
                "passed": total > 0 and passed == total,
                "ratio": (passed / total) if total else 0.0,
                "detail": f"{passed}/{total} tests passed",
                "failed": best["failed"][:20],
                "passed_names": best.get("passed_names", [])[:40],
            },
        }
        if not code_exists:
            summary = "No source files produced — model did not complete the task"
        elif not boot_ok:
            summary = f"App failed to boot ({best['detail']})"
        elif passed == total:
            summary = f"100% functional — {passed}/{total} tests passed"
        else:
            summary = f"{score:.0%} functional — {passed}/{total} tests passed"

        return FunctionalTestResult(
            passed=(total > 0 and passed == total),
            exit_code=0 if boot_ok else 1,
            score=score, checkpoints=checkpoints, summary=summary,
            duration_seconds=duration,
        )
