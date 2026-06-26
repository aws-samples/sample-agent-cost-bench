"""
Preflight checks run before a benchmark to catch misconfiguration early.

* ``check_targets_available`` — verify every target's CLI binary resolves on
  disk/PATH. A missing binary would make every run for that target fail and look
  like a capability problem instead of a setup one. Applies to both modes.
* ``validate_models`` — model-compare only: validate Kiro model ids against the
  CLI's ``--list-models`` output. An unknown id makes the Kiro CLI hang silently
  in --no-interactive mode, so we fail fast.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from .models import BenchConfig, CompareMode, CostSource


def check_targets_available(config: BenchConfig) -> list[str]:
    """Return target binaries that could NOT be resolved (empty = all good)."""
    missing: list[str] = []
    seen: set[str] = set()
    for t in config.enabled_targets():
        if t.cli_path in seen:
            continue
        seen.add(t.cli_path)
        if shutil.which(t.cli_path) is None:
            missing.append(f"{t.label} ({t.cli_path})")
    if config.judge_model and config.judge_cli_path and config.judge_cli_path not in seen:
        if shutil.which(config.judge_cli_path) is None:
            missing.append(f"judge ({config.judge_cli_path})")
    return missing


def list_available_models(config: BenchConfig) -> list[str] | None:
    """Query the Kiro CLI for valid model ids, or None if it can't be queried."""
    cli_path = config.kiro_cli_path
    # Validate: resolve the binary via PATH to ensure it exists and reject
    # paths containing null bytes (defense-in-depth against corrupted config).
    resolved = shutil.which(cli_path) if cli_path and "\x00" not in cli_path else None
    if resolved is None:
        return None
    cmd = [resolved, "chat", "--list-models", "--format", "json"]
    try:
        # Security: cmd[0] is resolved via shutil.which above (no shell);
        # remaining args are static literals. Input originates from the
        # operator's own config file.
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=60
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    models = data.get("models", [])
    ids: list[str] = []
    for m in models:
        if "model_id" in m:
            ids.append(m["model_id"])
        if "model_name" in m and m["model_name"] not in ids:
            ids.append(m["model_name"])
    return ids or None


def required_docker_images(tasks) -> set[str]:
    """Collect the Docker images the given tasks need for verification
    (from a declarative ``verify.image`` or the legacy ``docker_image``)."""
    images: set[str] = set()
    for t in tasks:
        spec = getattr(t, "verify", None)
        if spec is not None and getattr(spec, "image", None):
            images.add(spec.image)
        elif getattr(t, "docker_image", None):
            images.add(t.docker_image)
    return images


def docker_available() -> bool:
    """True if the Docker CLI is installed AND the daemon is reachable."""
    from .verify.docker_env import docker_available as _da

    return shutil.which("docker") is not None and _da()


def missing_docker_images(images: set[str]) -> list[str]:
    """Return the images (from the given set) NOT present on any reachable
    Docker daemon/context (handles headless-subprocess context mismatches)."""
    from .verify.docker_env import resolve_docker_env

    return [img for img in sorted(images) if resolve_docker_env(img) is None]


def docker_report(tasks) -> dict:
    """
    Diagnostic for Docker-verified tasks. Returns a dict with:
      required (sorted images), needs_docker (bool), docker_ok (bool),
      missing_images (list), tasks_blocked (count of tasks that can't verify).
    """
    required = required_docker_images(tasks)
    needs = bool(required)
    if not needs:
        return {"required": [], "needs_docker": False, "docker_ok": True,
                "missing_images": [], "tasks_blocked": 0}
    ok = docker_available()
    missing = sorted(required) if not ok else missing_docker_images(required)
    blocked = sum(
        1 for t in tasks
        if getattr(t, "docker_image", None) and (not ok or t.docker_image in missing)
    )
    return {
        "required": sorted(required),
        "needs_docker": True,
        "docker_ok": ok,
        "missing_images": missing,
        "tasks_blocked": blocked,
    }


def git_available() -> bool:
    """True if git is installed and callable."""
    return shutil.which("git") is not None


def repo_report(tasks) -> dict:
    """
    Diagnostic for repo-based tasks. Returns a dict with:
      needs_git (bool), git_ok (bool), unpinned (list of task ids whose ref is
      not a SHA), tasks (count of tasks that use repo:).
    """
    repo_tasks = [t for t in tasks if getattr(t, "repo", None) is not None]
    needs = bool(repo_tasks)
    if not needs:
        return {"needs_git": False, "git_ok": True, "unpinned": [], "tasks": 0}
    ok = git_available()
    unpinned = [t.id for t in repo_tasks if not t.repo.is_sha_pinned]
    return {
        "needs_git": True,
        "git_ok": ok,
        "unpinned": unpinned,
        "tasks": len(repo_tasks),
    }


def validate_models(config: BenchConfig) -> tuple[list[str], list[str]]:
    """
    model-compare only. Return (valid, invalid) Kiro model ids validated against
    the CLI. If the CLI can't be queried, treat all as valid so mock/offline runs
    still work. cli-compare returns everything as valid (no model validation).
    """
    if config.mode != CompareMode.MODEL_COMPARE:
        return [t.model_id for t in config.enabled_targets()], []

    available = list_available_models(config)
    kiro_targets = [
        t for t in config.enabled_targets() if t.cost_source == CostSource.KIRO_CREDITS
    ]
    if available is None:
        return [t.model_id for t in kiro_targets], []

    valid: list[str] = []
    invalid: list[str] = []
    for t in kiro_targets:
        (valid if t.model_id in available else invalid).append(t.model_id)
    return valid, invalid
