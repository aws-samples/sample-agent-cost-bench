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
# When using kiro-cli-plus, the wrapper already invokes `kiro-cli chat --v3`, so
# we must NOT include `chat` again in the base args — only flags.
_KIRO_PLUS_BASE_ARGS = ["--no-interactive", "--trust-all-tools"]
_KIRO_MODEL_FLAG = "--model={model}"
_KIRO_AGENT_FLAG = "--agent={agent}"
_KIRO_EFFORT_FLAG = "--effort={effort}"
# Kiro runs spec-driven work natively in a single invocation. `--mode spec`
# requires the `--v3` flag, so both are injected together for spec tasks only
# (vibe tasks are unaffected).
_KIRO_SPEC_MODE_ARGS = ["--v3", "--mode", "spec"]


def _infer_cost_source(cli_path: str, pricing: dict[str, Any]) -> CostSource:
    """Infer a runner's cost_source from its binary name and pricing fields.

    Detection rules (first match wins):

    1. Binary ends with ``kiro-cli`` or ``kiro`` → ``kiro_credits``
    2. Binary ends with ``claude``               → ``claude_json``
    3. Binary ends with ``copilot``              → ``copilot_json``
    4. Binary ends with ``codex``                → ``codex_json``
    5. Pricing has ``usd_per_input_token``
       AND ``usd_per_output_token``              → ``tokens``
    6. Pricing has ``usd_per_premium_request``   → ``premium_request``
    7. Fallback                                  → ``none``

    An explicit ``cost_source`` in the YAML always takes precedence over
    inference — this function is only called when the field is absent.
    """
    basename = cli_path.rstrip("/").rsplit("/", 1)[-1].lower()
    # Strip common suffixes like ".exe" on Windows.
    stem = basename.split(".")[0]

    if stem in ("kiro-cli", "kiro"):
        return CostSource.KIRO_CREDITS
    if stem == "claude":
        return CostSource.CLAUDE_JSON
    if stem == "copilot":
        return CostSource.COPILOT_JSON
    if stem == "codex":
        return CostSource.CODEX_JSON
    if stem in ("cursor", "cursor-agent"):
        return CostSource.CURSOR_JSON
    # Generic per-token pricing (any CLI that reports token counts via regex).
    if pricing.get("usd_per_input_token") and pricing.get("usd_per_output_token"):
        return CostSource.TOKENS
    # Fixed premium-request pricing.
    if pricing.get("usd_per_premium_request"):
        return CostSource.PREMIUM_REQUEST
    return CostSource.NONE


def make_kiro_target(
    entry: str | dict[str, Any],
    *,
    default_cli_path: str = "kiro",
    usd_per_credit: float | None = None,
    spec_mode_args: list[str] | None = None,
    use_kas_proxy_metrics: bool = False,
    kas_metrics_file: str | None = None,
    kas_metrics_timeout_seconds: float = 5.0,
) -> Target:
    """
    Desugar a model-compare ``models:`` entry into a Kiro Target.

    ``entry`` may be a bare model id string or a dict with keys: ``id``,
    ``display_name``, ``cli_path`` (per-model override), ``extra_args``,
    ``spec_mode_args``, ``env``, ``enabled``.

    When ``use_kas_proxy_metrics`` is true, the resulting target uses
    ``cost_source=KAS_PROXY_METRICS`` instead of ``KIRO_CREDITS`` so cost comes
    from kas-proxy's metrics.jsonl (works for both Kiro-passthrough and
    OpenRouter-routed turns). ``usd_per_credit`` is still recorded on the
    target's pricing in that case so passthrough metrics can be cross-checked.
    """
    if isinstance(entry, str):
        entry = {"id": entry}
    if "id" not in entry:
        raise ValueError("model entry requires an 'id'")

    model_id = entry["id"]
    pricing_kwargs: dict[str, Any] = {}
    if usd_per_credit is not None:
        pricing_kwargs["usd_per_credit"] = usd_per_credit
    if use_kas_proxy_metrics:
        pricing_kwargs["kas_metrics_file"] = kas_metrics_file
        pricing_kwargs["kas_metrics_timeout_seconds"] = kas_metrics_timeout_seconds
    pricing = Pricing(**pricing_kwargs) if pricing_kwargs else Pricing()
    cost_source = (
        CostSource.KAS_PROXY_METRICS if use_kas_proxy_metrics else CostSource.KIRO_CREDITS
    )
    # kiro-cli-plus already runs `kiro-cli chat --v3`, so we must NOT include
    # `chat` again in base args. Detect by checking if the cli path ends with
    # "kiro-cli-plus" (basename match, works with absolute paths too).
    resolved_cli = entry.get("cli_path") or default_cli_path
    is_plus_wrapper = resolved_cli.rstrip("/").endswith("kiro-cli-plus")
    base_args = list(_KIRO_PLUS_BASE_ARGS if is_plus_wrapper else _KIRO_BASE_ARGS)
    # Per-model override > loader-level override > native default.
    resolved_spec_args = entry.get("spec_mode_args")
    if resolved_spec_args is None:
        resolved_spec_args = spec_mode_args if spec_mode_args is not None else _KIRO_SPEC_MODE_ARGS

    return Target(
        name=entry.get("display_name") or model_id,
        display_name=entry.get("display_name"),
        cli_path=resolved_cli,
        model_id=model_id,
        cli_base_args=base_args,
        cli_model_flag=_KIRO_MODEL_FLAG,
        cli_agent_flag=_KIRO_AGENT_FLAG,
        cli_effort_flag=_KIRO_EFFORT_FLAG,
        extra_args=list(entry.get("extra_args", [])),
        spec_mode_args=list(resolved_spec_args),
        cost_source=cost_source,
        pricing=pricing,
        capabilities=TargetCapabilities(supports_spec=True, supports_agents=True),
        env=dict(entry.get("env", {})),
        enabled=bool(entry.get("enabled", True)),
    )


def make_cli_target(entry: dict[str, Any], *, comparison_label: str | None = None) -> Target:
    """
    Desugar a cli-compare ``runners:`` entry into a Target. The runner keeps its
    own ``cost_source`` and ``model_id``; ``comparison_label`` is a reporting label.

    ``cost_source`` is optional in the YAML: when absent it is inferred from the
    ``cli_path`` binary name and the ``pricing`` fields via
    :func:`_infer_cost_source`, so most runners work without any explicit
    ``cost_source`` entry.
    """
    if "name" not in entry:
        raise ValueError("runner entry requires a 'name'")
    if "cli_path" not in entry:
        raise ValueError(f"runner '{entry['name']}' requires a 'cli_path'")
    if "model_id" not in entry:
        raise ValueError(f"runner '{entry['name']}' requires a 'model_id'")

    pricing_raw = entry.get("pricing") or {}

    # cost_source: explicit value in YAML wins; otherwise infer from binary name
    # and pricing fields so users don't need to know this internal detail.
    if "cost_source" in entry:
        raw_cs = entry["cost_source"]
        cost_source = CostSource(raw_cs.value if isinstance(raw_cs, CostSource) else raw_cs)
    else:
        cost_source = _infer_cost_source(entry["cli_path"], pricing_raw)

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
        cost_source=cost_source,
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
