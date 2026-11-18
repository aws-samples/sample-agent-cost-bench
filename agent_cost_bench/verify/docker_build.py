"""
Build a Docker image from a task-supplied build context when it is missing.

Imported Terminal-Bench tasks often ship an ``environment/Dockerfile``
rather than a prebuilt image. Rather than asking the operator to build it by
hand, the framework can build it on demand: :func:`ensure_image` checks whether
``image`` already exists on a reachable daemon and, if not, runs
``<runtime> build -t <image> <context>``.

The build uses the same runtime resolution as verification
(:mod:`agent_cost_bench.verify.docker_env`), so the image lands on the daemon the
verify runner will actually use. Building is best-effort and reported via the
optional ``log`` callback; failures return False so the caller can fall back to
the normal "image missing → harness error" path instead of crashing the run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .docker_env import docker_available, get_runtime, resolve_docker_env


class BuildResult:
    __slots__ = ("image", "built", "ok", "detail")

    def __init__(self, image: str, *, built: bool, ok: bool, detail: str = ""):
        self.image = image
        self.built = built  # True if a build was actually attempted
        self.ok = ok        # True if the image is present after this call
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"BuildResult(image={self.image!r}, built={self.built}, ok={self.ok})"


def ensure_image_pulled(
    image: str,
    *,
    timeout_seconds: int = 1800,
    log=None,
) -> BuildResult:
    """Ensure ``image`` exists locally, pulling it from its registry if missing.

    For imported tasks that reference a prebuilt, externally-hosted image (e.g.
    Terminal-Bench's ``alexgshaw/<task>:<date>``) there is no local build
    context — the image is published to a registry and must be pulled. This is
    the pull analogue of :func:`ensure_image`.

    Returns a :class:`BuildResult` (``built`` means "a pull was attempted").
    Never raises for an ordinary pull failure — it returns ``ok=False`` so the
    caller degrades gracefully to the "image missing → harness error" path.
    """
    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    runtime = get_runtime()

    # Already present on a reachable daemon? Nothing to do.
    if resolve_docker_env(image) is not None:
        return BuildResult(image, built=False, ok=True, detail="already present")

    if not docker_available():
        return BuildResult(
            image, built=False, ok=False,
            detail=f"{runtime} daemon not reachable — cannot pull {image}",
        )

    _log(f"pulling image {image} …")
    # Security: runtime resolved from PATH; "pull" is static; `image` comes from
    # operator-owned task fixtures (task.yaml verify.image). Array exec, no shell.
    cmd = [runtime, "pull", image]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout_seconds
        )
    except FileNotFoundError:
        return BuildResult(image, built=False, ok=False, detail=f"{runtime} not found on PATH")
    except subprocess.TimeoutExpired:
        return BuildResult(
            image, built=True, ok=False,
            detail=f"pull timed out after {timeout_seconds}s",
        )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        return BuildResult(
            image, built=True, ok=False,
            detail="pull failed:\n" + "\n".join(tail),
        )

    # Confirm the freshly pulled image is now visible to the verify runner.
    ok = resolve_docker_env(image) is not None
    return BuildResult(
        image, built=True, ok=ok,
        detail="pulled" if ok else "pull reported success but image not found",
    )


def ensure_image(
    image: str,
    context_dir: Path,
    *,
    timeout_seconds: int = 1800,
    log=None,
    platform: str | None = None,
) -> BuildResult:
    """Ensure ``image`` exists, building it from ``context_dir`` if missing.

    ``platform`` (e.g. ``"linux/amd64"``) builds for that architecture; Docker
    emulates it on a host with a different CPU.

    Returns a :class:`BuildResult`. ``ok`` is True when the image is present
    afterwards (already there, or built successfully). Never raises for an
    ordinary build failure — it returns ``ok=False`` so the caller degrades
    gracefully.
    """
    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    runtime = get_runtime()

    # Already present on a reachable daemon? Nothing to do.
    if resolve_docker_env(image) is not None:
        return BuildResult(image, built=False, ok=True, detail="already present")

    if not docker_available():
        return BuildResult(
            image, built=False, ok=False,
            detail=f"{runtime} daemon not reachable — cannot build {image}",
        )

    context_dir = Path(context_dir)
    dockerfile = context_dir / "Dockerfile"
    if not context_dir.is_dir() or not dockerfile.is_file():
        return BuildResult(
            image, built=False, ok=False,
            detail=f"no Dockerfile in build context {context_dir}",
        )

    _log(f"building image {image} from {context_dir} …")
    # Security: runtime resolved from PATH; flags are static; `image` and the
    # context path come from operator-owned task fixtures. Array exec, no shell.
    cmd = [runtime, "build", "-t", image]
    if platform:
        cmd += ["--platform", platform]
    cmd.append(str(context_dir))
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout_seconds
        )
    except FileNotFoundError:
        return BuildResult(image, built=False, ok=False, detail=f"{runtime} not found on PATH")
    except subprocess.TimeoutExpired:
        return BuildResult(
            image, built=True, ok=False,
            detail=f"build timed out after {timeout_seconds}s",
        )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        return BuildResult(
            image, built=True, ok=False,
            detail="build failed:\n" + "\n".join(tail),
        )

    # Confirm the freshly built image is now visible to the verify runner.
    ok = resolve_docker_env(image) is not None
    return BuildResult(
        image, built=True, ok=ok,
        detail="built" if ok else "build reported success but image not found",
    )
