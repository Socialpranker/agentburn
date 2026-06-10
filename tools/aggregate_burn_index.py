#!/usr/bin/env python3
"""Aggregate burn-index issue submissions → data/burn-index.json.

Runs in the weekly Action: `gh api` dumps issues labeled burn-index to a file,
this script extracts the ```json blocks, applies plausibility bounds (junk and
flexing get dropped, not ranked), and publishes per-metric quantiles. Pure
stdlib; testable offline: python3 tools/aggregate_burn_index.py issues.json out.json
"""

from __future__ import annotations

import json
import re
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from agentburn.burnindex import BOUNDS, METRICS, SCHEMA  # noqa: E402

BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def extract_submissions(issues: list) -> list:
    subs = []
    for issue in issues:
        body = issue.get("body") or ""
        m = BLOCK.search(body)
        if not m:
            continue
        try:
            payload = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if payload.get("schema") != SCHEMA:
            continue
        clean = {}
        for key in METRICS:
            v = payload.get(key)
            lo, hi = BOUNDS[key]
            if isinstance(v, (int, float)) and lo <= v <= hi:
                clean[key] = float(v)
        if clean:
            clean["spend_band"] = str(payload.get("spend_band", "unknown"))[:12]
            subs.append(clean)
    return subs


def quantiles(values: list) -> dict:
    vs = sorted(values)
    n = len(vs)

    def q(p):
        if n == 0:
            return None
        i = min(n - 1, max(0, round(p / 100 * (n - 1))))
        return round(vs[i], 4)

    return {"n": n, "p25": q(25), "p50": q(50), "p75": q(75), "p90": q(90)}


def aggregate(issues: list) -> dict:
    subs = extract_submissions(issues)
    metrics = {}
    for key in METRICS:
        vals = [s[key] for s in subs if key in s]
        if len(vals) >= 5:
            metrics[key] = quantiles(vals)
        else:
            metrics[key] = {"n": len(vals)}
    bands = {}
    for s in subs:
        bands[s["spend_band"]] = bands.get(s["spend_band"], 0) + 1
    return {
        "schema": SCHEMA,
        "as_of": time.strftime("%Y-%m-%d"),
        "n": len(subs),
        "metrics": metrics,
        "spend_bands": bands,
        "note": "Efficiency percentiles from anonymous agentburn --submit payloads; "
                "plausibility-bounded; volumes are never ranked.",
    }


if __name__ == "__main__":
    issues_path, out_path = sys.argv[1], sys.argv[2]
    with open(issues_path, "r", encoding="utf-8") as f:
        issues = json.load(f)
    result = aggregate(issues)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    print(f"burn-index: {result['n']} valid submission(s) → {out_path}")
