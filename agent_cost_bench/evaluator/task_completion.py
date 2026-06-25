"""
Task completion rate evaluator: reads tasks.md from the workspace and counts
checked vs total checkboxes. Returns a float 0.0–1.0.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import TaskConfig


class TaskCompletionEvaluator:
    def __init__(self, task: TaskConfig, workspace_path: Path):
        self.task = task
        self.workspace = workspace_path

    def evaluate(self) -> float:
        from .spec_paths import resolve_spec_dir

        tasks_path = resolve_spec_dir(self.workspace, self.task.id) / "tasks.md"
        if not tasks_path.exists():
            return 0.0
        content = tasks_path.read_text(encoding="utf-8", errors="replace")
        return self._compute_rate(content)

    @staticmethod
    def _compute_rate(content: str) -> float:
        all_boxes = re.findall(r"^\s*-\s+\[[ xX]\]", content, re.MULTILINE)
        checked = re.findall(r"^\s*-\s+\[[xX]\]", content, re.MULTILINE)
        total = len(all_boxes)
        done = len(checked)
        return done / total if total > 0 else 0.0

    def get_task_details(self) -> dict[str, int]:
        from .spec_paths import resolve_spec_dir

        tasks_path = resolve_spec_dir(self.workspace, self.task.id) / "tasks.md"
        if not tasks_path.exists():
            return {"total": 0, "completed": 0, "pending": 0}
        content = tasks_path.read_text(encoding="utf-8", errors="replace")
        all_boxes = re.findall(r"^\s*-\s+\[[ xX]\]", content, re.MULTILINE)
        checked = re.findall(r"^\s*-\s+\[[xX]\]", content, re.MULTILINE)
        total = len(all_boxes)
        done = len(checked)
        return {"total": total, "completed": done, "pending": total - done}
