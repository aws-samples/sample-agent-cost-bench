"""
Resolve a Docker environment that can see a given image.

Existence is checked with ``docker image ls -q <ref>`` (matches what
``docker images`` shows) — NOT ``docker image inspect``, which can fail by short
name under Docker Desktop's containerd image store even when the image is
present.

In the common case (one daemon, ``DOCKER_HOST`` unset) the inherited environment
already sees the image and is used as-is. The context/socket fallbacks only kick
in for unusual multi-daemon setups.
"""

from __future__ import annotations

import os
import subprocess


def _present(image: str, env: dict | None) -> bool:
    """True if ``image`` exists in the daemon reachable with ``env``."""
    try:
        p = subprocess.run(
            ["docker", "image", "ls", "-q", image],
            capture_output=True, text=True, timeout=20, env=env,
        )
        return p.returncode == 0 and bool(p.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def list_contexts() -> list[str]:
    try:
        out = subprocess.run(
            ["docker", "context", "ls", "-q"],
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
    """Return an env whose Docker daemon has ``image``, or None. The inherited
    environment (what your shell uses) is tried first."""
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
    """True if the Docker CLI is installed and a daemon is reachable."""
    try:
        return (
            subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True, timeout=20, env=env,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def diagnostic(image: str) -> str:
    """Human-readable diagnostic of what Docker the subprocess sees (for logs)."""
    def _run(args, env=None):
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=20, env=env)
            return (p.stdout or p.stderr).strip()
        except Exception as e:  # pragma: no cover
            return f"<error: {e}>"

    imgs = _run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
    kb = [ln for ln in imgs.splitlines() if "kirobench" in ln]
    return "\n".join([
        f"docker on PATH : {_run(['which', 'docker'])}",
        f"HOME           : {os.environ.get('HOME', '<unset>')}",
        f"DOCKER_HOST    : {os.environ.get('DOCKER_HOST', '<unset>')}",
        f"DOCKER_CONTEXT : {os.environ.get('DOCKER_CONTEXT', '<unset>')}",
        f"current context: {_run(['docker', 'context', 'show'])}",
        f"image ls -q '{image}': {_run(['docker', 'image', 'ls', '-q', image]) or '<empty>'}",
        f"kirobench images: {', '.join(kb) or '<none>'}",
    ])
