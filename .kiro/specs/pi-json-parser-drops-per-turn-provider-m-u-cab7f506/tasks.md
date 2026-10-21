# Implementation Plan: Cost reports from pi_json runs cannot be audited: when turn_end usage is aggregated, the per-turn provider/model and the split between cach…

## Overview

Implements option **A — Additive usage_events in the JSON report, parser-local** of the Decision Packet for Unit `U-cab7f506`, at blast radius R3.

Work the tasks in order. Tick a checkbox when its task is done AND verified — the checkbox is the only progress record, and every Kiro surface reads it from this file.

## Tasks

- [x] 1. Add optional usage_events to Usage and RunResult
  - Usage.usage_events: dict | None = None
  - RunResult.usage_events: dict | None = None
  - Copy across where runner maps Usage onto RunResult
  - to_dict emits key only when not None
  - python3 -m pytest tests/test_models.py
  - _Requirements: 4.1_

- [x] 2. Record allowlisted per-turn events in parse_pi_usage
  - Allowlist constant of emitted keys
  - Append per turn_end; tag agent_end fallback
  - Keep message_end ignored
  - python3 -m pytest tests/test_usage.py -k pi
  - _Requirements: 1.1, 1.2, 3.1, 3.2_

- [x] 3. Compute cost_basis, completeness and partial_reasons
  - Derive from existing saw_cost / zero-cost / rates / agent_end branches
  - Empty stream -> partial, no_usage_in_stream
  - python3 -m pytest tests/test_usage.py -k pi
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 4. Tests: reconciliation, sentinel exclusion, message_end duplicate, back-compat fixture
  - Property-style test over generated turn lists: sum == cost_usd
  - Sentinel strings in content/thinking/toolCall/toolResult/errorMessage absent from json.dumps(usage_events)
  - Pre-change report fixture loads via report command
  - python3 -m pytest
  - _Requirements: 2.1, 3.1, 3.2, 4.1, 4.2_

- [x] 5. Document usage_events in README cost-source section
  - Field shape, cost_basis values, reason codes
  - python3 -m pytest
  - _Requirements: 2.3_
