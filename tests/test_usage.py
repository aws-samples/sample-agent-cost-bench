"""Task 2 — cost/usage parsing tests (USD + native units)."""

from __future__ import annotations

import json

from agent_cost_bench.models import CostSource, Pricing, Target
from agent_cost_bench.targets import make_cli_target
from agent_cost_bench.usage import (
    parse_claude_usage,
    parse_codex_usage,
    compute_codex_cost,
    parse_copilot_usage,
    parse_kiro_usage,
    parse_token_regex_usage,
    parse_usage,
)


def _target(cost_source, **pricing):
    return make_cli_target(
        {
            "name": "t",
            "cli_path": "x",
            "model_id": "m",
            "cost_source": cost_source,
            "pricing": pricing,
        }
    )


def test_kiro_credits_and_usd():
    stderr = "some output\n▸ Credits: 0.05 • Time: 2s\n"
    u = parse_kiro_usage("", stderr, Pricing(usd_per_credit=0.04))
    assert abs(u.raw_credits - 0.05) < 1e-9
    assert abs(u.cost_usd - 0.05 * 0.04) < 1e-9
    assert u.seconds == 2.0


def test_kiro_ignores_model_generated_text_prefers_telemetry_banner():
    # A model echoing "Credits:" in stdout must not override the real banner.
    stdout = "Here is the plan. Credits: 999\n"
    stderr = "▸ Credits: 0.10 • Time: 1m 3s\n"
    u = parse_kiro_usage(stdout, stderr, Pricing(usd_per_credit=0.04))
    assert abs(u.raw_credits - 0.10) < 1e-9
    assert u.seconds == 63.0


def test_claude_json_total_cost_usd():
    obj = {
        "type": "result",
        "total_cost_usd": 0.0123,
        "duration_ms": 4200,
        "usage": {"input_tokens": 1200, "output_tokens": 350},
    }
    u = parse_claude_usage(json.dumps(obj), "", Pricing())
    assert abs(u.cost_usd - 0.0123) < 1e-9
    assert u.input_tokens == 1200
    assert u.output_tokens == 350
    assert abs(u.seconds - 4.2) < 1e-9


def test_copilot_jsonl_premium_request_fallback():
    lines = [
        json.dumps({"type": "turn", "usage": {"input_tokens": 100, "output_tokens": 50}}),
        json.dumps({"type": "result", "premiumRequests": 0.33, "sessionDurationMs": 5000}),
    ]
    u = parse_copilot_usage("\n".join(lines), "", Pricing(usd_per_premium_request=0.04))
    assert abs(u.premium_requests - 0.33) < 1e-9
    assert abs(u.cost_usd - 0.33 * 0.04) < 1e-9
    assert u.seconds == 5.0
    assert u.input_tokens == 100


def test_copilot_session_state_aiu_overrides(tmp_path):
    # Write a fake session-state events.jsonl with totalNanoAiu.
    session_id = "sess-123"
    ev_dir = tmp_path / ".copilot" / "session-state" / session_id
    ev_dir.mkdir(parents=True)
    (ev_dir / "events.jsonl").write_text(
        json.dumps({"type": "session.shutdown", "data": {"totalNanoAiu": 2_000_000_000}}) + "\n"
    )
    stdout = json.dumps({"type": "result", "sessionId": session_id, "premiumRequests": 0.33})
    u = parse_copilot_usage(stdout, "", Pricing(usd_per_premium_request=0.04), home=tmp_path)
    # 2e9 nanoAIU = 2 AIU = $0.02, more accurate than premiumRequests * price.
    assert abs(u.cost_usd - 0.02) < 1e-9
    assert abs(u.raw_credits - 2.0) < 1e-9


def test_token_regex_pricing():
    text = "tokens in=1000 out=500"
    u = parse_token_regex_usage(
        text,
        "",
        Pricing(usd_per_input_token=0.000001, usd_per_output_token=0.000002),
        r"in=(?P<input>\d+)\s+out=(?P<output>\d+)",
    )
    assert u.input_tokens == 1000
    assert u.output_tokens == 500
    assert abs(u.cost_usd - (1000 * 1e-6 + 500 * 2e-6)) < 1e-12


def test_premium_request_fixed():
    t = _target("premium_request", usd_per_premium_request=0.04, requests_per_run=2)
    u = parse_usage(t, "", "")
    assert u.premium_requests == 2
    assert abs(u.cost_usd - 0.08) < 1e-9


def test_dispatch_none_source_returns_empty():
    t = Target(name="t", cli_path="x", model_id="m", cost_source=CostSource.NONE)
    u = parse_usage(t, "anything", "")
    assert u.cost_usd is None and u.raw_credits is None


# ---------------------------------------------------------------------------
# kas_proxy_metrics cost source
# ---------------------------------------------------------------------------


def _write_metrics(path, *records):
    """Write a kas-proxy-style metrics.jsonl with the given records."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_kas_proxy_metrics_openrouter_record(tmp_path):
    """A routed turn's record (cost_usd from OpenRouter, no kiro_credits) maps
    cleanly into Usage."""
    from agent_cost_bench.usage import parse_kas_proxy_metrics_usage

    mfile = tmp_path / "metrics.jsonl"
    _write_metrics(
        mfile,
        {
            "ts": 1.0,
            "path": "openrouter",
            "model_id": "glm-5",
            "run_id": "abc-123",
            "cost_usd": 0.0421,
            "input_tokens": 12700,
            "output_tokens": 980,
            "total_ms": 8200.0,
            "ttft_ms": 1830,
            "ttfb_ms": 1830,
            "kiro_credits": None,
        },
    )
    pricing = Pricing(kas_metrics_file=str(mfile), kas_metrics_timeout_seconds=0.0)
    u = parse_kas_proxy_metrics_usage(pricing, run_id="abc-123")
    assert abs(u.cost_usd - 0.0421) < 1e-9
    assert u.input_tokens == 12700
    assert u.output_tokens == 980
    assert abs(u.seconds - 8.2) < 1e-9  # total_ms summed across turns
    assert u.raw_credits is None


def test_kas_proxy_metrics_passthrough_record(tmp_path):
    """A passthrough record carries kiro_credits and a derived cost_usd; both
    surface on Usage so the report can show credits AND dollars."""
    from agent_cost_bench.usage import parse_kas_proxy_metrics_usage

    mfile = tmp_path / "metrics.jsonl"
    _write_metrics(
        mfile,
        {
            "ts": 2.0,
            "path": "passthrough",
            "model_id": "claude-opus-4.8",
            "run_id": "xyz-789",
            "cost_usd": 0.05,             # derived from kiro_credits × price
            "kiro_credits": 1.25,
            "input_tokens": None,
            "output_tokens": None,
            "total_ms": 3500.0,
            "ttfb_ms": 2200,
        },
    )
    pricing = Pricing(kas_metrics_file=str(mfile), kas_metrics_timeout_seconds=0.0)
    u = parse_kas_proxy_metrics_usage(pricing, run_id="xyz-789")
    assert abs(u.cost_usd - 0.05) < 1e-9
    assert abs(u.raw_credits - 1.25) < 1e-9
    assert abs(u.seconds - 3.5) < 1e-9  # total_ms summed
    assert u.input_tokens is None


def test_kas_proxy_metrics_picks_correct_run_id(tmp_path):
    """When multiple records share a file, only the one with our run_id is
    returned — no timestamp-window heuristics needed."""
    from agent_cost_bench.usage import parse_kas_proxy_metrics_usage

    mfile = tmp_path / "metrics.jsonl"
    _write_metrics(
        mfile,
        {"ts": 1.0, "path": "openrouter", "run_id": "first", "cost_usd": 0.01,
         "kiro_credits": None, "input_tokens": 10, "output_tokens": 5},
        {"ts": 2.0, "path": "openrouter", "run_id": "second", "cost_usd": 0.02,
         "kiro_credits": None, "input_tokens": 20, "output_tokens": 10},
        {"ts": 3.0, "path": "openrouter", "run_id": "third", "cost_usd": 0.03,
         "kiro_credits": None, "input_tokens": 30, "output_tokens": 15},
    )
    pricing = Pricing(kas_metrics_file=str(mfile), kas_metrics_timeout_seconds=0.0)
    u = parse_kas_proxy_metrics_usage(pricing, run_id="second")
    assert abs(u.cost_usd - 0.02) < 1e-9
    assert u.output_tokens == 10


def test_kas_proxy_metrics_aggregates_multi_turn(tmp_path):
    """A single CLI invocation (one run_id) may produce multiple inference
    turns. The parser sums cost and tokens across all records with that id."""
    from agent_cost_bench.usage import parse_kas_proxy_metrics_usage

    mfile = tmp_path / "metrics.jsonl"
    _write_metrics(
        mfile,
        # Turn 1: initial code generation
        {"ts": 1.0, "path": "openrouter", "run_id": "multi-turn-id",
         "cost_usd": 0.025, "kiro_credits": None,
         "input_tokens": 13000, "output_tokens": 1600, "total_ms": 24000.0},
        # Different run_id (another task running concurrently)
        {"ts": 1.5, "path": "openrouter", "run_id": "other-task",
         "cost_usd": 0.01, "kiro_credits": None,
         "input_tokens": 5000, "output_tokens": 200, "total_ms": 5000.0},
        # Turn 2: follow-up (same run_id)
        {"ts": 2.0, "path": "openrouter", "run_id": "multi-turn-id",
         "cost_usd": 0.007, "kiro_credits": None,
         "input_tokens": 15000, "output_tokens": 270, "total_ms": 8800.0},
    )
    pricing = Pricing(kas_metrics_file=str(mfile), kas_metrics_timeout_seconds=0.0)
    u = parse_kas_proxy_metrics_usage(pricing, run_id="multi-turn-id")
    # Should sum both turns, not just the last
    assert abs(u.cost_usd - 0.032) < 1e-9          # 0.025 + 0.007
    assert u.input_tokens == 28000                   # 13000 + 15000
    assert u.output_tokens == 1870                   # 1600 + 270
    assert abs(u.seconds - 32.8) < 1e-9             # (24000 + 8800) / 1000
    assert u.raw_credits is None                     # OpenRouter path has no credits


def test_kas_proxy_metrics_missing_record_returns_empty_usage(tmp_path):
    """No matching record (e.g. proxy isn't running) → empty Usage rather than
    a crash. The run isn't lost, just costless."""
    from agent_cost_bench.usage import parse_kas_proxy_metrics_usage

    mfile = tmp_path / "metrics.jsonl"
    _write_metrics(
        mfile,
        {"ts": 1.0, "path": "openrouter", "run_id": "other", "cost_usd": 0.01,
         "kiro_credits": None, "input_tokens": 10, "output_tokens": 5},
    )
    pricing = Pricing(kas_metrics_file=str(mfile), kas_metrics_timeout_seconds=0.0)
    u = parse_kas_proxy_metrics_usage(pricing, run_id="never-existed")
    assert u.cost_usd is None and u.raw_credits is None


def test_kas_proxy_metrics_dispatch_via_parse_usage(tmp_path):
    """parse_usage routes to the new parser when cost_source=kas_proxy_metrics
    and threads run_id through."""
    mfile = tmp_path / "metrics.jsonl"
    _write_metrics(
        mfile,
        {"ts": 1.0, "path": "openrouter", "run_id": "thread-it-through",
         "cost_usd": 0.07, "kiro_credits": None,
         "input_tokens": 100, "output_tokens": 50, "ttft_ms": 1500},
    )
    t = Target(
        name="t", cli_path="x", model_id="glm-5",
        cost_source=CostSource.KAS_PROXY_METRICS,
        pricing=Pricing(kas_metrics_file=str(mfile), kas_metrics_timeout_seconds=0.0),
    )
    u = parse_usage(t, "", "", run_id="thread-it-through")
    assert abs(u.cost_usd - 0.07) < 1e-9
    assert u.output_tokens == 50


def test_kas_proxy_metrics_no_run_id_returns_empty(tmp_path):
    """Defensive: a missing/empty run_id (e.g. legacy caller) returns empty
    Usage rather than scanning every record."""
    from agent_cost_bench.usage import parse_kas_proxy_metrics_usage

    mfile = tmp_path / "metrics.jsonl"
    _write_metrics(mfile, {"path": "openrouter", "run_id": "x", "cost_usd": 0.5})
    pricing = Pricing(kas_metrics_file=str(mfile))
    assert parse_kas_proxy_metrics_usage(pricing, run_id=None).cost_usd is None
    assert parse_kas_proxy_metrics_usage(pricing, run_id="").cost_usd is None


# ---------------------------------------------------------------------------
# codex_json cost source
# ---------------------------------------------------------------------------



def _codex_line(**usage_fields) -> str:
    return json.dumps({"type": "turn.completed", "usage": usage_fields})


def test_codex_single_turn_cost():
    """Single turn: cost = uncached_input × p_in + cached × p_cached + output × p_out.
    reasoning_output_tokens is a SUBSET of output_tokens — not billed separately.
    Pricing ref: https://developers.openai.com/api/docs/pricing?latest-pricing=standard
    """
    # Simulates real o4-mini output: input=10000 (2000 cached), output=592 (256 reasoning)
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "abc"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        _codex_line(input_tokens=10000, cached_input_tokens=2000,
                    output_tokens=592, reasoning_output_tokens=256),
    ])
    # o4-mini standard rates: $1.10 / $0.275 / $4.40 per 1M tokens
    pricing = Pricing(
        usd_per_input_token=0.0000011,
        usd_per_cached_input_token=0.000000275,
        usd_per_output_token=0.0000044,
    )
    u = parse_codex_usage(stdout, "", pricing)
    assert u.input_tokens == 10000
    assert u.cached_input_tokens == 2000
    assert u.output_tokens == 592
    assert u.reasoning_output_tokens == 256  # informational only
    # uncached = 10000 - 2000 = 8000; reasoning already in output_tokens
    expected = 8000 * 0.0000011 + 2000 * 0.000000275 + 592 * 0.0000044
    assert abs(u.cost_usd - expected) < 1e-12


def test_codex_multi_turn_sums_all_turns():
    """Multi-turn: tokens summed across all turn.completed events.
    Real output from `codex exec --json -m o4-mini` benchmark run.
    """
    stdout = "\n".join([
        _codex_line(input_tokens=14441, cached_input_tokens=0,
                    output_tokens=656, reasoning_output_tokens=448),
        _codex_line(input_tokens=10014, cached_input_tokens=9344,
                    output_tokens=351, reasoning_output_tokens=128),
    ])
    pricing = Pricing(
        usd_per_input_token=0.0000011,
        usd_per_cached_input_token=0.000000275,
        usd_per_output_token=0.0000044,
    )
    u = parse_codex_usage(stdout, "", pricing)
    assert u.input_tokens == 14441 + 10014
    assert u.cached_input_tokens == 0 + 9344
    assert u.output_tokens == 656 + 351
    assert u.reasoning_output_tokens == 448 + 128  # informational only
    # Turn 1: all uncached (cached=0)
    # Turn 2: uncached = 10014 - 9344 = 670
    expected = (
        14441 * 0.0000011 + 0 * 0.000000275 + 656 * 0.0000044
        + 670 * 0.0000011 + 9344 * 0.000000275 + 351 * 0.0000044
    )
    assert abs(u.cost_usd - expected) < 1e-12


def test_codex_no_cached_rate_falls_back_to_input_rate():
    """When usd_per_cached_input_token is absent, cached tokens are billed at
    the regular input rate (conservative fallback)."""
    stdout = _codex_line(input_tokens=5000, cached_input_tokens=2000,
                         output_tokens=300, reasoning_output_tokens=0)
    pricing = Pricing(
        usd_per_input_token=0.0000011,
        # no usd_per_cached_input_token → falls back to input rate
        usd_per_output_token=0.0000044,
    )
    u = parse_codex_usage(stdout, "", pricing)
    # All 5000 input tokens at p_in (no cached discount)
    expected = 5000 * 0.0000011 + 300 * 0.0000044
    assert abs(u.cost_usd - expected) < 1e-12


def test_codex_reasoning_tokens_not_double_billed():
    """Verify reasoning tokens are NOT charged separately — they're already in
    output_tokens. Cost equals the simple uncached + cached + output formula."""
    stdout = _codex_line(input_tokens=5000, cached_input_tokens=0,
                         output_tokens=800, reasoning_output_tokens=600)
    pricing = Pricing(
        usd_per_input_token=0.0000011,
        usd_per_cached_input_token=0.000000275,
        usd_per_output_token=0.0000044,
    )
    u = parse_codex_usage(stdout, "", pricing)
    # reasoning (600) is inside output (800) — no extra charge
    expected = 5000 * 0.0000011 + 800 * 0.0000044
    assert abs(u.cost_usd - expected) < 1e-12
    assert u.reasoning_output_tokens == 600  # still captured for reporting


def test_codex_no_pricing_returns_tokens_only():
    """Without pricing rates, usage still reports token counts (cost_usd=None)."""
    stdout = _codex_line(input_tokens=5000, cached_input_tokens=1000,
                         output_tokens=300, reasoning_output_tokens=0)
    u = parse_codex_usage(stdout, "", Pricing())
    assert u.input_tokens == 5000
    assert u.cached_input_tokens == 1000
    assert u.output_tokens == 300
    assert u.cost_usd is None


def test_codex_empty_output_returns_empty_usage():
    pricing = Pricing(usd_per_input_token=0.0000011, usd_per_output_token=0.0000044)
    u = parse_codex_usage("", "", pricing)
    assert u.cost_usd is None
    assert u.input_tokens is None


def test_compute_codex_cost_formula():
    """Verify compute_codex_cost directly — reasoning tokens not double-billed."""
    from agent_cost_bench.usage import compute_codex_cost

    pricing = Pricing(
        usd_per_input_token=0.0000011,
        usd_per_cached_input_token=0.000000275,
        usd_per_output_token=0.0000044,
    )
    # 10000 input, 2000 cached → 8000 uncached; 592 output (256 reasoning subset)
    cost = compute_codex_cost(10000, 2000, 592, 256, pricing)
    expected = 8000 * 0.0000011 + 2000 * 0.000000275 + 592 * 0.0000044
    assert abs(cost - expected) < 1e-12

    # All cached (e.g. second turn of idempotent task)
    cost_all_cached = compute_codex_cost(5000, 5000, 100, 0, pricing)
    expected_all_cached = 5000 * 0.000000275 + 100 * 0.0000044
    assert abs(cost_all_cached - expected_all_cached) < 1e-12

    # No pricing → None
    assert compute_codex_cost(1000, 0, 100, 0, Pricing()) is None


def test_codex_dispatch_via_parse_usage():
    """parse_usage routes CostSource.CODEX_JSON to the codex parser."""
    stdout = _codex_line(input_tokens=2000, cached_input_tokens=500,
                         output_tokens=200, reasoning_output_tokens=50)
    t = make_cli_target({
        "name": "codex",
        "cli_path": "codex",
        "model_id": "o4-mini",
        "cost_source": "codex_json",
        "pricing": {
            "usd_per_input_token": 0.0000011,
            "usd_per_cached_input_token": 0.000000275,
            "usd_per_output_token": 0.0000044,
        },
    })
    u = parse_usage(t, stdout, "")
    assert u.input_tokens == 2000
    assert u.cached_input_tokens == 500
    assert u.output_tokens == 200
    assert u.reasoning_output_tokens == 50  # informational only
    # uncached = 2000 - 500 = 1500; reasoning already in output_tokens
    expected = 1500 * 0.0000011 + 500 * 0.000000275 + 200 * 0.0000044
    assert abs(u.cost_usd - expected) < 1e-12
