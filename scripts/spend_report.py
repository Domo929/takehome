#!/usr/bin/env python3
"""Spend ledger.

Running experiments on someone else's cloud project creates an obligation to be able
to answer "what has this cost you?" precisely, at any moment, without reconstructing
it from memory. This reads every run manifest on disk and reports actual spend from
reported ``usage_metadata``, never estimates, broken down by the account it was
billed to.

Ad-hoc probes made outside the harness (single curl calls, access checks) do not
produce manifests, so they are declared explicitly below rather than quietly omitted.
The alternative is a total that is wrong in the flattering direction.

    python scripts/spend_report.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
from collections import defaultdict

from llm.pricing import GROUNDING_FREE_PROMPTS_PER_MONTH, GROUNDING_USD_PER_1K_PROMPTS

REPO = pathlib.Path(__file__).resolve().parent.parent

# Requests issued outside the harness, so not captured in any manifest. Counted at a
# conservative per-request rate rather than dropped.
UNTRACKED = [
    ("vertex", "access check (check_vertex.sh) x2", 2, 0.0000200),
    ("vertex", "preflight single request", 1, 0.0004329),
    ("developer", "preflight + spelling probes", 14, 0.0003000),
    ("developer", "rate-limit probe (230 tiny requests)", 230, 0.0000160),
    ("vertex", "logprobs support probe", 2, 0.0000700),
    ("vertex", "logprobs token-cost A/B (10 on, 10 off)", 20, 0.0000128),
    ("vertex", "logprobs 100-sample experiment", 100, 0.0000146),
]


def _k6_rows() -> list[dict]:
    """k6 writes its own summary shape and does not use the -manifest suffix.

    Scanned separately rather than shoehorned into the manifest parser. k6 runs bill
    the same project as everything else, so leaving them out understates real spend,
    which is the failure this whole script exists to prevent. It cost the ledger a
    $3.85 run before this existed.
    """
    rows = []
    for f in sorted((REPO / "results").rglob("k6-*.json")):
        try:
            m = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if "target" not in m or "cost_usd" not in m:
            continue
        # Two shapes reach here. A k6 run pointed straight at the vendor knows its own
        # target. A run through our service does not, because the backend is behind
        # the service, so service.js asks /health at setup and records `billable`.
        # Without that a real $0.76 through the service was invisible to this ledger,
        # which is the same class of error as counting a mock run as spend.
        if m.get("target") == "service":
            if m.get("billable") is not True:
                continue
        elif m.get("target") != "vertex":
            continue  # mock-backed rehearsal, modelled dollars, nothing spent
        rows.append(
            {
                "label": f.stem,
                "backend": "vertex",
                "location": m.get("service_url") and "via service" or "-",
                "model": m.get("model", "-"),
                "requests": m.get("requests", 0),
                "cost": m.get("cost_usd", 0.0),
                "tokens_in": 0,
                "tokens_out": 0,
            }
        )
    return rows


def load_manifests() -> list[dict]:
    rows = _k6_rows()
    for f in sorted((REPO / "results").rglob("*-manifest.json")):
        try:
            m = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        # Two manifest shapes exist: harness runs (provider + stages) and one-off
        # experiments (flat). A ledger that silently skipped the second shape would
        # under-report spend, which is the exact failure this script exists to prevent.
        p = m.get("provider") or {
            "backend": "vertex" if m.get("project") else "?",
            "location": m.get("location", "-"),
            "model": m.get("model", "-"),
        }
        # A run against the local mock still says backend="vertex", because only the
        # URL was swapped. Counting it charges fake money to a real project, which is
        # the one thing this script must never do. Older manifests predate the flag,
        # so fall back to sniffing the URL.
        base_url = p.get("base_url") or m.get("base_url")
        billable = p.get("billable")
        if billable is None:
            billable = not (base_url and ("127.0.0.1" in base_url or "localhost" in base_url))
        if not billable:
            continue
        stages = m.get("stages", [])
        requests = sum(s.get("requests", 0) for s in stages) or m.get("requests", 0)
        rows.append(
            {
                "label": m.get("label") or m.get("experiment") or f.stem,
                "backend": p.get("backend", "?"),
                "location": p.get("location", "-"),
                "model": p.get("model", "-"),
                "requests": requests,
                "cost": m.get("actual_usd", m.get("modelled_cost_usd", 0.0)),
                "tokens_in": sum(s.get("tokens", {}).get("input", 0) for s in stages),
                "tokens_out": sum(s.get("tokens", {}).get("output", 0) for s in stages),
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = ap.parse_args()

    rows = load_manifests()

    by_account: dict[str, dict] = defaultdict(
        lambda: {"cost": 0.0, "requests": 0, "runs": 0}
    )
    for r in rows:
        acct = "Evertune (vertex)" if r["backend"] == "vertex" else "Personal (developer API)"
        by_account[acct]["cost"] += r["cost"]
        by_account[acct]["requests"] += r["requests"]
        by_account[acct]["runs"] += 1

    untracked_total = 0.0
    for backend, _desc, n, per in UNTRACKED:
        acct = "Evertune (vertex)" if backend == "vertex" else "Personal (developer API)"
        by_account[acct]["cost"] += n * per
        by_account[acct]["requests"] += n
        untracked_total += n * per

    if args.json:
        print(json.dumps({"by_account": by_account, "runs": rows}, indent=2))
        return

    print("\nTracked runs (from manifests, actual usage_metadata)\n")
    print(f"  {'run':<18} {'backend':<10} {'location':<14} {'req':>6} {'in tok':>9} {'out tok':>9} {'cost':>11}")
    for r in rows:
        print(
            f"  {r["label"]:<26} {r['backend']:<10} {r['location']:<14} {r['requests']:>6} "
            f"{r['tokens_in']:>9,} {r['tokens_out']:>9,} {r['cost']:>11.6f}"
        )

    print("\nUntracked probes (declared, not from manifests)\n")
    print(f"  {'what':<40} {'backend':<10} {'req':>6} {'est cost':>11}")
    for backend, desc, n, per in UNTRACKED:
        print(f"  {desc:<40} {backend:<10} {n:>6} {n * per:>11.6f}")

    print("\nTotal by account\n")
    print(f"  {'account':<28} {'runs':>6} {'requests':>10} {'total cost':>12}")
    for acct in sorted(by_account):
        d = by_account[acct]
        print(f"  {acct:<28} {d['runs']:>6} {d['requests']:>10,} {d['cost']:>12.4f}")

    # Early grounded runs were priced at $25/1k when their manifests were written; the
    # SKU has since been verified at $35/1k (FINDINGS 2). Manifests are not rewritten,
    # they record what was believed at the time, so the correction is reported here.
    # Each manifest carries the rate it used, because assuming a single rate across all
    # runs is what made the original number wrong.
    grounded = 0
    shortfall = 0.0
    # k6 summaries do not use the -manifest suffix, and a grounded run through the
    # service issues real SKU prompts just like a direct one.
    for f in sorted((REPO / "results").rglob("*-manifest.json")) + sorted(
        (REPO / "results").rglob("k6-*.json")
    ):
        try:
            m = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        n = m.get("grounded_prompts_billed", 0)
        if not n:
            continue
        grounded += n
        used = m.get("grounding_rate_assumed_usd_per_1k", 25.0)
        shortfall += n * (GROUNDING_USD_PER_1K_PROMPTS - used) / 1000.0
    if grounded:
        true_cost = grounded * GROUNDING_USD_PER_1K_PROMPTS / 1000.0
        print(f"\n  Grounding: {grounded} grounded prompts, "
              f"${true_cost:.2f} at the verified ${GROUNDING_USD_PER_1K_PROMPTS:.0f}/1k")
        if shortfall > 0.005:
            print(f"    runs written before the rate was verified understate by "
                  f"${shortfall:.2f}; later runs already use the correct rate")
        print(f"    all of it may be free: the SKU's first "
              f"{GROUNDING_FREE_PROMPTS_PER_MONTH:,} prompts per month cost nothing "
              f"and only {grounded} were issued")

    evertune = by_account.get("Evertune (vertex)", {"cost": 0.0, "requests": 0})
    # Fold the correction in rather than printing it as a footnote above a total that
    # ignores it. The headline used to be the sum of stale manifest values, which
    # still embed the old $25/1k rate, so it disagreed with the line directly above
    # it and did so in the flattering direction. This file exists to prevent exactly
    # that.
    total = evertune["cost"] + shortfall
    print(
        f"\n  Spent on Evertune's project: ${total:.4f} "
        f"across {evertune['requests']:,} requests"
    )
    if shortfall > 0.005:
        print(f"    (includes ${shortfall:.2f} of grounding under-charged by older "
              f"manifests, corrected to the verified rate)")
    print("  (untracked probes are estimated; everything else is reported usage.)\n")


if __name__ == "__main__":
    main()
