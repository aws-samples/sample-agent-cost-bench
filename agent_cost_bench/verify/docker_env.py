"""
Resolve a container runtime environment that can see a given image.

Existence is checked with ``<runtime> image ls -q <ref>`` (matches what
``<runtime> images`` shows) — NOT ``<runtime> image inspect``, which can fail by
short name under Docker Desktop's containerd image store even when the image is
present.

In the common case (one daemon, ``DOCKER_HOST`` unset) the inherited environment
already sees the image and is used as-is. The context/socket fallbacks only kick
in for unusual multi-daemon setups.

The container runtime binary is determined by the ``CONTAINER_RUNTIME`` environment
variable (default: ``docker``). Set ``CONTAINER_RUNTIME=finch`` to use Finch.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def get_runtime() -> str:
    """Return the container runtime binary name (docker or finch)."""
    return os.environ.get("CONTAINER_RUNTIME", "docker")


def _present(image: str, env: dict | None) -> bool:
    """True if ``image`` exists in the daemon reachable with ``env``."""
    try:
        # Security: runtime is resolved from PATH; "image", "ls", "-q" are
        # static. `image` comes from task.yaml verify.image (operator-owned).
        p = subprocess.run(  # noqa: S603
            [get_runtime(), "image", "ls", "-q", image],
            capture_output=True, text=True, timeout=20, env=env,
        )
        return p.returncode == 0 and bool(p.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def list_contexts() -> list[str]:
    try:
        # Security: fully static command, no external input.
        out = subprocess.run(  # noqa: S603
            [get_runtime(), "context", "ls", "-q"],
            capture_output=True, text=True, timeout=20,
        ).stdout.split()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    preferred = [c for c in out if c in ("desktop-linux", "default", "colima")]
    return preferred + [c for c in out if c not in preferred]


def _context_env(name: str) -> dict:
    env = os.environ.copy()
    env.pop("DOCKER_HOST", None)
    env["DOCKER_CONTEXT"] = name
    return env


def _socket_envs() -> list[dict]:
    home = os.path.expanduser("~")
    envs = []
    for sock in (
        f"{home}/.docker/run/docker.sock",
        "/var/run/docker.sock",
        f"{home}/.colima/default/docker.sock",
    ):
        if os.path.exists(sock):
            e = os.environ.copy()
            e.pop("DOCKER_CONTEXT", None)
            e["DOCKER_HOST"] = f"unix://{sock}"
            envs.append(e)
    return envs


def resolve_docker_env(image: str) -> dict | None:
    """Return an env whose container daemon has ``image``, or None. The inherited
    environment (what your shell uses) is tried first."""
    if shutil.which(get_runtime()) is None:
        return None
    if _present(image, os.environ.copy()):
        return os.environ.copy()
    for name in list_contexts():
        env = _context_env(name)
        if _present(image, env):
            return env
    for env in _socket_envs():
        if _present(image, env):
            return env
    return None


def docker_available(env: dict | None = None) -> bool:
    """True if the container runtime CLI is installed and a daemon is reachable."""
    rt = get_runtime()
    if shutil.which(rt) is None:
        return False
    try:
        # Security: fully static command, no external input.
        # `... info --format {{.ServerVersion}}` is the fast daemon-reachability
        # check (matches the original `docker version --format` cost) and works
        # for both docker and finch.
        return (
            subprocess.run(  # noqa: S603
                [rt, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, timeout=20, env=env,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def diagnostic(image: str) -> str:
    """Human-readable diagnostic of what container runtime the subprocess sees (for logs)."""
    rt = get_runtime()

    def _run(args, env=None):
        try:
            # Security: args are static CLI commands or the image name
            # from operator-owned task.yaml. Array-based exec, no shell.
            p = subprocess.run(  # noqa: S603
                args, capture_output=True, text=True, timeout=20, env=env
            )
            return (p.stdout or p.stderr).strip()
        except Exception as e:  # pragma: no cover
            return f"<error: {e}>"

    imgs = _run([rt, "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
    kb = [ln for ln in imgs.splitlines() if "agent_cost_bench" in ln]
    return "\n".join([
        f"runtime        : {rt}",
        f"{rt} on PATH   : {_run(['which', rt])}",
        f"HOME           : {os.environ.get('HOME', '<unset>')}",
        f"DOCKER_HOST    : {os.environ.get('DOCKER_HOST', '<unset>')}",
        f"DOCKER_CONTEXT : {os.environ.get('DOCKER_CONTEXT', '<unset>')}",
        f"image ls -q '{image}': {_run([rt, 'image', 'ls', '-q', image]) or '<empty>'}",
        f"agent_cost_bench images: {', '.join(kb) or '<none>'}",
    ])
