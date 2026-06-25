"""
Steering adherence evaluator.

Parses steering docs for rules and checks whether generated code follows them.
Rule-based matching always runs; an optional LLM-as-judge (via the Kiro CLI)
blends in when ``config.judge_model`` is set.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models import BenchConfig, TaskConfig


class SteeringAdherenceEvaluator:
    RULE_PATTERNS = [
        r"(?:always|must|should|use|prefer|avoid|never|do not|don't)\s+.{5,80}",
    ]

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
        self._model_label = model_label

    async def evaluate(self) -> tuple[float, dict[str, Any]]:
        steering_dir = self.workspace / ".kiro" / "steering"
        if not steering_dir.exists():
            return 1.0, {"note": "No steering docs found", "not_applicable": True}

        steering_docs = list(steering_dir.glob("*.md"))
        if not steering_docs:
            return 1.0, {"note": "No steering docs found", "not_applicable": True}

        rules = self._extract_rules(steering_docs)
        if not rules:
            return 1.0, {"note": "No extractable rules found in steering docs", "not_applicable": True}

        source_files = self._collect_source_files()
        if not source_files:
            return 0.0, {"note": "No source files found in workspace", "rules": rules}

        source_text = "\n".join(source_files.values())

        violations: list[str] = []
        adherences: list[str] = []
        for rule in rules:
            if self._rule_violated(rule, source_text):
                violations.append(rule)
            else:
                adherences.append(rule)

        rule_score = len(adherences) / len(rules) if rules else 1.0
        details: dict[str, Any] = {
            "total_rules": len(rules),
            "adhered": len(adherences),
            "violated": len(violations),
            "violations": violations[:10],
            "rule_based_score": rule_score,
        }

        if self.config.judge_model:
            llm_score = await self._llm_judge(steering_docs, source_files)
            details["llm_judge_score"] = llm_score
            w = self.config.judge_weight
            final_score = (1 - w) * rule_score + w * llm_score
        else:
            final_score = rule_score

        details["final_score"] = final_score
        return final_score, details

    # ------------------------------------------------------------------

    def _extract_rules(self, steering_docs: list[Path]) -> list[str]:
        rules: list[str] = []
        for doc in steering_docs:
            text = doc.read_text(encoding="utf-8", errors="replace")
            for pattern in self.RULE_PATTERNS:
                matches = re.findall(pattern, text, re.IGNORECASE)
                rules.extend(m.strip() for m in matches if len(m.strip()) > 10)
        seen: set[str] = set()
        unique: list[str] = []
        for r in rules:
            key = r.lower()[:60]
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique[:50]

    def _rule_violated(self, rule: str, source_text: str) -> bool:
        tokens = re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{2,}\b", rule)
        stop_words = {
            "use", "the", "for", "and", "with", "all", "any", "not",
            "should", "must", "always", "never", "avoid", "prefer",
        }
        keywords = [t for t in tokens if t.lower() not in stop_words]
        if not keywords:
            return False
        if re.search(r"\buse\b", rule, re.IGNORECASE):
            return not any(kw.lower() in source_text.lower() for kw in keywords)
        if re.search(r"\bnever\b|\bavoid\b|\bdo not\b", rule, re.IGNORECASE):
            return any(kw.lower() in source_text.lower() for kw in keywords)
        return False

    def _collect_source_files(self) -> dict[str, str]:
        source_extensions = {".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".java", ".rs", ".rb"}
        files: dict[str, str] = {}
        for ext in source_extensions:
            for p in self.workspace.rglob(f"*{ext}"):
                parts = p.parts
                if any(
                    part.startswith(".") or part in ("node_modules", "__pycache__", "dist", "build")
                    for part in parts
                ):
                    continue
                try:
                    rel = str(p.relative_to(self.workspace))
                    files[rel] = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        return files

    async def _llm_judge(self, steering_docs: list[Path], source_files: dict[str, str]) -> float:
        from ..judge import LLMJudge, steering_prompt

        steering_content = "\n\n---\n\n".join(
            f"# {doc.name}\n{doc.read_text(encoding='utf-8', errors='replace')}"
            for doc in steering_docs
        )
        code_sample = "\n\n".join(
            f"## {name}\n```\n{content[:500]}\n```"
            for name, content in list(source_files.items())[:5]
        )
        judge = LLMJudge(
            self.config, logger=self._logger, task_id=self.task.id, model_label=self._model_label
        )
        result = await judge.score(
            steering_prompt(steering_content, code_sample), phase="judge:steering"
        )
        return result.score
