"""
agent_cost_bench — a unified benchmark framework with two modes:

  * cli-compare   — run the SAME model through multiple coding CLIs (Kiro,
                    Claude Code, GitHub Copilot) on the same vibe tasks and
                    compare cost in USD (and native units) per task / success.

  * model-compare — run multiple models inside the Kiro CLI across vibe AND
                    spec-driven tasks and compare quality (functional, spec
                    quality, task completion, steering, optional LLM judge)
                    alongside cost (credits + USD).

Both modes share one internal ``Target`` abstraction, one execution core, one
evaluation core, and a unified result model. Cost is always reported both ways:
USD and native units (credits / premium requests).
"""

__version__ = "0.1.0"
__author__ = "Kiro Team"
