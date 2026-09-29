"""Task 2 — cost/usage parsing tests (USD + native units)."""

from __future__ import annotations

import json

from agent_cost_bench.models import CostSource, Pricing, Target
from agent_cost_bench.targets import make_cli_target
from agent_cost_bench.usage import (
    compute_codex_cost,
    parse_antigravity_usage,
    parse_claude_usage,
    parse_codex_usage,
    parse_copilot_usage,
    parse_devin_usage,
    parse_kiro_usage,
    parse_pi_usage,
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


def _kiro_v3_turn(credits: float, elapsed_ms: int) -> str:
    """One Kiro v3 stream-json turn_completion event as a JSONL line."""
    return json.dumps({
        "type": "sessionUpdate",
        "data": {"update": {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {
                "promptTurnSummaries": [
                    {"unit": "credit", "unitPlural": "credits", "usage": credits}
                ],
                "elapsedTime": elapsed_ms,
                "kind": "turn_completion",
            }},
        }},
    })


def test_kiro_v3_stream_json_credits():
    # kiro-cli 2.23.0 (v3) emits no banner; credits arrive in a turn_completion
    # event's promptTurnSummaries. Cost falls back to the stream-json path.
    stdout = (
        '{"type":"runStarted"}\n'
        + _kiro_v3_turn(1.10991, 10862)
        + '\n{"type":"runFinished","data":{"status":"success"}}\n'
    )
    u = parse_kiro_usage(stdout, "", Pricing(usd_per_credit=0.04))
    assert abs(u.raw_credits - 1.10991) < 1e-9
    assert abs(u.cost_usd - 1.10991 * 0.04) < 1e-9
    # elapsedTime is per-turn model time, NOT run latency — must not be reported
    # as seconds (the harness uses wall-clock instead).
    assert u.seconds is None


def test_kiro_v3_stream_json_sums_multiple_turns():
    # Defensive: if a run ever emits more than one turn_completion, sum credits.
    stdout = _kiro_v3_turn(1.0, 5000) + "\n" + _kiro_v3_turn(0.5, 3000) + "\n"
    u = parse_kiro_usage(stdout, "", Pricing(usd_per_credit=0.04))
    assert abs(u.raw_credits - 1.5) < 1e-9
    assert u.seconds is None


def test_kiro_stream_json_wins_and_suppresses_bogus_time():
    # v3 stream-json output contains credit/time SUBSTRINGS that the v2 banner
    # field-scan would otherwise mis-scrape as a tiny bogus latency (the per-turn
    # elapsedTime, not a run total). When turn_completion events are present the
    # stream-json path must win for credits AND force seconds=None so the harness
    # falls back to wall-clock. This is the regression that made Kiro look ~2s.
    stdout = (
        "Credits: 0.5\n"          # stray substrings the loose scan would catch
        "elapsed 16.693 s\n"
        + _kiro_v3_turn(24.384, 16693) + "\n"
    )
    u = parse_kiro_usage(stdout, "", Pricing(usd_per_credit=0.04))
    assert abs(u.raw_credits - 24.384) < 1e-9      # from turn_completion, not "0.5"
    assert u.seconds is None                        # bogus 16.693s suppressed


def test_kiro_no_telemetry_returns_none():
    u = parse_kiro_usage("Hi", "", Pricing(usd_per_credit=0.04))
    assert u.raw_credits is None
    assert u.cost_usd is None


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


def _copilot_aiu_event(nano: int, *, dupe_paths: bool = True) -> dict:
    """A `model.model_call_success` event carrying a turn's AIU cost. Real output
    puts the same value at data.copilotUsage.total_nano_aiu AND
    data.responseChunk.copilot_usage.total_nano_aiu — the parser must read it
    once per event, not sum both."""
    data: dict = {"copilotUsage": {"total_nano_aiu": nano}}
    if dupe_paths:
        data["responseChunk"] = {"copilot_usage": {"total_nano_aiu": nano}}
    return {"type": "model.model_call_success", "data": data}


def test_copilot_no_aiu_reports_no_cost():
    """Without any AIU telemetry, cost is None. Copilot cost is AIU-only — a
    streamed premiumRequests value is ignored entirely (not turned into cost,
    not reported as premium_requests). Tokens/timing are still reported."""
    lines = [
        json.dumps({"type": "turn", "usage": {"input_tokens": 100, "output_tokens": 50}}),
        json.dumps({"type": "result", "premiumRequests": 0.33, "sessionDurationMs": 5000}),
    ]
    u = parse_copilot_usage("\n".join(lines), "", Pricing())
    assert u.cost_usd is None            # no misleading 0.33 * 0.04 fallback
    assert u.premium_requests is None    # premiumRequests no longer scraped
    assert u.seconds == 5.0
    assert u.input_tokens == 100


def test_copilot_inline_aiu_single_turn():
    """Newer CLIs stream per-turn total_nano_aiu in model.model_call_success.
    2e9 nanoAIU = 2 AIU = $0.02."""
    stdout = "\n".join([
        json.dumps({"type": "turn", "usage": {"input_tokens": 100, "output_tokens": 50}}),
        json.dumps(_copilot_aiu_event(2_000_000_000)),
    ])
    u = parse_copilot_usage(stdout, "", Pricing())
    assert abs(u.cost_usd - 0.02) < 1e-9
    assert abs(u.raw_credits - 2.0) < 1e-9


def test_copilot_inline_aiu_summed_across_turns_once_per_event():
    """Per-turn values are summed; the duplicate copilotUsage /
    responseChunk.copilot_usage paths in ONE event are NOT double counted."""
    stdout = "\n".join([
        json.dumps(_copilot_aiu_event(30_241_625_000)),  # turn 0
        json.dumps(_copilot_aiu_event(14_671_100_000)),  # turn 1
        json.dumps(_copilot_aiu_event(10_931_250_000)),  # turn 2
    ])
    u = parse_copilot_usage(stdout, "", Pricing())
    total_nano = 30_241_625_000 + 14_671_100_000 + 10_931_250_000
    assert abs(u.raw_credits - total_nano / 1e9) < 1e-6
    assert abs(u.cost_usd - (total_nano / 1e9) * 0.01) < 1e-9


def test_copilot_inline_aiu_single_path_still_counted():
    """An event that carries the value at only one of the two paths still counts."""
    stdout = json.dumps(_copilot_aiu_event(5_000_000_000, dupe_paths=False))
    u = parse_copilot_usage(stdout, "", Pricing())
    assert abs(u.cost_usd - 0.05) < 1e-9
    assert abs(u.raw_credits - 5.0) < 1e-9


def test_copilot_inline_aiu_preferred_over_session_state(tmp_path):
    """When both inline AIU and a session-state record exist, the inline stream
    (authoritative billed cost for this run) wins."""
    session_id = "sess-123"
    ev_dir = tmp_path / ".copilot" / "session-state" / session_id
    ev_dir.mkdir(parents=True)
    (ev_dir / "events.jsonl").write_text(
        json.dumps({"type": "session.shutdown", "data": {"totalNanoAiu": 999_000_000_000}}) + "\n"
    )
    stdout = "\n".join([
        json.dumps(_copilot_aiu_event(3_000_000_000)),
        json.dumps({"type": "result", "sessionId": session_id}),
    ])
    u = parse_copilot_usage(stdout, "", Pricing(), home=tmp_path)
    assert abs(u.cost_usd - 0.03) < 1e-9  # inline 3 AIU, not the 999 AIU session record


def test_copilot_session_state_aiu_used_when_no_inline(tmp_path):
    """Older CLIs with no inline AIU fall back to the session-state record,
    located via the result event's sessionId."""
    session_id = "sess-123"
    ev_dir = tmp_path / ".copilot" / "session-state" / session_id
    ev_dir.mkdir(parents=True)
    (ev_dir / "events.jsonl").write_text(
        json.dumps({"type": "session.shutdown", "data": {"totalNanoAiu": 2_000_000_000}}) + "\n"
    )
    stdout = json.dumps({"type": "result", "sessionId": session_id})
    u = parse_copilot_usage(stdout, "", Pricing(), home=tmp_path)
    # 2e9 nanoAIU = 2 AIU = $0.02.
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


# ---------------------------------------------------------------------------
# codex_json: cache-write tokens must not be billed as fresh input
# ---------------------------------------------------------------------------


def test_codex_cache_write_billed_at_cache_write_rate():
    """`cache_write_input_tokens` is a distinct slice of input_tokens and must be
    priced separately. Billing it as fresh input overstates cost substantially
    (observed ~20% on a real run, where cache writes were 702k of 703k
    "uncached" tokens)."""
    pricing = Pricing(
        usd_per_input_token=0.000005,
        usd_per_cached_input_token=0.0000005,
        usd_per_cache_write_token=0.00000625,
        usd_per_output_token=0.00003,
    )
    stdout = _codex_line(
        input_tokens=100_000,
        cached_input_tokens=90_000,
        cache_write_input_tokens=9_500,
        output_tokens=1_000,
    )
    u = parse_codex_usage(stdout, "", pricing)
    assert u.cache_write_input_tokens == 9_500
    # fresh = 100000 - 90000 - 9500 = 500
    expected = (
        500 * 0.000005
        + 90_000 * 0.0000005
        + 9_500 * 0.00000625
        + 1_000 * 0.00003
    )
    assert abs(u.cost_usd - expected) < 1e-12


def test_codex_cache_write_defaults_to_cached_rate_not_input_rate():
    """With no explicit cache-write rate, cache writes fall back to the CACHED
    rate (OpenAI charges no cache-write premium) — never the fresh-input rate."""
    pricing = Pricing(
        usd_per_input_token=0.000005,
        usd_per_cached_input_token=0.0000005,
        usd_per_output_token=0.00003,
    )
    cost = compute_codex_cost(
        100_000, 90_000, 1_000, 0, pricing, cache_write_input_tokens=9_500
    )
    expected = (
        500 * 0.000005 + 90_000 * 0.0000005 + 9_500 * 0.0000005 + 1_000 * 0.00003
    )
    assert abs(cost - expected) < 1e-12
    # Sanity: the old (buggy) behaviour billed cache writes at the input rate.
    buggy = 10_000 * 0.000005 + 90_000 * 0.0000005 + 1_000 * 0.00003
    assert cost < buggy
# devin_export cost source
# ---------------------------------------------------------------------------


# Opus 4.8/5 through Devin: $5 / MTok in, $25 / MTok out (`devin models list`).
_DEVIN_PRICING = dict(
    usd_per_input_token=0.000005,
    usd_per_cached_input_token=0.0000005,
    usd_per_output_token=0.000025,
)


def _write_devin_export(workspace, *, filename="devin-usage.json", **metrics):
    """Write a minimal ATIF export with the given final_metrics."""
    export = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "devin", "model_name": "Claude Opus 5"},
        "steps": [],
        "final_metrics": metrics,
    }
    path = workspace / filename
    path.write_text(json.dumps(export), encoding="utf-8")
    return path


def test_devin_export_cost(tmp_path):
    """cost = uncached_input × p_in + cached × p_cached + completion × p_out.
    total_prompt_tokens INCLUDES total_cached_tokens (same as Codex).
    """
    _write_devin_export(
        tmp_path,
        total_prompt_tokens=19249, total_completion_tokens=1204,
        total_cached_tokens=12262, total_steps=9,
    )
    u = parse_devin_usage(Pricing(**_DEVIN_PRICING), tmp_path)
    assert u.input_tokens == 19249
    assert u.cached_input_tokens == 12262
    assert u.output_tokens == 1204
    # uncached = 19249 - 12262 = 6987
    expected = 6987 * 0.000005 + 12262 * 0.0000005 + 1204 * 0.000025
    assert abs(u.cost_usd - expected) < 1e-12


def test_devin_export_no_cached_rate_falls_back_to_input_rate(tmp_path):
    """Without usd_per_cached_input_token, cached tokens bill at the input rate."""
    _write_devin_export(
        tmp_path,
        total_prompt_tokens=5000, total_completion_tokens=300,
        total_cached_tokens=2000,
    )
    pricing = Pricing(usd_per_input_token=0.000005, usd_per_output_token=0.000025)
    u = parse_devin_usage(pricing, tmp_path)
    # All 5000 prompt tokens at p_in (no cached discount)
    expected = 5000 * 0.000005 + 300 * 0.000025
    assert abs(u.cost_usd - expected) < 1e-12


def test_devin_export_cached_exceeding_prompt_clamps_to_zero(tmp_path):
    """A cached count larger than the prompt total must not produce a negative
    uncached charge."""
    _write_devin_export(
        tmp_path,
        total_prompt_tokens=1000, total_completion_tokens=0,
        total_cached_tokens=4000,
    )
    u = parse_devin_usage(Pricing(**_DEVIN_PRICING), tmp_path)
    assert u.cost_usd == 4000 * 0.0000005


def test_devin_export_no_pricing_returns_tokens_only(tmp_path):
    """Without pricing rates, token counts are still reported (cost_usd=None)."""
    _write_devin_export(
        tmp_path,
        total_prompt_tokens=5000, total_completion_tokens=300,
        total_cached_tokens=1000,
    )
    u = parse_devin_usage(Pricing(), tmp_path)
    assert u.input_tokens == 5000
    assert u.cached_input_tokens == 1000
    assert u.output_tokens == 300
    assert u.cost_usd is None


def test_devin_export_missing_file_returns_empty_usage(tmp_path):
    """A run where the CLI died before writing the export reports no cost data
    rather than a misleading zero."""
    u = parse_devin_usage(Pricing(**_DEVIN_PRICING), tmp_path)
    assert u.cost_usd is None
    assert u.input_tokens is None


def test_devin_export_no_workspace_returns_empty_usage():
    assert parse_devin_usage(Pricing(**_DEVIN_PRICING), None).cost_usd is None


def test_devin_export_malformed_json_returns_empty_usage(tmp_path):
    (tmp_path / "devin-usage.json").write_text("{not json", encoding="utf-8")
    u = parse_devin_usage(Pricing(**_DEVIN_PRICING), tmp_path)
    assert u.cost_usd is None
    assert u.input_tokens is None


def test_devin_export_missing_final_metrics_returns_empty_usage(tmp_path):
    """An export truncated before final_metrics (e.g. session still running)."""
    (tmp_path / "devin-usage.json").write_text(
        json.dumps({"schema_version": "ATIF-v1.7", "steps": []}), encoding="utf-8"
    )
    u = parse_devin_usage(Pricing(**_DEVIN_PRICING), tmp_path)
    assert u.cost_usd is None
    assert u.input_tokens is None


def test_devin_export_honours_custom_export_filename(tmp_path):
    """pricing.devin_export_file must match the runner's --export path."""
    _write_devin_export(
        tmp_path, filename="usage.json",
        total_prompt_tokens=2000, total_completion_tokens=100,
        total_cached_tokens=0,
    )
    default = parse_devin_usage(Pricing(**_DEVIN_PRICING), tmp_path)
    assert default.input_tokens is None  # looked for devin-usage.json
    custom = Pricing(**_DEVIN_PRICING, devin_export_file="usage.json")
    assert parse_devin_usage(custom, tmp_path).input_tokens == 2000


def test_devin_dispatch_via_parse_usage(tmp_path):
    """parse_usage routes CostSource.DEVIN_EXPORT to the devin parser, passing
    the run's workspace through so the export can be located."""
    _write_devin_export(
        tmp_path,
        total_prompt_tokens=8000, total_completion_tokens=500,
        total_cached_tokens=3000,
    )
    t = make_cli_target({
        "name": "devin",
        "cli_path": "devin",
        "model_id": "claude-opus-4-8",
        "pricing": _DEVIN_PRICING,
    })
    assert t.cost_source == CostSource.DEVIN_EXPORT  # inferred from cli_path
    u = parse_usage(t, "", "", workspace=tmp_path)
    assert u.input_tokens == 8000
    assert u.cached_input_tokens == 3000
    assert u.output_tokens == 500
    expected = 5000 * 0.000005 + 3000 * 0.0000005 + 500 * 0.000025
    assert abs(u.cost_usd - expected) < 1e-12


# ---------------------------------------------------------------------------
# antigravity_json cost source (`agy -p "..." --output-format json`)
# ---------------------------------------------------------------------------


# Example per-token rates (divide a published per-1M rate by 1,000,000).
_AGY_PRICING = dict(
    usd_per_input_token=0.00000125,        # $1.25  / 1M (fresh input)
    usd_per_cached_input_token=0.0000003125,  # $0.3125 / 1M (cache read)
    usd_per_output_token=0.00001,          # $10.00 / 1M
)


def _agy_result(**usage_fields) -> str:
    """Serialize a single `agy --output-format json` result object."""
    obj = {
        "conversation_id": "978c29ed-26db-46fd-ac6e-3c2be0438f3e",
        "status": "SUCCESS",
        "response": "some answer",
        "duration_seconds": 5.909435,
        "num_turns": 1,
        "usage": usage_fields,
    }
    return json.dumps(obj)


def test_antigravity_cost():
    """cost = input × p_in + cache_read × p_cached + output × p_out.
    thinking_tokens is a SUBSET of output_tokens — not billed separately.
    """
    stdout = _agy_result(
        input_tokens=5563, output_tokens=1250,
        thinking_tokens=611, cache_read_tokens=8130, total_tokens=6813,
    )
    u = parse_antigravity_usage(stdout, "", Pricing(**_AGY_PRICING))
    # Total input processed = fresh input + cache reads.
    assert u.input_tokens == 5563 + 8130
    assert u.cached_input_tokens == 8130
    assert u.output_tokens == 1250
    assert u.reasoning_output_tokens == 611  # informational only
    assert abs(u.seconds - 5.909435) < 1e-9
    expected = 5563 * 0.00000125 + 8130 * 0.0000003125 + 1250 * 0.00001
    assert abs(u.cost_usd - expected) < 1e-12


def test_antigravity_no_cached_rate_falls_back_to_input_rate():
    """Without usd_per_cached_input_token, cache reads bill at the input rate."""
    stdout = _agy_result(
        input_tokens=1000, output_tokens=200,
        thinking_tokens=0, cache_read_tokens=500, total_tokens=1700,
    )
    pricing = Pricing(usd_per_input_token=0.00000125, usd_per_output_token=0.00001)
    u = parse_antigravity_usage(stdout, "", pricing)
    expected = 1000 * 0.00000125 + 500 * 0.00000125 + 200 * 0.00001
    assert abs(u.cost_usd - expected) < 1e-12


def test_antigravity_thinking_tokens_not_double_billed():
    """thinking_tokens live inside output_tokens; cost ignores them."""
    stdout = _agy_result(
        input_tokens=1000, output_tokens=800,
        thinking_tokens=600, cache_read_tokens=0, total_tokens=1800,
    )
    u = parse_antigravity_usage(stdout, "", Pricing(**_AGY_PRICING))
    expected = 1000 * 0.00000125 + 800 * 0.00001
    assert abs(u.cost_usd - expected) < 1e-12
    assert u.reasoning_output_tokens == 600  # still captured for reporting


def test_antigravity_no_pricing_returns_tokens_only():
    """Without pricing rates, token counts are still reported (cost_usd=None)."""
    stdout = _agy_result(
        input_tokens=1000, output_tokens=200,
        thinking_tokens=50, cache_read_tokens=300, total_tokens=1500,
    )
    u = parse_antigravity_usage(stdout, "", Pricing())
    assert u.input_tokens == 1300
    assert u.cached_input_tokens == 300
    assert u.output_tokens == 200
    assert u.cost_usd is None


def test_antigravity_empty_output_returns_empty_usage():
    u = parse_antigravity_usage("", "", Pricing(**_AGY_PRICING))
    assert u.cost_usd is None
    assert u.input_tokens is None


def test_antigravity_dispatch_via_parse_usage():
    """parse_usage routes CostSource.ANTIGRAVITY_JSON (inferred from `agy`)."""
    stdout = _agy_result(
        input_tokens=2000, output_tokens=300,
        thinking_tokens=100, cache_read_tokens=1000, total_tokens=3300,
    )
    t = make_cli_target({
        "name": "antigravity",
        "cli_path": "agy",
        "model_id": "default",
        "pricing": _AGY_PRICING,
    })
    assert t.cost_source == CostSource.ANTIGRAVITY_JSON  # inferred from cli_path
    u = parse_usage(t, stdout, "")
    assert u.input_tokens == 2000 + 1000
    assert u.cached_input_tokens == 1000
    assert u.output_tokens == 300
    expected = 2000 * 0.00000125 + 1000 * 0.0000003125 + 300 * 0.00001
    assert abs(u.cost_usd - expected) < 1e-12


# ---------------------------------------------------------------------------
# pi_json cost source (`pi -p --mode json`)
# ---------------------------------------------------------------------------


def _pi_usage(inp, out, cache_read=0, cache_write=0, cost_total=None):
    """Build a pi `usage` block; omit `cost` entirely when cost_total is None."""
    usage = {
        "input": inp,
        "output": out,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": inp + out + cache_read + cache_write,
    }
    if cost_total is not None:
        usage["cost"] = {
            "input": 0.0, "output": 0.0, "cacheRead": 0.0,
            "cacheWrite": 0.0, "total": cost_total,
        }
    return usage


def _pi_stream(turns, *, timestamps=None, include_agent_end=True) -> str:
    """Serialize a pi `--mode json` JSONL stream from a list of turn usages.

    Each turn emits the `message_end` + `turn_end` pair a real run produces, so
    the tests also prove `message_end` is not double-counted.
    """
    lines = []
    msgs = []
    for i, usage in enumerate(turns):
        ts = (timestamps or [1789580503571 + i * 1000 for i in range(len(turns))])[i]
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "api": "bedrock-converse-stream",
            "provider": "amazon-bedrock",
            "model": "global.anthropic.claude-sonnet-5",
            "usage": usage,
            "stopReason": "stop",
            "timestamp": ts,
        }
        msgs.append(msg)
        lines.append(json.dumps({"type": "turn_start"}))
        lines.append(json.dumps({"type": "message_end", "message": msg}))
        lines.append(json.dumps({"type": "turn_end", "message": msg, "toolResults": []}))
    if include_agent_end:
        lines.append(json.dumps({"type": "agent_end", "messages": msgs, "willRetry": False}))
    lines.append(json.dumps({"type": "agent_settled"}))
    return "\n".join(lines) + "\n"


def test_pi_prefers_reported_cost_and_sums_turns():
    """pi prices each turn itself, so cost is the sum of the reported totals —
    no pricing config required. message_end must not be counted twice."""
    stdout = _pi_stream([
        _pi_usage(3, 70, cache_read=0, cache_write=6512, cost_total=0.025479),
        _pi_usage(1, 16, cache_read=6512, cache_write=91, cost_total=0.00253785),
    ])
    u = parse_pi_usage(stdout, "", Pricing())
    assert abs(u.cost_usd - (0.025479 + 0.00253785)) < 1e-12
    # Total input processed = fresh input + cache reads + cache writes.
    assert u.input_tokens == (3 + 0 + 6512) + (1 + 6512 + 91)
    assert u.cached_input_tokens == 6512 + 6512 + 91
    assert u.output_tokens == 70 + 16
    assert abs(u.seconds - 1.0) < 1e-9  # timestamp span, ms → s


def test_pi_reported_cost_wins_over_configured_rates():
    """Configured per-token rates are a fallback only — never override the
    cost the CLI reports (which comes from its own model catalog)."""
    stdout = _pi_stream([_pi_usage(1000, 200, cache_read=5000, cost_total=0.5)])
    u = parse_pi_usage(
        stdout, "",
        Pricing(usd_per_input_token=0.1, usd_per_output_token=0.1),
    )
    assert abs(u.cost_usd - 0.5) < 1e-12


def test_pi_falls_back_to_token_pricing_when_no_cost_reported():
    """An unpriced model reports no `cost` block; per-token rates fill the gap.
    cacheRead and cacheWrite are billed at their own rates."""
    stdout = _pi_stream([_pi_usage(1000, 200, cache_read=5000, cache_write=800)])
    pricing = Pricing(
        usd_per_input_token=0.000002,          # $2.00 / 1M
        usd_per_cached_input_token=0.0000002,  # $0.20 / 1M
        usd_per_cache_write_token=0.0000025,   # $2.50 / 1M
        usd_per_output_token=0.00001,          # $10.00 / 1M
    )
    u = parse_pi_usage(stdout, "", pricing)
    expected = (
        1000 * 0.000002 + 5000 * 0.0000002 + 800 * 0.0000025 + 200 * 0.00001
    )
    assert abs(u.cost_usd - expected) < 1e-12


def test_pi_no_cost_and_no_pricing_returns_tokens_only():
    stdout = _pi_stream([_pi_usage(1000, 200, cache_read=300)])
    u = parse_pi_usage(stdout, "", Pricing())
    assert u.cost_usd is None
    assert u.input_tokens == 1300
    assert u.cached_input_tokens == 300
    assert u.output_tokens == 200


def test_pi_zero_cost_with_real_tokens_is_not_trusted():
    """A reported cost of exactly 0 alongside real token usage means the route
    priced nothing, not that the turn was free — fall back to configured rates
    rather than publishing $0.00."""
    stdout = _pi_stream([_pi_usage(1000, 200, cache_read=300, cost_total=0.0)])
    pricing = Pricing(usd_per_input_token=0.000004, usd_per_output_token=0.00002)
    u = parse_pi_usage(stdout, "", pricing)
    expected = 1000 * 0.000004 + 300 * 0.000004 + 200 * 0.00002
    assert abs(u.cost_usd - expected) < 1e-12
    # …and with no rates configured it is unknown, NOT zero.
    assert parse_pi_usage(stdout, "", Pricing()).cost_usd is None


def test_pi_failed_turn_zero_tokens_zero_cost_is_genuinely_free():
    """Bedrock rejecting the request emits stopReason:"error" with all-zero
    usage AND all-zero cost. Nothing was billed, so $0.00 is the right answer."""
    stdout = _pi_stream([_pi_usage(0, 0, cost_total=0.0)])
    u = parse_pi_usage(stdout, "", Pricing())
    assert u.cost_usd == 0.0
    assert u.input_tokens is None
    assert u.output_tokens is None


def test_pi_falls_back_to_agent_end_when_no_turn_end():
    """A run killed mid-turn has no turn_end; the agent_end summary is used."""
    msg = {
        "role": "assistant",
        "usage": _pi_usage(10, 20, cost_total=0.001),
        "timestamp": 1789580503571,
    }
    stdout = "\n".join([
        json.dumps({"type": "turn_start"}),
        json.dumps({"type": "agent_end", "messages": [
            {"role": "user", "content": [], "timestamp": 1789580503000},
            msg,
        ], "willRetry": False}),
    ])
    u = parse_pi_usage(stdout, "", Pricing())
    assert abs(u.cost_usd - 0.001) < 1e-12
    assert u.output_tokens == 20


def test_pi_empty_output_returns_empty_usage():
    u = parse_pi_usage("", "", Pricing())
    assert u.cost_usd is None
    assert u.input_tokens is None
    assert u.output_tokens is None


def test_pi_ignores_unrelated_events():
    """Streaming message_update events carry zeroed usage and no turn data —
    they must not be mistaken for a turn."""
    stdout = "\n".join([
        json.dumps({"type": "message_update", "usage": _pi_usage(0, 0, cost_total=0.0),
                    "assistantMessageEvent": {"type": "text_delta", "delta": "hi"}}),
        json.dumps({"type": "agent_settled"}),
    ])
    u = parse_pi_usage(stdout, "", Pricing())
    assert u.cost_usd is None
    assert u.input_tokens is None


def test_pi_dispatch_via_parse_usage():
    """parse_usage routes CostSource.PI_JSON (inferred from the `pi` binary)."""
    stdout = _pi_stream([_pi_usage(2000, 300, cache_read=1000, cost_total=0.0123)])
    t = make_cli_target({
        "name": "pi",
        "cli_path": "pi",
        "model_id": "global.anthropic.claude-sonnet-5",
    })
    assert t.cost_source == CostSource.PI_JSON  # inferred from cli_path
    u = parse_usage(t, stdout, "")
    assert abs(u.cost_usd - 0.0123) < 1e-12
    assert u.input_tokens == 3000
    assert u.cached_input_tokens == 1000
    assert u.output_tokens == 300
