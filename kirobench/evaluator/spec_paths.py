"""
Resolve the spec artifact directory inside a workspace.

Native spec mode may name the spec directory after the feature rather than the
benchmark task id, so we don't assume ``.kiro/specs/<task_id>/``. Resolution:

  1. ``.kiro/specs/<task_id>/`` if it contains ``requirements.md`` (seeded spec).
  2. Otherwise the most-recently-modified subdir under ``.kiro/specs/`` that
     contains a ``requirements.md`` (what native spec mode writes).
  3. Otherwise the single subdirectory under ``.kiro/specs/`` (if exactly one).
  4. Otherwise the conventional ``.kiro/specs/<task_id>/`` path (may not exist;
     callers treat a missing artifact as score 0).
"""

from __future__ import annotations

from pathlib import Path


def resolve_spec_dir(workspace: Path, task_id: str) -> Path:
    specs_root = workspace / ".kiro" / "specs"
    preferred = specs_root / task_id
    # Prefer the task-id dir only if it actually holds artifacts (e.g. a seeded
    # spec). Native spec mode writes to a feature-named dir, and the harness may
    # have pre-created an empty <task_id> dir — don't let that empty dir shadow
    # the real one.
    if (preferred / "requirements.md").exists():
        return preferred
    if specs_root.exists():
        subdirs = [d for d in specs_root.iterdir() if d.is_dir()]
        with_reqs = [d for d in subdirs if (d / "requirements.md").exists()]
        if with_reqs:
            # Most recently modified dir containing requirements.md.
            return max(with_reqs, key=lambda d: d.stat().st_mtime)
        if len(subdirs) == 1:
            return subdirs[0]
    return preferred
