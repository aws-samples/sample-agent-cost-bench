"""
Statistical helpers for repeat-aware reporting (pass@1, pass@k, pass^k, Wilson CI).

When each (task × target) instance is run k times, a single pass rate hides
whether a target is reliably correct or flaky. These helpers compute the metrics
that make multi-repeat results trustworthy:

- pass@1            : expected success of a single attempt (mean over all runs)
- pass^k (all)      : fraction of instances where ALL k repeats passed (consistency)
- any-pass@k        : fraction of instances where AT LEAST ONE repeat passed
- pass@k (unbiased) : HumanEval-style estimator of "≥1 of k passes"
- Wilson interval   : confidence interval for a pass rate
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def pass_at_k(n: int, c: int, k: int) -> float:
    """
    Unbiased pass@k estimator (HumanEval, Chen et al. 2021):
    the probability ≥1 of k samples is correct, given c correct out of n.

        pass@k = 1 - C(n - c, k) / C(n, k)
    """
    if n <= 0 or k <= 0 or c <= 0:
        return 0.0
    if n - c < k:
        return 1.0
    if k > n:
        return 1.0 if c > 0 else 0.0
    return 1.0 - (math.comb(n - c, k) / math.comb(n, k))


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stddev(values: list[float]) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    m = mean(values)
    var = sum((v - m) ** 2 for v in values) / len(values)
    return math.sqrt(var)


def wilson_interval(passes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion (default 95%)."""
    if total == 0:
        return (0.0, 0.0)
    p = passes / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    margin = (z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass
class RepeatStats:
    instances: int
    repeats: int
    total_runs: int
    total_passes: int
    pass_at_1: float
    pass_all_k: float
    any_pass_k: float
    pass_at_k_unbiased: float
    pass_rate_stddev: float
    ci_low: float
    ci_high: float

    def to_dict(self) -> dict:
        return {
            "instances": self.instances,
            "repeats": self.repeats,
            "total_runs": self.total_runs,
            "total_passes": self.total_passes,
            "pass_at_1": self.pass_at_1,
            "pass_all_k": self.pass_all_k,
            "any_pass_k": self.any_pass_k,
            "pass_at_k_unbiased": self.pass_at_k_unbiased,
            "pass_rate_stddev": self.pass_rate_stddev,
            "ci95_low": self.ci_low,
            "ci95_high": self.ci_high,
        }


def compute_repeat_stats(instance_outcomes: list[list[bool]]) -> RepeatStats:
    """
    Compute repeat-aware stats from per-instance pass outcomes.

    ``instance_outcomes`` is a list (one per instance) of lists of booleans
    (one per repeat), e.g. [[True, True, False], [True, True, True]].
    """
    instances = len(instance_outcomes)
    total_runs = sum(len(o) for o in instance_outcomes)
    total_passes = sum(sum(1 for x in o if x) for o in instance_outcomes)
    max_k = max((len(o) for o in instance_outcomes), default=0)

    if instances == 0 or total_runs == 0:
        return RepeatStats(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    per_instance_rates = [mean([1.0 if x else 0.0 for x in o]) for o in instance_outcomes]
    pass_all = mean([1.0 if all(o) else 0.0 for o in instance_outcomes])
    any_pass = mean([1.0 if any(o) else 0.0 for o in instance_outcomes])
    unbiased = mean(
        [pass_at_k(len(o), sum(1 for x in o if x), len(o)) for o in instance_outcomes]
    )
    low, high = wilson_interval(total_passes, total_runs)

    return RepeatStats(
        instances=instances,
        repeats=max_k,
        total_runs=total_runs,
        total_passes=total_passes,
        pass_at_1=total_passes / total_runs,
        pass_all_k=pass_all,
        any_pass_k=any_pass,
        pass_at_k_unbiased=unbiased,
        pass_rate_stddev=stddev(per_instance_rates),
        ci_low=low,
        ci_high=high,
    )
