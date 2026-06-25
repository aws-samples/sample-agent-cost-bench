"""Task 2 — repeat statistics tests (pass@k matches HumanEval estimator)."""

from __future__ import annotations

from agent_cost_bench.stats import compute_repeat_stats, pass_at_k, wilson_interval


def test_pass_at_k_humaneval_known_values():
    # n=5, c=1, k=1 -> 0.2 ; k=5 -> 1.0 (the one correct must appear)
    assert abs(pass_at_k(5, 1, 1) - 0.2) < 1e-9
    assert abs(pass_at_k(5, 1, 5) - 1.0) < 1e-9
    # n=2, c=1, k=2 -> 1.0 (only one fail, can't fill a 2-fail set)
    assert abs(pass_at_k(2, 1, 2) - 1.0) < 1e-9
    # No correct samples -> 0
    assert pass_at_k(5, 0, 3) == 0.0


def test_pass_at_k_general_formula():
    # n=10, c=3, k=2 : 1 - C(7,2)/C(10,2) = 1 - 21/45
    assert abs(pass_at_k(10, 3, 2) - (1 - 21 / 45)) < 1e-9


def test_wilson_interval_bounds():
    low, high = wilson_interval(5, 10)
    assert 0.0 <= low <= 0.5 <= high <= 1.0


def test_compute_repeat_stats_consistency():
    # 2 instances, 3 repeats each.
    outcomes = [[True, True, False], [True, True, True]]
    s = compute_repeat_stats(outcomes)
    assert s.instances == 2
    assert s.repeats == 3
    assert s.total_runs == 6
    assert s.total_passes == 5
    assert abs(s.pass_at_1 - 5 / 6) < 1e-9
    # pass^k (all repeats pass): only the 2nd instance -> 0.5
    assert abs(s.pass_all_k - 0.5) < 1e-9
    # any-pass@k: both instances had at least one pass -> 1.0
    assert abs(s.any_pass_k - 1.0) < 1e-9


def test_compute_repeat_stats_empty():
    s = compute_repeat_stats([])
    assert s.instances == 0 and s.total_runs == 0 and s.pass_at_1 == 0.0
