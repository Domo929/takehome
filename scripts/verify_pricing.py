#!/usr/bin/env python3
"""Check llm/pricing.py against Google's Cloud Billing Catalog API.

Every cost figure in FINDINGS rests on the rates in `llm/pricing.py`, and those started
as numbers copied from documentation. Documentation was wrong about grounding by 40%,
and the disagreement was only visible because two sources quoted different values.

The catalog API is the same data the invoice is generated from, so it settles rates
without waiting a day for billing to land and without spending anything. It is public
pricing rather than account usage - this reads no spend data.

Run it when a rate is in doubt, or after a model change.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

VERTEX_SERVICE = "C7E2-9256-1C43"

# (label, SKU description substring, expected USD per unit, unit scale for display)
CHECKS = [
    ("input /1M tokens", "Gemini 2.5 Flash GA Text Input - Predictions", 0.30, 1e6),
    ("output /1M tokens", "Gemini 2.5 Flash GA Text Output - Predictions", 2.50, 1e6),
    ("thinking /1M tokens", "Gemini 2.5 Flash GA Thinking Text Output - Predictions", 2.50, 1e6),
    ("cached input /1M", "Gemini 2.5 Flash GA Input Text Caching", 0.03, 1e6),
    ("grounding /1k prompts", "LLM Grounding with Google Search tool - Predictions", 35.0, 1e3),
]


def token() -> str:
    out = subprocess.run(
        ["gcloud", "auth", "print-access-token"], capture_output=True, text=True
    )
    if out.returncode:
        sys.exit("gcloud auth print-access-token failed; run `gcloud auth login`")
    return out.stdout.strip()


def fetch_skus(tok: str) -> list[dict]:
    skus, page = [], ""
    while True:
        url = (
            f"https://cloudbilling.googleapis.com/v1/services/{VERTEX_SERVICE}/skus"
            f"?pageSize=5000{'&pageToken=' + page if page else ''}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        data = json.load(urllib.request.urlopen(req))
        skus += data.get("skus", [])
        page = data.get("nextPageToken") or ""
        if not page:
            return skus


def main() -> None:
    skus = fetch_skus(token())
    print(f"Vertex AI catalog: {len(skus):,} SKUs\n")
    print(f"  {'rate':<24}{'in pricing.py':>15}{'catalog':>12}  SKU")

    failures = 0
    for label, needle, expected, scale in CHECKS:
        match = next((s for s in skus if s["description"].strip() == needle), None)
        if match is None:
            print(f"  {label:<24}{expected:>15.4f}{'NOT FOUND':>12}")
            failures += 1
            continue
        tiers = match["pricingInfo"][0]["pricingExpression"]["tieredRates"]
        # Last tier, because a free first tier is an allowance rather than the rate.
        unit = tiers[-1]["unitPrice"]
        actual = (int(unit.get("units", 0)) + unit.get("nanos", 0) / 1e9) * scale
        ok = abs(actual - expected) < 0.005
        failures += not ok
        flag = "" if ok else "   <-- MISMATCH"
        print(f"  {label:<24}{expected:>15.4f}{actual:>12.4f}  {match['skuId']}{flag}")
        if len(tiers) > 1:
            free_to = tiers[1]["startUsageAmount"]
            print(f"  {'':<24}{'':>15}{'':>12}  free below {free_to:,.0f}")

    print()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
