"""
Desugaring helpers that turn the two YAML schemas into a unified ``Target`` list.

* ``make_cli_target``   — a cli-compare ``runners:`` entry maps almost directly
  to a Target; it keeps its own ``cost_source`` and ``model_id`` and carries the
  optional ``comparison_label`` grouping label.
* ``make_kiro_target``  — a model-compare ``models:`` entry (a bare model id or a
  dict) desugars to a Kiro Target: ``cost_source=kiro_credits``, the standard
  Kiro chat command templates, and ``supports_spec=true`` / ``supports_agents=true``.

Keeping this logic in one place means the config loaders (and the Task-1 tests)
share exactly the same desugaring rules.
"""

from __future__ import annotations

from typing import Any

from .models import CostSource, Pricing, Target, TargetCapabilities

# Standard Kiro CLI chat invocation templates (model-compare default).
_KIRO_BASE_ARGS = ["chat", "--no-interactive", "--trust-all-tools"]
_KIRO_MODEL_FLAG = "--model={model}"
_KIRO_AGENT_FLAG = "--agent={agent}"
_KIRO_EFFORT_FLAG = "--effort={effort}"
# Kiro runs spec-driven work natively in a single invocation. `--mode spec`
# requires the `--v3` flag, so both are injected together for spec tasks only
# (vibe tasks are unaffected).
_KIRO_SPEC_MODE_ARGS = ["--v3", "--mode", "spec"]


def make_kiro_target(
    entry: str | dict[str, Any],
    *,
    default_cli_path: str = "kiro",
    usd_per_credit: float | None = None,
    spec_mode_args: list[str] | None = None,
) -> Target:
    """
    Desugar a model-compare ``models:`` entry into a Kiro Target.

    ``entry`` may be a bare model id string or a dict with keys: ``id``,
    ``display_name``, ``cli_path`` (per-model override), ``extra_args``,
    ``spec_mode_args``, ``env``, ``enabled``.
    """
    if isinstance(entry, str):
        entry = {"id": entry}
    if "id" not in entry:
        raise ValueError("model entry requires an 'id'")

    model_id = entry["id"]
    pricing = Pricing(usd_per_credit=usd_per_credit) if usd_per_credit is not None else Pricing()
    # Per-model override > loader-level override > native default.
    resolved_spec_args = entry.get("spec_mode_args")
    if resolved_spec_args is None:
        resolved_spec_args = spec_mode_args if spec_mode_args is not None else _KIRO_SPEC_MODE_ARGS

    return Target(
        name=entry.get("display_name") or model_id,
        display_name=entry.get("display_name"),
        cli_path=entry.get("cli_path") or default_cli_path,
        model_id=model_id,
        cli_base_args=list(_KIRO_BASE_ARGS),
        cli_model_flag=_KIRO_MODEL_FLAG,
        cli_agent_flag=_KIRO_AGENT_FLAG,
        cli_effort_flag=_KIRO_EFFORT_FLAG,
        extra_args=list(entry.get("extra_args", [])),
        spec_mode_args=list(resolved_spec_args),
        cost_source=CostSource.KIRO_CREDITS,
        pricing=pricing,
        capabilities=TargetCapabilities(supports_spec=True, supports_agents=True),
        env=dict(entry.get("env", {})),
        enabled=bool(entry.get("enabled", True)),
    )


def make_cli_target(entry: dict[str, Any], *, comparison_label: str | None = None) -> Target:
    """
    Desugar a cli-compare ``runners:`` entry into a Target. The runner keeps its
    own ``cost_source`` and ``model_id``; ``comparison_label`` is a reporting label.
    """
    if "name" not in entry:
        raise ValueError("runner entry requires a 'name'")
    if "cli_path" not in entry:
        raise ValueError(f"runner '{entry['name']}' requires a 'cli_path'")
    if "model_id" not in entry:
        raise ValueError(f"runner '{entry['name']}' requires a 'model_id'")

    pricing_raw = entry.get("pricing") or {}
    cost_source = entry.get("cost_source", CostSource.NONE.value)
    if isinstance(cost_source, CostSource):
        cost_source = cost_source.value

    caps_raw = entry.get("capabilities") or {}

    return Target(
        name=entry["name"],
        display_name=entry.get("display_name"),
        cli_path=entry["cli_path"],
        model_id=entry["model_id"],
        cli_base_args=list(entry.get("cli_base_args", [])),
        cli_model_flag=entry.get("cli_model_flag", ""),
        cli_agent_flag=entry.get("cli_agent_flag", ""),
        cli_effort_flag=entry.get("cli_effort_flag", ""),
        extra_args=list(entry.get("extra_args", [])),
        spec_mode_args=list(entry.get("spec_mode_args", [])),
        cost_source=CostSource(cost_source),
        pricing=Pricing(**pricing_raw),
        token_regex=entry.get("token_regex"),
        capabilities=TargetCapabilities(**caps_raw),
        env=dict(entry.get("env", {})),
        enabled=bool(entry.get("enabled", True)),
        comparison_label=comparison_label,
    )


def validate_targets(targets: list[Target]) -> list[Target]:
    """Raise if there are no targets / no enabled targets; else return them."""
    if not targets:
        raise ValueError("At least one target must be specified")
    if not [t for t in targets if t.enabled]:
        raise ValueError("At least one enabled target must be specified")
    return targets
