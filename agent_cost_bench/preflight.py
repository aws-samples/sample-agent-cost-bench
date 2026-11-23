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
import re
import shutil
import subprocess
from pathlib import Path

from .models import BenchConfig, CompareMode, CostSource


def check_targets_available(config: BenchConfig) -> list[str]:
    """Return target binaries that could NOT be resolved (empty = all good)."""
    if config.execution.backend == "harbor":
        # Harbor installs the agent in the task image, not on this host.
        # BenchConfig._validate_harbor() checks runners are supported via Target.harbor_agent().
        return []
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
    missing_set = set(missing)

    def _task_image(t) -> str | None:
        """The verification image a task needs, from either the declarative
        ``verify.image`` or the legacy ``docker_image`` field."""
        spec = getattr(t, "verify", None)
        if spec is not None and getattr(spec, "image", None):
            return spec.image
        return getattr(t, "docker_image", None)

    blocked = sum(
        1 for t in tasks
        if (img := _task_image(t)) and (not ok or img in missing_set)
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


# ---------------------------------------------------------------------------
# Runtime-dependency preflight (imported Docker-verified tasks)
# ---------------------------------------------------------------------------
#
# Some external-benchmark tasks (notably Terminal-Bench) ship a task image that
# deliberately omits a language runtime and expect the AGENT to install it while
# solving the task ("Install R if not already available…"). This framework runs
# the agent on the HOST and only copies its produced files into a fresh image
# for grading, so any runtime the agent installed does not exist at verify time.
# The task's own tests then fail to launch that runtime (e.g. Rscript) and the
# task scores 0 — a harness limitation, not a model failure.
#
# We detect this up front: scan the task's authoritative test scripts for the
# external runtimes they invoke, probe the task image for those binaries, and
# report any that are missing from the image AND not installed by the task's own
# test script. Such tasks are skipped rather than scored, so the pass rate stays
# honest.

# External runtimes worth probing. Deliberately excludes python/bash/sh/coreutils
# (effectively always present) and things the test harness installs itself
# (uv/uvx/pip/pytest). Maps binary -> a human label for messages.
_RUNTIME_BINARIES = {
    "Rscript": "R",
    "node": "Node.js",
    "npm": "Node.js",
    "go": "Go",
    "cargo": "Rust (cargo)",
    "rustc": "Rust",
    "javac": "Java (JDK)",
    "java": "Java (JRE)",
    "ruby": "Ruby",
    "perl": "Perl",
    "php": "PHP",
    "dotnet": ".NET",
    "gcc": "GCC",
    "g++": "GCC (g++)",
    "clang": "Clang",
    "ghc": "Haskell (GHC)",
    "swift": "Swift",
    "scala": "Scala",
    "kotlinc": "Kotlin",
}

# subprocess.run(["<bin>", ...]) / Popen(["<bin>"...]) / check_output(["<bin>"...])
# — capture the first list element (the executable). Also matches a bare
# `"<bin>"` used as executable=... would not appear here, which is fine.
_SUBPROCESS_EXEC_RE = re.compile(
    r"""subprocess\.(?:run|Popen|check_output|check_call|call)\(\s*\[\s*['"]([A-Za-z0-9_.+-]+)['"]"""
)


def _scan_required_runtimes(task) -> set[str]:
    """Return the set of external runtime binaries the task's tests invoke.

    Scans every file under the task's authoritative tests dir
    (``verify/tests``) for ``subprocess.run(["<bin>", …])``-style calls and
    keeps only binaries in ``_RUNTIME_BINARIES``. Best-effort and static: it
    reads text files and ignores anything it can't parse.
    """
    import os

    spec = getattr(task, "verify", None)
    task_dir = getattr(task, "task_dir", None)
    if spec is None or task_dir is None:
        return set()
    tests_sub = getattr(spec, "tests_subdir", None) or "verify/tests"
    tests_dir = Path(task_dir) / tests_sub
    if not tests_dir.is_dir():
        return set()

    found: set[str] = set()
    for root, _dirs, files in os.walk(tests_dir):
        for name in files:
            p = Path(root) / name
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _SUBPROCESS_EXEC_RE.finditer(text):
                b = m.group(1)
                if b in _RUNTIME_BINARIES:
                    found.add(b)
    return found


# Package names an install line might use for a given runtime binary, so an
# apt/dnf line like `apt-get install -y r-base` counts as providing `Rscript`.
_RUNTIME_INSTALL_ALIASES = {
    "Rscript": ("r-base", "r-base-core", "r-cran", "rscript"),
    "node": ("nodejs", "node"),
    "npm": ("npm", "nodejs"),
    "go": ("golang", "go"),
    "cargo": ("cargo", "rust", "rustc"),
    "rustc": ("rustc", "rust", "cargo"),
    "javac": ("jdk", "openjdk", "default-jdk"),
    "java": ("jre", "jdk", "openjdk", "default-jre", "default-jdk"),
    "ruby": ("ruby",),
    "perl": ("perl",),
    "php": ("php",),
    "dotnet": ("dotnet", "dotnet-sdk"),
    "gcc": ("gcc", "build-essential"),
    "g++": ("g++", "build-essential"),
    "clang": ("clang", "llvm"),
    "ghc": ("ghc", "haskell"),
    "swift": ("swift",),
    "scala": ("scala",),
    "kotlinc": ("kotlin",),
}


def _install_aliases(binary: str) -> tuple[str, ...]:
    """Return the tokens whose presence in an install line implies ``binary``."""
    return (binary.lower(),) + _RUNTIME_INSTALL_ALIASES.get(binary, ())


def _text_installs_binary(text: str, binary: str) -> bool:
    """True if ``text`` contains an install line that provides ``binary``.

    Heuristic: a line mentions a package manager install (apt/apt-get/dnf/yum/
    apk/pip/conda/brew or a bare 'install') AND one of the binary's aliases.
    """
    lower = text.lower()
    if not any(k in lower for k in ("install", "apt", "dnf", "yum", "apk", "conda", "brew")):
        return False
    aliases = _install_aliases(binary)
    for line in lower.splitlines():
        if any(k in line for k in ("install", "apt", "dnf", "yum", "apk", "add ")) \
           and any(a in line for a in aliases):
            return True
    return False


def _test_script_installs(task, binary: str) -> bool:
    """True if the task's own test script (test.sh/run-tests.sh) installs
    ``binary`` — if so it will be present at verify time even if the base image
    lacks it, so we must NOT flag the task."""
    import os

    task_dir = getattr(task, "task_dir", None)
    spec = getattr(task, "verify", None)
    if task_dir is None or spec is None:
        return False
    tests_sub = getattr(spec, "tests_subdir", None) or "verify/tests"
    tests_dir = Path(task_dir) / tests_sub
    for root, _dirs, files in os.walk(tests_dir):
        for name in files:
            if not name.endswith(".sh"):
                continue
            try:
                text = (Path(root) / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _text_installs_binary(text, binary):
                return True
    return False


def _dockerfile_provides(task, binary: str) -> bool:
    """True if the task's imported environment Dockerfile installs ``binary``
    (an ``apt-get install …`` / ``dnf install …`` RUN line naming the runtime).

    This is a static, pull-free check: the Dockerfile is copied into the fixture
    at import time (``verify/environment/Dockerfile``), so we can read it without
    fetching the prebuilt image. It's a heuristic — a prebuilt image could bake
    in a runtime the Dockerfile doesn't show — but TB tasks that expect the AGENT
    to install a runtime ship a vanilla base image whose Dockerfile does not
    install it, which is exactly the case we want to catch.
    """
    task_dir = getattr(task, "task_dir", None)
    if task_dir is None:
        return False
    for rel in ("verify/environment/Dockerfile", "verify/environment/dockerfile"):
        df = Path(task_dir) / rel
        if df.is_file():
            try:
                text = df.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return _text_installs_binary(text, binary)
    return False


def unsupported_runtime_report(tasks) -> dict:
    """Identify Docker-verified tasks whose tests need a runtime that nothing
    provides — i.e. neither the task's test script nor its environment Dockerfile
    installs it — so it won't exist in the grading container under the host-agent
    model. Such tasks are reported for skipping.

    Detection is fully STATIC (no image pull): it reads the imported task's test
    files, test.sh, and verify/environment/Dockerfile. Returns
    ``{task_id: {"image": str, "missing": [(binary, label), …]}}``.
    """
    report: dict = {}
    for t in tasks:
        spec = getattr(t, "verify", None)
        image = getattr(spec, "image", None) if spec else None
        if not image:
            continue
        if getattr(spec, "dedicated_image", False):
            continue  # grading-only image: its author validated the grader there
        required = _scan_required_runtimes(t)
        if not required:
            continue
        missing: list[tuple[str, str]] = []
        for b in sorted(required):
            # Provided if either the test script or the env Dockerfile installs it.
            if _test_script_installs(t, b) or _dockerfile_provides(t, b):
                continue
            missing.append((b, _RUNTIME_BINARIES[b]))
        if missing:
            report[t.id] = {"image": image, "missing": missing}
    return report
