"""
Usage / cost parsing for each supported CLI, normalized to a common ``Usage``.

Every parser returns cost in BOTH USD and native units (credits / premium
requests) so the reports can always show them side by side.

  Kiro      ──  "▸ Credits: 0.05 • Time: 2s" telemetry line on stderr.
                USD = credits × pricing.usd_per_credit.

  Claude    ──  `claude -p --output-format json` prints a result object with
  Code          total_cost_usd, duration_ms, and a usage{} token block. USD is
                reported directly — no pricing table needed.

  Copilot   ──  `copilot --output-format json` prints JSONL. The terminal
                `result` event carries a sessionId; we read
                ~/.copilot/session-state/<id>/events.jsonl and extract the
                `session.shutdown` event's `totalNanoAiu` — the true AI-credit
                cost (1 AIU = 1 AI Credit = $0.01 USD). This is more accurate
                than the legacy `premiumRequests` multiplier (0.33 for Haiku).

A generic ``tokens`` regex parser and a fixed ``premium_request`` parser let a
new CLI be added from config alone.
"""

from __future__ import annotations

import json
import re
import time as _time
from pathlib import Path

from .models import CostSource, Pricing, Target, Usage

# ---------------------------------------------------------------------------
# Kiro credits/time telemetry
# ---------------------------------------------------------------------------

_CREDITS_RE = re.compile(r"Credits?\s*[:=]\s*([0-9][0-9,]*\.?[0-9]*)", re.IGNORECASE)
_TIME_RE = re.compile(
    r"Time\s*[:=]\s*("
    r"\d+\s*h\s*\d+\s*m\s*\d+(?:\.\d+)?\s*s"
    r"|\d+\s*h\s*\d+(?:\.\d+)?\s*m"
    r"|\d+\s*m\s*\d+(?:\.\d+)?\s*s"
    r"|\d+(?:\.\d+)?\s*h"
    r"|\d+(?:\.\d+)?\s*m"
    r"|\d+(?:\.\d+)?\s*s"
    r"|\d+(?:\.\d+)?"
    r")",
    re.IGNORECASE,
)
_HAS_CREDITS = re.compile(r"Credits?\s*[:=]", re.IGNORECASE)
_HAS_TIME = re.compile(r"\bTime\s*[:=]", re.IGNORECASE)
_H = re.compile(r"(\d+(?:\.\d+)?)\s*h", re.IGNORECASE)
_M = re.compile(r"(\d+(?:\.\d+)?)\s*m", re.IGNORECASE)
_S = re.compile(r"(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _safe_int(val) -> int | None:
    """Convert a value to int if it's numeric, else None."""
    if isinstance(val, (int, float)):
        return int(val)
    return None


def _to_seconds(token: str) -> float | None:
    token = token.strip()
    if not token:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    total = 0.0
    matched = False
    if (h := _H.search(token)):
        total += float(h.group(1)) * 3600
        matched = True
    if (m := _M.search(token)):
        total += float(m.group(1)) * 60
        matched = True
    if (s := _S.search(token)):
        total += float(s.group(1))
        matched = True
    return total if matched else None


def _parse_credits(text: str) -> float | None:
    matches = _CREDITS_RE.findall(text or "")
    if not matches:
        return None
    try:
        return float(matches[-1].replace(",", ""))
    except ValueError:
        return None


def _parse_time_seconds(text: str) -> float | None:
    matches = _TIME_RE.findall(text or "")
    if not matches:
        return None
    return _to_seconds(matches[-1].strip())


def _find_telemetry_line(text: str) -> str | None:
    if not text:
        return None
    found = None
    for line in text.splitlines():
        if _HAS_CREDITS.search(line) and _HAS_TIME.search(line):
            found = line
    return found


def parse_kiro_credits_time(stdout: str, stderr: str = "") -> tuple[float | None, float | None]:
    """Return (credits, time_seconds) from Kiro CLI output. Anchored to the
    combined 'Credits ... Time' telemetry banner; falls back to a per-field scan
    (stderr first) so model-generated stdout text can't override real telemetry."""
    for stream in (stderr, stdout):
        line = _find_telemetry_line(stream)
        if line is not None:
            return _parse_credits(line), _parse_time_seconds(line)
    for stream in (stderr, stdout):
        credits = _parse_credits(stream)
        time_s = _parse_time_seconds(stream)
        if credits is not None or time_s is not None:
            return credits, time_s
    return None, None


def parse_kiro_usage(stdout: str, stderr: str, pricing: Pricing) -> Usage:
    credits, time_s = parse_kiro_credits_time(stdout, stderr)
    cost = None
    if credits is not None and pricing.usd_per_credit is not None:
        cost = credits * pricing.usd_per_credit
    return Usage(cost_usd=cost, seconds=time_s, raw_credits=credits)


# ---------------------------------------------------------------------------
# JSON helpers (Claude / Copilot)
# ---------------------------------------------------------------------------


def _find_json_objects(text: str) -> list[dict]:
    objs: list[dict] = []
    text = (text or "").strip()
    if not text:
        return objs
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return [obj]
        if isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)]
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                objs.append(obj)
        except json.JSONDecodeError:
            continue
    return objs


def parse_claude_usage(stdout: str, stderr: str, pricing: Pricing) -> Usage:
    """Parse `claude -p --output-format json`.

    Claude Code reports cached tokens separately:
      - input_tokens: non-cached input
      - cache_creation_input_tokens: tokens written to cache this turn
      - cache_read_input_tokens: tokens read from cache
    Total input = all three summed.
    """
    objs = _find_json_objects(stdout) or _find_json_objects(stderr)
    result_obj = None
    for o in objs:
        if o.get("type") == "result" or "total_cost_usd" in o:
            result_obj = o
    if result_obj is None and objs:
        result_obj = objs[-1]
    if not result_obj:
        return Usage()

    cost = result_obj.get("total_cost_usd")
    duration_ms = result_obj.get("duration_ms")
    seconds = (duration_ms / 1000.0) if isinstance(duration_ms, (int, float)) else None
    usage = result_obj.get("usage", {}) or {}

    # Sum all input token buckets (non-cached + cache creation + cache read).
    in_tok = _safe_int(usage.get("input_tokens"))
    cache_create = _safe_int(usage.get("cache_creation_input_tokens"))
    cache_read = _safe_int(usage.get("cache_read_input_tokens"))
    total_in = None
    if any(v is not None for v in (in_tok, cache_create, cache_read)):
        total_in = (in_tok or 0) + (cache_create or 0) + (cache_read or 0)

    out_tok = _safe_int(usage.get("output_tokens"))
    return Usage(
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        input_tokens=total_in,
        output_tokens=out_tok,
        seconds=seconds,
    )


# ---------------------------------------------------------------------------
# Copilot JSONL + session-state AIU lookup
# ---------------------------------------------------------------------------

_TOKEN_IN_KEYS = ("input_tokens", "prompt_tokens", "inputTokens", "promptTokens")
_TOKEN_OUT_KEYS = ("output_tokens", "completion_tokens", "outputTokens", "completionTokens")
_PREMIUM_KEYS = ("premiumRequests", "premium_requests", "credits")
_SESSION_DURATION_KEYS = ("sessionDurationMs", "session_duration_ms")


def _dig(obj: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in obj and isinstance(obj[k], (int, float)):
            return obj[k]
    usage = obj.get("usage")
    if isinstance(usage, dict):
        for k in keys:
            if k in usage and isinstance(usage[k], (int, float)):
                return usage[k]
    return None


def _extract_copilot_session_id(stdout: str) -> str | None:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                return obj.get("sessionId")
        except Exception:
            continue
    return None


def read_copilot_session_aiu(session_id: str, home: Path | None = None) -> float | None:
    """
    Read totalNanoAiu from ~/.copilot/session-state/<session_id>/events.jsonl
    and convert to USD. Returns the cost in USD, or None if unavailable.
    1 AIU = 1 AI Credit = $0.01; value stored in nano-AIU (1e9 nanoAIU = 1 AIU).
    """
    if not session_id:
        return None
    base = home or Path.home()
    ev_file = base / ".copilot" / "session-state" / session_id / "events.jsonl"
    if not ev_file.exists():
        return None
    try:
        for line in ev_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "session.shutdown":
                nano_aiu = obj.get("data", {}).get("totalNanoAiu")
                if isinstance(nano_aiu, (int, float)) and nano_aiu > 0:
                    return (nano_aiu / 1_000_000_000) * 0.01
    except Exception:
        pass
    return None


def parse_copilot_usage(stdout: str, stderr: str, pricing: Pricing, home: Path | None = None) -> Usage:
    """
    Parse `copilot --output-format json` (JSONL) and prefer the real
    session-state ``totalNanoAiu`` cost when the session file is present.
    """
    objs = _find_json_objects(stdout) + _find_json_objects(stderr)
    in_tok = out_tok = 0
    measured_premium = None
    saw_tokens = False
    seconds = None
    for o in objs:
        i = _dig(o, _TOKEN_IN_KEYS)
        out = _dig(o, _TOKEN_OUT_KEYS)
        if i is not None:
            in_tok += int(i)
            saw_tokens = True
        if out is not None:
            out_tok += int(out)
            saw_tokens = True
        p = _dig(o, _PREMIUM_KEYS)
        if p is not None:
            measured_premium = (measured_premium or 0.0) + float(p)
        ms = _dig(o, _SESSION_DURATION_KEYS)
        if ms is not None:
            seconds = ms / 1000.0

    premium = measured_premium if measured_premium is not None else pricing.requests_per_run

    cost = None
    if (
        measured_premium is None
        and saw_tokens
        and pricing.usd_per_input_token is not None
        and pricing.usd_per_output_token is not None
    ):
        cost = in_tok * pricing.usd_per_input_token + out_tok * pricing.usd_per_output_token
    elif pricing.usd_per_premium_request is not None:
        cost = premium * pricing.usd_per_premium_request

    raw_credits = None

    # Prefer the accurate session-state AIU cost when available.
    session_id = _extract_copilot_session_id(stdout)
    if session_id:
        real_cost = read_copilot_session_aiu(session_id, home=home)
        if real_cost is not None:
            cost = real_cost
            raw_credits = real_cost / 0.01  # AIU credits driving the cost

    return Usage(
        cost_usd=cost,
        input_tokens=in_tok if saw_tokens else None,
        output_tokens=out_tok if saw_tokens else None,
        premium_requests=premium,
        seconds=seconds,
        raw_credits=raw_credits,
    )


# ---------------------------------------------------------------------------
# kas-proxy metrics.jsonl (cost correlated via X-Kas-Run-Id)
# ---------------------------------------------------------------------------


def _kas_metrics_path(pricing: Pricing) -> Path:
    """Resolve the metrics.jsonl path with the default ~/.kas-proxy fallback."""
    raw = pricing.kas_metrics_file or "~/.kas-proxy/metrics.jsonl"
    return Path(raw).expanduser()


def _find_kas_metrics_record(
    path: Path, run_id: str, timeout_seconds: float, sleep: float = 0.1
) -> dict | None:
    """
    Locate ALL metrics.jsonl records whose ``run_id`` matches, aggregate them,
    and return a single combined dict.

    A single CLI invocation (one run_id) may produce multiple inference turns
    (the agent loop calls the model N times). Each turn writes one record. We
    sum cost/tokens across all records for that run_id so the harness sees the
    total task cost, not just one turn's.

    The proxy may flush the last line a fraction of a second after the CLI
    returns, so we poll the file until we either find at least one record or
    exhaust the timeout.
    """
    if not run_id:
        return None
    deadline = _time.monotonic() + max(0.0, timeout_seconds)
    last_size = -1
    while True:
        if path.exists():
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            if size != last_size:
                last_size = size
                try:
                    records: list[dict] = []
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line or not line.startswith("{"):
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if obj.get("run_id") == run_id:
                                records.append(obj)
                    if records:
                        return _aggregate_kas_records(records)
                except OSError:
                    pass
        if _time.monotonic() >= deadline:
            return None
        _time.sleep(sleep)


def _aggregate_kas_records(records: list[dict]) -> dict:
    """Combine multiple per-turn records from one run_id into one summary.

    Sums: cost_usd, input_tokens, output_tokens, kiro_credits, total_ms.
    Takes the last record's path/model_id (they should all match).
    """
    def _sum_field(field: str) -> float | int | None:
        vals = [r[field] for r in records if r.get(field) is not None]
        return sum(vals) if vals else None

    return {
        "path": records[-1].get("path"),
        "model_id": records[-1].get("model_id"),
        "run_id": records[-1].get("run_id"),
        "cost_usd": _sum_field("cost_usd"),
        "input_tokens": _sum_field("input_tokens"),
        "output_tokens": _sum_field("output_tokens"),
        "kiro_credits": _sum_field("kiro_credits"),
        "total_ms": _sum_field("total_ms"),
        "turns": len(records),
    }


def parse_kas_proxy_metrics_usage(
    pricing: Pricing, run_id: str | None
) -> Usage:
    """
    Read kas-proxy's metrics.jsonl and return Usage for the turn correlated by
    ``run_id`` (an X-Kas-Run-Id header injected by the harness via KAS_RUN_ID).

    Returns an empty ``Usage()`` when no run_id is supplied (e.g. the proxy
    isn't in use) or no matching record is found within the timeout.

    Field mapping (kas-proxy metrics → agent_cost_bench Usage):
      cost_usd          -> Usage.cost_usd            (real billed cost)
      input_tokens      -> Usage.input_tokens
      output_tokens     -> Usage.output_tokens
      kiro_credits      -> Usage.raw_credits          (passthrough only)
      ttft_ms or ttfb_ms -> Usage.seconds             (best available timing)
    Both ``openrouter`` and ``passthrough`` records map cleanly; cost_usd is
    populated by the proxy on both paths (passthrough derived from
    kiro_credits × kiro_credit_price_usd).
    """
    if not run_id:
        return Usage()
    path = _kas_metrics_path(pricing)
    record = _find_kas_metrics_record(
        path, run_id, pricing.kas_metrics_timeout_seconds
    )
    if record is None:
        return Usage()

    cost = record.get("cost_usd")
    input_tokens = record.get("input_tokens")
    output_tokens = record.get("output_tokens")
    raw_credits = record.get("kiro_credits")
    # Sum of total_ms across all inference turns for this run_id — the
    # cumulative time the model spent on inference (excludes tool execution,
    # file I/O, agent orchestration between turns). More meaningful than
    # wall-clock for comparing model speed across different agent strategies.
    total_ms = record.get("total_ms")
    seconds = (total_ms / 1000.0) if isinstance(total_ms, (int, float)) else None

    return Usage(
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        input_tokens=int(input_tokens) if isinstance(input_tokens, (int, float)) else None,
        output_tokens=int(output_tokens) if isinstance(output_tokens, (int, float)) else None,
        seconds=seconds,
        raw_credits=float(raw_credits) if isinstance(raw_credits, (int, float)) else None,
    )


# ---------------------------------------------------------------------------
# Codex CLI JSONL (codex exec --json)
# ---------------------------------------------------------------------------

# Pricing reference: https://developers.openai.com/api/docs/pricing?latest-pricing=standard
# All rates below are for standard (non-batch, non-flex) pricing.
#
# Cost formula (per OpenAI billing):
#   uncached_input = input_tokens - cached_input_tokens
#   cost = (uncached_input      / 1M) × usd_per_input_token
#        + (cached_input_tokens / 1M) × usd_per_cached_input_token
#        + (output_tokens       / 1M) × usd_per_output_token
#
# NOTE: reasoning_output_tokens is a SUBSET of output_tokens — not additive.
# The API bills all output tokens (reasoning + visible) at the same output
# rate. reasoning_output_tokens is an informational breakdown only.
#
# Standard rates per 1M tokens (from OpenAI pricing page, June 2026):
#   Model              input    cached_input    output
#   o4-mini            $1.10       $0.275        $4.40
#   o3                 $2.00       $0.500        $8.00
#   o3-mini            $1.10       $0.550        $4.40
#   o1                $15.00       $7.500       $60.00
#   gpt-5.5            $5.00       $0.500       $30.00
#   gpt-5.5-pro       $30.00          n/a      $180.00
#   gpt-5.4            $2.50       $0.250       $15.00
#   gpt-5.4-mini       $0.75       $0.075        $4.50
#   gpt-5.4-pro       $30.00          n/a      $180.00
#   gpt-4o             $2.50       $1.250       $10.00
#   gpt-4.1            $2.00       $0.500        $8.00


def compute_codex_cost(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,  # informational only — already included in output_tokens
    pricing: Pricing,
) -> float | None:
    """Compute Codex API cost using OpenAI's standard billing formula.

    Pricing reference: https://developers.openai.com/api/docs/pricing?latest-pricing=standard

    The ``Pricing`` fields are **per-token** rates. In the YAML config, express
    them as the published per-1M-token rate divided by 1,000,000
    (e.g. $1.10/1M → ``0.0000011``).

    Formula::

        uncached_input = input_tokens - cached_input_tokens
        cost = uncached_input      × usd_per_input_token
             + cached_input_tokens × usd_per_cached_input_token
             + output_tokens       × usd_per_output_token

    **Important**: ``reasoning_output_tokens`` is a **subset** of
    ``output_tokens`` (already included — not additive). The API bills all
    output tokens at the same rate regardless of whether they are reasoning or
    visible response tokens. The parameter is accepted for call-site
    compatibility but is not used in the cost calculation.

    When ``usd_per_cached_input_token`` is absent, cached tokens are billed at
    the regular input rate (conservative fallback).

    Returns ``None`` when the minimum required rates (input + output) are not
    configured, so callers can distinguish "no pricing set" from a zero cost.
    """
    p_in = pricing.usd_per_input_token
    p_out = pricing.usd_per_output_token
    if p_in is None or p_out is None:
        return None

    # Cached tokens are cheaper; fall back to regular input rate when no
    # separate cached rate is configured.
    p_cached = (
        pricing.usd_per_cached_input_token
        if pricing.usd_per_cached_input_token is not None
        else p_in
    )

    uncached_input = max(0, input_tokens - cached_input_tokens)
    cost = (
        uncached_input        * p_in
        + cached_input_tokens * p_cached
        + output_tokens       * p_out
        # reasoning_output_tokens intentionally omitted — subset of output_tokens
    )
    return cost


def parse_codex_usage(stdout: str, stderr: str, pricing: Pricing) -> Usage:
    """Parse ``codex exec --json`` JSONL output.

    Codex streams JSONL events to stdout. Token usage lives in
    ``turn.completed`` events::

        {"type":"turn.completed","usage":{"input_tokens":N,
            "cached_input_tokens":N,"output_tokens":N,
            "reasoning_output_tokens":N}}

    A task may produce multiple turns (the agent loop calls the model several
    times), so we **sum** tokens across ALL ``turn.completed`` events in the
    output — the same aggregation behaviour as kas-proxy metrics.

    Cost is computed by :func:`compute_codex_cost`:
    ``uncached_input × p_in + cached_input × p_cached + output × p_out``.
    ``reasoning_output_tokens`` is a subset of ``output_tokens`` (already
    counted there), so it is tracked for reporting but not billed separately.

    Pricing reference: https://developers.openai.com/api/docs/pricing?latest-pricing=standard

    Timing: Codex does not emit a wall-clock duration in the JSONL stream.
    ``seconds`` is left ``None``; the harness records wall-clock time separately.
    """
    in_tok = out_tok = reasoning_tok = cached_tok = 0
    saw_usage = False

    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "turn.completed":
            usage = obj.get("usage")
            if not isinstance(usage, dict):
                continue
            saw_usage = True
            in_tok += int(usage.get("input_tokens") or 0)
            cached_tok += int(usage.get("cached_input_tokens") or 0)
            out_tok += int(usage.get("output_tokens") or 0)
            reasoning_tok += int(usage.get("reasoning_output_tokens") or 0)

    if not saw_usage:
        return Usage()

    cost = compute_codex_cost(in_tok, cached_tok, out_tok, reasoning_tok, pricing)

    return Usage(
        cost_usd=cost,
        input_tokens=in_tok or None,
        cached_input_tokens=cached_tok or None,
        output_tokens=out_tok or None,
        reasoning_output_tokens=reasoning_tok or None,
    )





def parse_token_regex_usage(
    stdout: str, stderr: str, pricing: Pricing, token_regex: str | None
) -> Usage:
    if not token_regex:
        return Usage()
    rx = re.compile(token_regex, re.IGNORECASE)
    combined = f"{stderr}\n{stdout}"
    m = None
    for match in rx.finditer(combined):
        m = match
    if not m:
        return Usage()
    gd = m.groupdict()
    in_tok = int(gd["input"]) if gd.get("input") else None
    out_tok = int(gd["output"]) if gd.get("output") else None
    cost = None
    if (
        in_tok is not None
        and out_tok is not None
        and pricing.usd_per_input_token is not None
        and pricing.usd_per_output_token is not None
    ):
        cost = in_tok * pricing.usd_per_input_token + out_tok * pricing.usd_per_output_token
    return Usage(cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok)


def parse_premium_request_usage(pricing: Pricing) -> Usage:
    reqs = pricing.requests_per_run
    cost = (
        reqs * pricing.usd_per_premium_request
        if pricing.usd_per_premium_request is not None
        else None
    )
    return Usage(cost_usd=cost, premium_requests=reqs)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def parse_usage(
    target: Target,
    stdout: str,
    stderr: str = "",
    home: Path | None = None,
    run_id: str | None = None,
) -> Usage:
    """Parse usage for a target according to its ``cost_source``.

    ``run_id`` is the per-turn correlation id used by the kas_proxy_metrics
    parser (passed through X-Kas-Run-Id by the shim, into the proxy's
    metrics.jsonl); ignored by other cost sources.
    """
    src = target.cost_source
    p = target.pricing
    if src == CostSource.KIRO_CREDITS:
        return parse_kiro_usage(stdout, stderr, p)
    if src == CostSource.CLAUDE_JSON:
        return parse_claude_usage(stdout, stderr, p)
    if src == CostSource.COPILOT_JSON:
        return parse_copilot_usage(stdout, stderr, p, home=home)
    if src == CostSource.CODEX_JSON:
        return parse_codex_usage(stdout, stderr, p)
    if src == CostSource.KAS_PROXY_METRICS:
        return parse_kas_proxy_metrics_usage(p, run_id)
    if src == CostSource.TOKENS:
        return parse_token_regex_usage(stdout, stderr, p, target.token_regex)
    if src == CostSource.PREMIUM_REQUEST:
        return parse_premium_request_usage(p)
    return Usage()
