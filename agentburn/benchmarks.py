"""Community-measured reference points, embedded as dated constants.

These are NOT our measurements. Each constant carries its public source and
date; the report cites them verbatim so users can calibrate "is my number
normal?" without agentburn ever touching the network.
"""

from __future__ import annotations

# Phala / Clawdi token benchmark of an always-on agent (OpenClaw-class),
# published 2026-03-10: https://phala.com/posts/understanding-openclaws-token-usage
PHALA_2026_03 = {
    "source": "Phala token benchmark, 2026-03",
    "url": "https://phala.com/posts/understanding-openclaws-token-usage",
    "baseline_tokens_per_call": 8_000,  # instruction/bootstrap baseline resent per request
    "multi_turn_5x_cost_factor": 13.3,  # 5-turn dialog vs single turn
    "output_share_typical": 0.06,  # output is 1–6% of tokens
}

# Hermes Agent community measurement (user-reported, issue #4379, 2026):
# https://github.com/NousResearch/hermes-agent/issues/4379
HERMES_4379 = {
    "source": "hermes-agent #4379 (user-measured)",
    "url": "https://github.com/NousResearch/hermes-agent/issues/4379",
    "fixed_overhead_tokens": 13_935,
    "overhead_share": 0.73,
}

REFERENCE_BASELINE = PHALA_2026_03["baseline_tokens_per_call"]


def overhead_vs_reference_short(avg_input_per_call: int) -> str:
    """Card-friendly: '2.5× the community norm (≈8k)' — no nested citations."""
    if avg_input_per_call <= 0:
        return ""
    ratio = avg_input_per_call / REFERENCE_BASELINE
    return f"{ratio:.1f}× the community norm (≈{REFERENCE_BASELINE // 1000}k)"


def overhead_vs_reference(avg_input_per_call: int) -> str:
    """One-line calibration against the community baseline."""
    if avg_input_per_call <= 0:
        return ""
    delta = avg_input_per_call / REFERENCE_BASELINE - 1
    sign = "+" if delta >= 0 else "−"
    return (
        f"community baseline ≈{REFERENCE_BASELINE // 1000}k/call "
        f"({PHALA_2026_03['source']}): {sign}{abs(delta):.0%}"
    )
