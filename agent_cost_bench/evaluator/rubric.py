"""
Rubric (checklist) evaluator — no-code quality grading.

When a task carries a ``quality.rubric`` (a list of plain-English acceptance
criteria) and has no code-based verification, the LLM judge grades the produced
files against each criterion. The functional score is the fraction of criteria
met, and each criterion becomes a checkpoint in the report.

This lets a non-programmer author a task by writing only a prompt plus a
checklist — no verify.sh, no hidden tests. Requires ``config.judge_model``.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from ..judge import LLMJudge, rubric_prompt
from ..models import BenchConfig, FunctionalTestResult, TaskConfig
from ..sandbox import pristine_baseline

# File types we show the judge as "the submission".
_INCLUDE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rs", ".rb", ".cs",
    ".cpp", ".c", ".h", ".php", ".swift", ".kt", ".scala", ".sh", ".sql",
    ".html", ".css", ".vue", ".svelte",
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".tf", ".dockerfile",
}
# Extension-less files worth including by name.
_INCLUDE_NAMES = {"Dockerfile", "Makefile", "requirements.txt", "go.mod", "Cargo.toml"}

# Specific filenames to always skip — large generated/lock files that add no
# grading signal and quickly consume the whole file budget.
_SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Gemfile.lock",
    "Cargo.lock", "poetry.lock", "composer.lock", "Pipfile.lock",
    "go.sum", ".DS_Store", "LICENSE", "LICENCE",
}

_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", "dist", "build",
    ".kiro", ".pytest_cache", "target", "bin", "obj", ".idea", ".vscode",
}

def _is_hidden_dir(name: str) -> bool:
    """True for hidden directories (start with '.') — version-control metadata,
    IDE config, and vendor context docs that are never model-produced code."""
    return name.startswith(".")

_MAX_FILES = 60
_MAX_FILE_CHARS = 6000
_MAX_TOTAL_CHARS = 80000

# Overall safety cap for changeset (diff) mode. Unlike the per-file cap above,
# this bounds only the model's *diff*, not whole files — normal changes are far
# smaller, so it exists purely to protect the judge's context window from a
# pathologically large change.
_MAX_DIFF_CHARS = 60000


class RubricEvaluator:
    def __init__(
        self,
        task: TaskConfig,
        workspace_path: Path,
        config: BenchConfig,
        logger=None,
        model_label: str = "",
    ):
        self.task = task
        self.workspace = workspace_path
        self.config = config
        self._logger = logger
        self._judge = LLMJudge(config, logger=logger, task_id=task.id, model_label=model_label)

    async def evaluate(self) -> FunctionalTestResult:
        criteria = list(self.task.quality.rubric) if self.task.quality else []
        if not criteria:
            return self._harness_error(
                "Rubric task has no criteria (quality.rubric is empty)."
            )
        if not self._judge.enabled:
            return self._harness_error(
                "Rubric grading requires an LLM judge, but no judge_model is configured. "
                "Set judge_model in the model-compare config (or use --judge-model)."
            )

        # Prefer grading the model's CHANGESET (a diff vs. the pristine baseline)
        # so the judge sees the full change regardless of file size. Fall back to
        # whole-file collection when there is no baseline (greenfield) or git is
        # unavailable.
        changeset = await self._collect_changeset()
        if changeset is None:
            submission_blob = self._collect_files(self.workspace)
            is_diff = False
        else:
            submission_blob = changeset
            is_diff = True

        if not submission_blob:
            detail = (
                "no changes were made relative to the baseline"
                if is_diff
                else "no files were produced in the workspace"
            )
            return FunctionalTestResult(
                passed=False,
                exit_code=-1,
                score=0.0,
                summary=f"Nothing to grade — {detail}.",
                checkpoints={"code_exists": {"passed": False, "detail": detail}},
            )

        reference_blob = self._collect_reference()
        prompt = rubric_prompt(criteria, submission_blob, reference_blob, is_diff=is_diff)
        result = await self._judge.score(prompt, phase="judge:rubric")
        if not result.ok:
            return self._harness_error(f"Rubric judge call failed: {result.error}")

        return self._build_result(criteria, result)

    # ------------------------------------------------------------------

    async def _collect_changeset(self) -> str | None:
        """Unified diff of the model's changes vs. the pristine cached baseline.

        Returns the filtered diff text, ``""`` when the model changed nothing, or
        ``None`` when no baseline/git is available (the caller then falls back to
        whole-file collection). The baseline lives in the read-only repo cache,
        outside the model-controlled workspace, so the model cannot corrupt it."""
        if shutil.which("git") is None:
            return None
        baseline = pristine_baseline(self.task, self.config)
        if baseline is None:
            return None

        cmd = [
            "git", "diff", "--no-index", "--unified=15",
            str(baseline), str(self.workspace),
        ]
        try:
            # Security: cmd is ["git", "diff", "--no-index", ...] with static
            # flags and harness-controlled paths. Array-based exec, no shell.
            proc = await asyncio.create_subprocess_exec(  # noqa: S603
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except Exception:
            return None
        # git diff --no-index: 0 = identical, 1 = differences found, >1 = error.
        if proc.returncode not in (0, 1):
            return None
        if self._logger:
            await self._logger.log_event(
                f"RUBRIC DIFF  {self.task.id}  baseline={baseline}"
            )
        return self._filter_diff(out_b.decode("utf-8", errors="replace"))

    def _filter_diff(self, diff: str) -> str:
        """Drop diff sections for vendored/generated/noise paths and enforce the
        overall size cap. Path matching is by exact path *segment* so substrings
        (e.g. 'bin' inside 'cabinet') don't cause false drops."""
        if not diff.strip():
            return ""

        # Split into per-file sections (each starts with a 'diff --git'/'--no-index' line).
        sections: list[str] = []
        current: list[str] = []
        for line in diff.splitlines(keepends=True):
            if line.startswith(("diff --git ", "diff --no-index")):
                if current:
                    sections.append("".join(current))
                    current = []
            current.append(line)
        if current:
            sections.append("".join(current))

        kept: list[str] = []
        total = 0
        for sec in sections:
            header = sec.splitlines()[0] if sec else ""
            tokens = set(re.split(r"[\s/]+", header))
            if tokens & _SKIP_DIRS or tokens & _SKIP_NAMES:
                continue
            if total + len(sec) > _MAX_DIFF_CHARS:
                kept.append(f"\n… [diff truncated: exceeds {_MAX_DIFF_CHARS} chars]\n")
                break
            kept.append(sec)
            total += len(sec)
        return "".join(kept).strip()

    def _build_result(self, criteria: list[str], judge_result) -> FunctionalTestResult:
        verdicts = judge_result.extra.get("criteria")
        checkpoints: dict = {"code_exists": {"passed": True, "detail": "files produced"}}

        if isinstance(verdicts, list) and verdicts:
            met = 0
            for i, criterion in enumerate(criteria):
                verdict = verdicts[i] if i < len(verdicts) else {}
                is_met = bool(verdict.get("met")) if isinstance(verdict, dict) else False
                reasoning = (
                    str(verdict.get("reasoning", "")) if isinstance(verdict, dict) else ""
                )
                if is_met:
                    met += 1
                checkpoints[f"criterion_{i + 1}"] = {
                    "passed": is_met,
                    "criterion": criterion,
                    "detail": reasoning[:300],
                }
            score = met / len(criteria)
            summary = f"{score:.0%} rubric — {met}/{len(criteria)} criteria met"
        else:
            # Judge returned no per-criterion breakdown; fall back to its holistic
            # score so a slightly-off response still yields a usable number.
            score = judge_result.score
            checkpoints["judge_holistic"] = {
                "passed": False,
                "detail": "judge did not return per-criterion verdicts; used holistic score",
            }
            summary = f"{score:.0%} rubric (holistic — no per-criterion verdicts returned)"

        return FunctionalTestResult(
            passed=score >= 1.0,
            exit_code=0,
            score=max(0.0, min(1.0, score)),
            checkpoints=checkpoints,
            summary=summary,
            stdout=(judge_result.reasoning or "")[:1000],
        )

    def _collect_files(self, root: Path) -> str:
        chunks: list[str] = []
        total = 0
        count = 0
        for path in sorted(root.rglob("*")):
            if count >= _MAX_FILES or total >= _MAX_TOTAL_CHARS:
                break
            if not path.is_file():
                continue
            if any(
                part in _SKIP_DIRS or part.startswith(".venv") or _is_hidden_dir(part)
                for part in path.parts
            ):
                continue
            if path.name in _SKIP_NAMES:
                continue
            if path.suffix.lower() not in _INCLUDE_EXTS and path.name not in _INCLUDE_NAMES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            rel = path.relative_to(root)
            snippet = text[:_MAX_FILE_CHARS]
            if len(text) > _MAX_FILE_CHARS:
                snippet += f"\n… [{len(text) - _MAX_FILE_CHARS} chars truncated]"
            chunks.append(f"### {rel}\n```\n{snippet}\n```")
            total += len(snippet)
            count += 1
        return "\n\n".join(chunks)

    def _collect_reference(self) -> str | None:
        if not (self.task.quality and self.task.quality.reference_solution):
            return None
        if self.task.task_dir is None:
            return None
        task_dir = self.task.task_dir.resolve()
        ref_path = (task_dir / self.task.quality.reference_solution).resolve()
        # Containment guard: a task.yaml could set reference_solution to
        # '../../../etc/passwd' and we'd otherwise read it and hand it to the
        # judge. Refuse anything that resolves outside the task directory.
        if not (ref_path == task_dir or ref_path.is_relative_to(task_dir)):
            return None
        if not ref_path.exists():
            return None
        if ref_path.is_file():
            try:
                return ref_path.read_text(encoding="utf-8", errors="replace")[:_MAX_TOTAL_CHARS]
            except Exception:
                return None
        return self._collect_files(ref_path) or None

    def _harness_error(self, message: str) -> FunctionalTestResult:
        return FunctionalTestResult(
            passed=False,
            exit_code=-1,
            score=0.0,
            summary=message,
            checkpoints={"harness_error": True},
            stderr=message,
        )
