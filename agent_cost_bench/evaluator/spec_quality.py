"""
Spec artifact quality evaluator.

Scores requirements.md, design.md, and tasks.md on multiple rule-based
dimensions. When ``config.judge_model`` is set, an LLM-as-judge (routed through
the Kiro CLI) scores each artifact too and the two are blended via
``config.judge_weight``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..judge import LLMJudge, design_prompt, requirements_prompt, tasks_prompt
from ..models import BenchConfig, SpecArtifactScores, TaskConfig


class SpecQualityEvaluator:
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

    async def evaluate(self) -> SpecArtifactScores:
        from .spec_paths import resolve_spec_dir

        specs_dir = resolve_spec_dir(self.workspace, self.task.id)
        req_text = self._read(specs_dir / "requirements.md")
        design_text = self._read(specs_dir / "design.md")
        tasks_text = self._read(specs_dir / "tasks.md")

        req_scores = self._score_requirements(req_text) if req_text else {}
        design_scores = self._score_design(design_text) if design_text else {}
        tasks_scores = self._score_tasks(tasks_text) if tasks_text else {}

        req_rule = self._avg(req_scores)
        design_rule = self._avg(design_scores)
        tasks_rule = self._avg(tasks_scores)

        details: dict[str, Any] = {
            "requirements": req_scores,
            "design": design_scores,
            "tasks": tasks_scores,
            "artifacts_present": {
                "requirements.md": req_text is not None,
                "design.md": design_text is not None,
                "tasks.md": tasks_text is not None,
            },
        }

        req_score, design_score, tasks_score = req_rule, design_rule, tasks_rule
        if self._judge.enabled:
            judge_details: dict[str, Any] = {}
            if req_text:
                req_score, jd = await self._judge_artifact(
                    requirements_prompt(req_text), req_rule, phase="judge:requirements"
                )
                judge_details["requirements"] = jd
            if design_text:
                design_score, jd = await self._judge_artifact(
                    design_prompt(design_text), design_rule, phase="judge:design"
                )
                judge_details["design"] = jd
            if tasks_text:
                tasks_score, jd = await self._judge_artifact(
                    tasks_prompt(tasks_text), tasks_rule, phase="judge:tasks"
                )
                judge_details["tasks"] = jd
            details["llm_judge"] = judge_details

        return SpecArtifactScores(
            requirements_score=req_score,
            design_score=design_score,
            tasks_score=tasks_score,
            details=details,
        )

    async def _judge_artifact(self, prompt, rule_score, phase="judge"):
        result = await self._judge.score(prompt, phase=phase)
        if not result.ok:
            return rule_score, {"ok": False, "error": result.error, "used": "rule_only"}
        w = self.config.judge_weight
        blended = (1 - w) * rule_score + w * result.score
        return blended, {
            "ok": True,
            "judge_score": result.score,
            "rule_score": rule_score,
            "blended": blended,
            "weight": w,
            "reasoning": result.reasoning[:300],
            "credits": result.credits,
        }

    # ------------------------------------------------------------------
    # Rule-based scorers
    # ------------------------------------------------------------------

    def _score_requirements(self, text: str) -> dict[str, float]:
        scores: dict[str, float] = {}
        ears_matches = re.findall(r"WHEN .+? THE SYSTEM SHALL .+", text, re.IGNORECASE)
        scores["ears_compliance"] = min(len(ears_matches) / 5.0, 1.0)
        user_stories = re.findall(r"as a .+?,?\s+i want", text, re.IGNORECASE)
        scores["has_user_stories"] = min(len(user_stories) / 3.0, 1.0)
        scores["has_acceptance_criteria"] = (
            1.0 if re.search(r"acceptance criteria", text, re.IGNORECASE) else 0.0
        )
        scores["has_nfr"] = (
            1.0 if re.search(
                r"non.functional|performance|security|scalability|reliability", text, re.IGNORECASE
            ) else 0.0
        )
        req_items = re.findall(r"^\s*[-*\d]+[\.\)]\s+.{10,}", text, re.MULTILINE)
        scores["has_structured_requirements"] = min(len(req_items) / 5.0, 1.0)
        scores["sufficient_length"] = 1.0 if len(text.strip()) >= 200 else len(text.strip()) / 200.0
        return scores

    def _score_design(self, text: str) -> dict[str, float]:
        scores: dict[str, float] = {}
        mermaid_blocks = re.findall(r"```mermaid", text, re.IGNORECASE)
        scores["has_diagrams"] = min(len(mermaid_blocks) / 1.0, 1.0)
        scores["has_sequence_diagram"] = (
            1.0 if re.search(r"sequenceDiagram", text, re.IGNORECASE) else 0.0
        )
        headers = re.findall(r"^#{2,3}\s+.+", text, re.MULTILINE)
        scores["has_sections"] = min(len(headers) / 3.0, 1.0)
        scores["has_error_handling"] = (
            1.0 if re.search(r"error handling|error cases|failure", text, re.IGNORECASE) else 0.0
        )
        scores["has_security"] = (
            1.0 if re.search(r"security|authentication|authorization|auth", text, re.IGNORECASE) else 0.0
        )
        scores["has_data_models"] = (
            1.0 if re.search(
                r"data model|schema|interface|type\s+\w+\s*=|class\s+\w+", text, re.IGNORECASE
            ) else 0.0
        )
        scores["sufficient_length"] = 1.0 if len(text.strip()) >= 300 else len(text.strip()) / 300.0
        return scores

    def _score_tasks(self, text: str) -> dict[str, float]:
        scores: dict[str, float] = {}
        all_tasks = re.findall(r"^\s*-\s+\[[ xX]\]", text, re.MULTILINE)
        task_count = len(all_tasks)
        scores["task_count"] = min(task_count / 5.0, 1.0)
        sub_tasks = re.findall(r"^\s{2,}-\s+\[[ xX]\]", text, re.MULTILINE)
        scores["has_subtasks"] = 1.0 if sub_tasks else 0.0
        descriptive_tasks = re.findall(r"^\s*-\s+\[[ xX]\]\s+.{10,}", text, re.MULTILINE)
        scores["tasks_are_descriptive"] = (
            min(len(descriptive_tasks) / max(task_count, 1), 1.0) if task_count > 0 else 0.0
        )
        sections = re.findall(r"^#{1,3}\s+.+", text, re.MULTILINE)
        scores["has_sections"] = 1.0 if sections else 0.0
        return scores

    @staticmethod
    def _read(path: Path) -> str | None:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    @staticmethod
    def _avg(scores: dict[str, float]) -> float:
        if not scores:
            return 0.0
        return sum(scores.values()) / len(scores)
