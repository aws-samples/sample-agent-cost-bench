"""
External-benchmark task importers.

These convert third-party benchmark task definitions into this framework's
native on-disk task fixtures (a ``task.yaml`` plus supporting ``verify/`` and
``src/`` assets), so the existing discovery/execution/evaluation pipeline runs
them unchanged.

Supported sources (selected by ``type`` in a config ``task_sources:`` entry):

* ``terminal-bench`` — Terminal-Bench 4.x tasks in the Harbor layout
  (``task.toml`` + ``instruction.md`` + ``environment/`` + ``tests/``). Older
  Terminal-Bench 1.x tasks (nested ``task.yaml`` instruction + ``run-tests.sh``)
  are also handled.

Each importer is a small function that reads one source task directory and emits
one native task directory. :func:`import_task_source` is the batch entry point
used by both the ``import-tasks`` CLI command and live config-driven discovery.
"""

from __future__ import annotations

from .base import ImportedTask, ImportError_, import_task_source, list_importers

__all__ = ["ImportedTask", "ImportError_", "import_task_source", "list_importers"]
