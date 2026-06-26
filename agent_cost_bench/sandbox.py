"""
Workspace sandbox: an isolated per-run directory for each task × target.

Handles both modes: plain ``src/`` starter files (cli-compare vibe tasks) and
the full Kiro layout — ``.kiro/specs``, ``.kiro/steering``, ``.kiro/agents`` —
with seed/steering fixtures copied in for spec-driven (model-compare) tasks.

GitHub repo tasks: when ``task.repo`` is set, the repository is cloned into a
shared cache (``workspace_base/.repo_cache/<url_hash>/<ref>/``) on first use,
then copied into the workspace's ``src/`` directory. Parallel runs on the same
repo+ref share the single cached clone — only one network fetch per benchmark
run regardless of how many models are being compared.

All git invocations run with a transport allowlist (``GIT_ALLOW_PROTOCOL``) so a
crafted ``repo.url`` can't use ``ext::``/``file::`` to run commands or read local
files. Private repos authenticate over HTTPS via ``repo.token_env`` (the name of
an env var holding a token); the token is injected through git's ephemeral
``GIT_CONFIG_*`` env interface, so it never lands in argv or ``.git/config``.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from .models import BenchConfig, RepoSpec, TaskConfig

_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)

# Transports git is allowed to use. Blocks `ext::` (runs arbitrary commands) and
# `file::` (reads local paths) which a crafted repo.url could otherwise abuse.
_ALLOWED_GIT_PROTOCOLS = "https:http:ssh:git"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _safe_ref(ref: str) -> str:
    """Filesystem-safe version of a git ref (branch / tag / SHA)."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", ref)[:64]


def _git_base_env() -> dict[str, str]:
    """Environment applied to every git invocation.

    * ``GIT_ALLOW_PROTOCOL`` restricts transports to a safe allowlist so a
      malicious ``repo.url`` (e.g. ``ext::sh -c …`` or ``file://``) can't run
      commands or read local files.
    * ``GIT_TERMINAL_PROMPT=0`` makes auth failures error out instead of hanging
      a headless benchmark run on an interactive credential prompt.
    """
    env = os.environ.copy()
    env["GIT_ALLOW_PROTOCOL"] = _ALLOWED_GIT_PROTOCOLS
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _auth_env(repo: RepoSpec) -> dict[str, str]:
    """Env additions that authenticate a private-repo fetch.

    The token is read from the env var named by ``repo.token_env`` (the token
    itself is never stored in task.yaml). It is handed to git via the
    ``GIT_CONFIG_*`` environment interface as an ephemeral ``http.extraHeader``:
    this keeps it out of the process argument list (invisible to ``ps``) and out
    of the persisted ``.git/config`` in the cache. HTTPS only — SSH URLs
    authenticate with keys and ignore this.
    """
    if not repo.token_env:
        return {}
    token = os.environ.get(repo.token_env)
    if not token:
        raise RuntimeError(
            f"repo.token_env='{repo.token_env}' is set but that environment variable "
            f"is empty. Export it before running, e.g. export {repo.token_env}=<token>"
        )
    user = repo.token_user or "x-access-token"
    header = "Authorization: Basic " + base64.b64encode(
        f"{user}:{token}".encode()
    ).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": header,
    }


def _repo_cache_dir(workspace_base: Path, repo: RepoSpec) -> Path:
    """Deterministic cache directory for a given repo URL + ref."""
    url_hash = hashlib.sha256(repo.url.encode()).hexdigest()[:16]
    return workspace_base / ".repo_cache" / url_hash / _safe_ref(repo.ref)


def _run_git(*args: str, cwd: str | None = None, env: dict | None = None) -> None:
    """Run a git command, raising RuntimeError with stderr on failure.

    ``env`` defaults to the hardened base env (protocol allowlist + no prompt).
    Callers performing network operations pass an env that also carries the auth
    header. The token never appears in ``args``, so the failure message below is
    safe to surface."""
    cmd = ["git", *args]
    # Security: array-based exec (no shell). args are static git subcommands
    # and paths derived from operator-owned task.yaml repo specs.
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, timeout=300, cwd=cwd,
        env=env if env is not None else _git_base_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed (exit {result.returncode}):\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  stderr: {result.stderr[-800:]}"
        )


def _clone_to_cache(repo: RepoSpec, cache_dir: Path) -> None:
    """
    Clone *repo* into *cache_dir*.  Uses a ``.tmp`` sibling so the final
    rename is atomic — concurrent runners on the same repo+ref are safe.

    Strategy:
    * 40-char SHA  → git init + fetch single object (works across git versions)
    * branch / tag → git clone --branch <ref> [--depth N]
    * subdir       → git sparse-checkout after clone to limit disk use
    """
    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir.with_suffix(".tmp")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    is_sha = bool(_SHA_RE.match(repo.ref))
    depth_args = [f"--depth={repo.depth}"] if repo.depth > 0 else []

    # Local-only ops use the hardened base env; network ops (clone/fetch) also
    # carry the optional private-repo auth header.
    base_env = _git_base_env()
    net_env = {**base_env, **_auth_env(repo)}

    try:
        if is_sha:
            # git init + fetch the exact commit (no --branch needed)
            _run_git("init", str(tmp), env=base_env)
            _run_git("-C", str(tmp), "remote", "add", "origin", repo.url, env=base_env)
            _run_git("-C", str(tmp), "fetch", *depth_args, "origin", repo.ref, env=net_env)
            _run_git("-C", str(tmp), "checkout", "FETCH_HEAD", env=base_env)
        else:
            # Branch or tag — git clone --branch <ref>
            _run_git(
                "clone", *depth_args,
                "--branch", repo.ref,
                repo.url, str(tmp),
                env=net_env,
            )

        # Sparse checkout: limit working tree to requested subdirectory
        if repo.subdir:
            _run_git("-C", str(tmp), "sparse-checkout", "set", repo.subdir, env=base_env)

        # Atomic move: tmp → cache_dir
        try:
            tmp.rename(cache_dir)
        except (FileExistsError, OSError):
            # Another parallel run already completed the cache — that's fine.
            shutil.rmtree(tmp, ignore_errors=True)

    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def clone_repo(repo: RepoSpec, workspace_base: Path) -> Path:
    """
    Return the local path of the cached clone, cloning if necessary.

    The returned path is the root of the cached repo (or the sparse-checked-out
    subdir when ``repo.subdir`` is set). Callers copy from here into the
    workspace; they must NOT write into the cache directory.
    """
    cache_dir = _repo_cache_dir(workspace_base, repo)
    if not cache_dir.exists():
        _clone_to_cache(repo, cache_dir)
    # Return the subdir within the cache if requested
    if repo.subdir:
        subdir_path = cache_dir / repo.subdir
        if subdir_path.exists():
            return subdir_path
    return cache_dir


def pristine_baseline(task: TaskConfig, config: BenchConfig) -> Path | None:
    """Resolve the read-only pristine source the model started from, for diffing.

    For repo tasks this is the cached clone that was already fetched during
    workspace setup — this call only resolves the path (the cache already exists,
    so ``clone_repo`` returns immediately without any network access). The
    returned directory mirrors what was copied into the workspace, so a
    ``git diff --no-index <baseline> <workspace>`` yields exactly the model's
    changeset.

    Returns ``None`` when there is no clear baseline (e.g. greenfield tasks);
    callers then fall back to whole-file collection.
    """
    if task.repo is None:
        return None
    try:
        base = clone_repo(task.repo, Path(config.workspace_base))
    except Exception:
        return None
    return base if base.exists() else None


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

class Workspace:
    """An isolated workspace for a single task × target run."""

    def __init__(self, run_id: str, task: TaskConfig, config: BenchConfig):
        self.run_id = run_id
        self.task = task
        self.config = config
        self._base = Path(config.workspace_base) / run_id

    @property
    def path(self) -> Path:
        return self._base

    @property
    def kiro_dir(self) -> Path:
        return self._base / ".kiro"

    @property
    def specs_dir(self) -> Path:
        return self.kiro_dir / "specs" / self.task.id

    @property
    def steering_dir(self) -> Path:
        return self.kiro_dir / "steering"

    @property
    def agents_dir(self) -> Path:
        return self.kiro_dir / "agents"

    def setup(self) -> None:
        """Create the workspace and seed task fixtures."""
        self._base.mkdir(parents=True, exist_ok=True)

        task_dir = self.task.task_dir
        if task_dir is None:
            return

        # Standard Kiro dirs (created for every task so spec phases & steering
        # evaluation have a place to read/write; harmless for vibe tasks).
        self.specs_dir.mkdir(parents=True, exist_ok=True)
        self.steering_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)

        # Seed spec artifacts (requirements.md, design.md, tasks.md)
        seed_dir = task_dir / "seed"
        if seed_dir.exists():
            for f in seed_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, self.specs_dir / f.name)

        # Inject steering docs
        steering_src = task_dir / "steering"
        if steering_src.exists():
            for f in steering_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, self.steering_dir / f.name)

        # GitHub repo clone: copy directly into the workspace root so the model
        # sees the repo exactly as it appears on GitHub (no extra src/ wrapper).
        if self.task.repo is not None:
            workspace_base = Path(self.config.workspace_base)
            repo_root = clone_repo(self.task.repo, workspace_base)
            shutil.copytree(repo_root, self._base, dirs_exist_ok=True)
        else:
            # Fall back to copying any static starter source files.
            src_dir = task_dir / "src"
            if src_dir.exists():
                shutil.copytree(src_dir, self._base / "src", dirs_exist_ok=True)

    def teardown(self, keep: bool = False) -> None:
        """Kept after a run for inspection; cleaned at the start of the next run."""
        pass

    def read_file(self, relative_path: str) -> str | None:
        p = self._base / relative_path
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return None

    def spec_file(self, filename: str) -> str | None:
        p = self.specs_dir / filename
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return None
