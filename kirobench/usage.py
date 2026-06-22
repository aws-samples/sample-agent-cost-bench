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
    """Parse `claude -p --output-format json`."""
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
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")
    return Usage(
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        input_tokens=int(in_tok) if isinstance(in_tok, (int, float)) else None,
        output_tokens=int(out_tok) if isinstance(out_tok, (int, float)) else None,
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
# Generic token-regex + fixed premium-request parsers
# ---------------------------------------------------------------------------


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


def parse_usage(target: Target, stdout: str, stderr: str = "", home: Path | None = None) -> Usage:
    """Parse usage for a target according to its ``cost_source``."""
    src = target.cost_source
    p = target.pricing
    if src == CostSource.KIRO_CREDITS:
        return parse_kiro_usage(stdout, stderr, p)
    if src == CostSource.CLAUDE_JSON:
        return parse_claude_usage(stdout, stderr, p)
    if src == CostSource.COPILOT_JSON:
        return parse_copilot_usage(stdout, stderr, p, home=home)
    if src == CostSource.TOKENS:
        return parse_token_regex_usage(stdout, stderr, p, target.token_regex)
    if src == CostSource.PREMIUM_REQUEST:
        return parse_premium_request_usage(p)
    return Usage()
