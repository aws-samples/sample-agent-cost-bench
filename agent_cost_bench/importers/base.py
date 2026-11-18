"""
Shared machinery for external-benchmark task importers.

An importer reads ONE source task directory and writes ONE native task fixture
(a ``task.yaml`` + ``verify/`` assets). The batch entry point
:func:`import_task_source` walks a source root, applies an optional task-name
filter, dispatches each task to the right per-source converter, and returns the
list of :class:`ImportedTask` records.

Design note — how a container-native benchmark maps onto this framework
------------------------------------------------------------------------
Terminal-Bench (Harbor) runs the agent *inside* the task's own Docker image
and then runs the task's own ``tests/test.sh`` (in that image, or in a separate
verifier image for 4.x tasks), where the test script writes a reward (0..1) to
``/logs/verifier/reward.txt``.

This framework instead runs the agent in a host workspace and, for a Docker
``verify:`` block, copies the model's ``workspace/src`` into a fresh container
built from ``image`` and runs a ``test_cmd`` there (report read from
``$RESULTS_DIR``). We bridge the two by generating a ``verify:`` block that:

  1. lays the model's ``src/`` down at the container path the task expects
     (its working dir), via the ``setup`` steps,
  2. copies the task's authoritative tests (mounted read-only at ``$TESTS_RO``)
     into place, and
  3. runs the task's test script, then publishes its reward file to
     ``$RESULTS_DIR/reward.txt`` for the ``reward-file`` parser.

The imported prompt tells the agent to place all solution files under ``src/``
so step (1) has something to copy. This keeps the agent side host-native (no
change to executors) while preserving the task's authoritative grading.

Docker images
-------------
When the source task names a prebuilt image, it is used as-is. When it ships
only a ``Dockerfile`` (its ``environment/``), the importer copies that context
to ``verify/environment/`` and records ``verify.build_context`` on the emitted
task. The framework then builds the image on demand at run time (see
:mod:`agent_cost_bench.verify.docker_build`) — no manual build step required.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class ImportError_(Exception):
    """Raised when a source task can't be converted (bad/missing files)."""


def dump_task_yaml(data: dict) -> str:
    """Serialize a task.yaml dict with multiline strings as literal blocks (|).

    Keeps prompts and multi-line shell ``test_cmd`` readable in the emitted file
    instead of collapsing them into folded scalars. Uses a private Dumper so the
    representer override never leaks into the rest of the app's YAML usage.
    """
    import yaml

    class _TaskDumper(yaml.SafeDumper):
        pass

    def _str_representer(dumper, value):
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _TaskDumper.add_representer(str, _str_representer)
    return yaml.dump(
        data, Dumper=_TaskDumper, sort_keys=False, default_flow_style=False, width=4096
    )


def _override_timeout(task_yaml_path: Path, minutes: int) -> None:
    """Rewrite the ``timeout_minutes:`` value in an emitted task.yaml in place.

    Edits just that one top-level line so the file's header comments and literal
    block scalars (prompt, multi-line test_cmd) are preserved. If no such line
    exists, one is inserted after the ``id:`` line.
    """
    import re

    if not task_yaml_path.is_file():
        return
    text = task_yaml_path.read_text(encoding="utf-8")
    new_line = f"timeout_minutes: {int(minutes)}"
    # Match a top-level (unindented) timeout_minutes line.
    pattern = re.compile(r"^timeout_minutes:.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(new_line, text, count=1)
    else:
        # Insert after the first top-level `id:` line as a sensible anchor.
        id_pat = re.compile(r"^(id:.*)$", re.M)
        m = id_pat.search(text)
        if m:
            text = text[: m.end()] + "\n" + new_line + text[m.end() :]
        else:
            text = new_line + "\n" + text
    task_yaml_path.write_text(text, encoding="utf-8")


@dataclass
class ImportedTask:
    """Record of one converted task."""

    source_id: str
    native_id: str
    dest_dir: Path
    image: str | None
    warnings: list[str] = field(default_factory=list)


# A converter reads one source task dir and writes into dest_dir; returns the
# ImportedTask. Signature: (source_dir, dest_dir, native_id) -> ImportedTask
Converter = Callable[[Path, Path, str], "ImportedTask"]


def slugify(value: str) -> str:
    """Filesystem/id-safe slug: lowercase, non-alnum runs collapsed to '-'."""
    out = []
    prev_dash = False
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "task"


def read_toml(path: Path) -> dict:
    """Read a TOML file. Uses stdlib ``tomllib`` (3.11+); falls back to a tiny
    parser sufficient for the flat ``task.toml`` used by Harbor tasks (top-level
    keys plus single-level ``[section]`` tables with string/number/bool/list
    scalars). The fallback is intentionally minimal — it is not a general TOML
    implementation, only enough to read a Harbor ``task.toml``."""
    text = path.read_text(encoding="utf-8")
    try:  # Python 3.11+
        import tomllib

        return tomllib.loads(text)
    except ModuleNotFoundError:
        pass
    try:  # optional dependency, if the user happens to have it
        import tomli  # type: ignore

        return tomli.loads(text)
    except ModuleNotFoundError:
        pass
    return _mini_toml(text)


def _mini_toml(text: str) -> dict:
    """Minimal single-level TOML reader (fallback for Python 3.10)."""
    import ast

    root: dict = {}
    section = root
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().strip('"')
            # Only single-level tables are supported; a dotted name uses the
            # last component so we degrade gracefully rather than crash.
            key = name.split(".")[-1]
            section = root.setdefault(key, {})
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().strip('"')
        v = v.strip()
        # Strip inline comments outside quotes/brackets (best effort).
        section[k] = _parse_scalar(v)
    return root


def _parse_scalar(v: str):
    import ast

    v = v.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        try:
            return list(ast.literal_eval(v))
        except (ValueError, SyntaxError):
            return []
    try:
        if "." in v or "e" in v.lower():
            return float(v)
        return int(v)
    except ValueError:
        return v.strip('"')


def copy_tree_into(src: Path, dest: Path) -> None:
    """Copy a directory's *contents* into dest (created if needed)."""
    if not src.exists():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


# ---------------------------------------------------------------------------
# Registry & batch entry point
# ---------------------------------------------------------------------------


def _registry() -> dict[str, Converter]:
    # Imported lazily to avoid a circular import (submodules import helpers here).
    from .terminal_bench import convert_terminal_bench_task, is_terminal_bench_task

    return {
        "terminal-bench": convert_terminal_bench_task,
        "tb": convert_terminal_bench_task,                # alias
    }


def list_importers() -> list[str]:
    return sorted(set(_registry().keys()))


def _detectors() -> dict[str, Callable[[Path], bool]]:
    from .terminal_bench import is_terminal_bench_task

    return {
        "terminal-bench": is_terminal_bench_task,
    }


def _find_task_dirs(source_root: Path) -> list[Path]:
    """A source root can be a single task dir or a directory OF task dirs.

    A task dir is recognised by any importer's detector; if the root itself is a
    task, return just it, otherwise return its immediate subdirectories that
    look like tasks (falling back to all subdirs so a nonstandard layout still
    surfaces something to convert)."""
    detectors = _detectors()
    if any(det(source_root) for det in detectors.values()):
        return [source_root]
    subdirs = [d for d in sorted(source_root.iterdir()) if d.is_dir()]
    task_like = [d for d in subdirs if any(det(d) for det in detectors.values())]
    return task_like or subdirs


def import_task_source(
    *,
    source_type: str,
    source_path: str | Path,
    dest_root: str | Path,
    native_mode: str = "vibe",
    only: list[str] | None = None,
    overwrite: bool = True,
    timeout_minutes: int | None = None,
    strict_only: bool = True,
) -> list[ImportedTask]:
    """Convert every (matching) task under ``source_path`` into a native fixture.

    Parameters
    ----------
    source_type   one of :func:`list_importers` (``terminal-bench`` / aliases)
    source_path   a single task dir OR a directory containing task dirs
    dest_root     where native fixtures are written: ``<dest_root>/<mode>/<id>/``
    native_mode   native task mode for the emitted fixtures (default ``vibe``)
    only          optional list of source task names to include (others skipped)
    overwrite     replace an existing destination dir (default True)
    timeout_minutes  when set, override the emitted per-task ``timeout_minutes``
                  for every imported task (others keep the source value)
    strict_only   when True (default) and ``only`` is given, raise if any named
                  task isn't found (catches user typos). Set False for a
                  best-effort default filter (e.g. the built-in allowlist) where
                  an upstream-renamed entry should be skipped, not fatal.
    """
    registry = _registry()
    key = source_type.strip().lower()
    convert = registry.get(key)
    if convert is None:
        raise ImportError_(
            f"Unknown task source type '{source_type}'. "
            f"Supported: {', '.join(list_importers())}"
        )

    src_root = Path(source_path).expanduser().resolve()
    if not src_root.exists():
        raise ImportError_(f"Task source path not found: {src_root}")

    dest_mode_root = Path(dest_root).expanduser().resolve() / native_mode
    dest_mode_root.mkdir(parents=True, exist_ok=True)

    wanted = set(only) if only else None
    imported: list[ImportedTask] = []
    for task_dir in _find_task_dirs(src_root):
        source_id = task_dir.name
        if wanted is not None and source_id not in wanted:
            continue
        native_id = slugify(f"{key}-{source_id}")
        dest_dir = dest_mode_root / native_id
        if dest_dir.exists():
            if not overwrite:
                continue
            shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            rec = convert(task_dir, dest_dir, native_id)
        except ImportError_:
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise
        except Exception as e:  # pragma: no cover - defensive
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise ImportError_(f"Failed to convert '{source_id}': {e}") from e
        if timeout_minutes is not None:
            _override_timeout(dest_dir / "task.yaml", timeout_minutes)
        imported.append(rec)

    if wanted is not None:
        missing = wanted - {r.source_id for r in imported}
        if missing and strict_only:
            raise ImportError_(
                f"Named task(s) not found under {src_root}: {', '.join(sorted(missing))}"
            )
    return imported
