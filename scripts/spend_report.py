#!/usr/bin/env python3
"""Spend ledger.

Running experiments on someone else's cloud project creates an obligation to be able
to answer "what has this cost you?" precisely, at any moment, without reconstructing
it from memory. This reads every run manifest on disk and reports actual spend from
reported ``usage_metadata`` — never estimates — broken down by the account it was
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

REPO = pathlib.Path(__file__).resolve().parent.parent

# Requests issued outside the harness, so not captured in any manifest. Counted at a
# conservative per-request rate rather than dropped.
UNTRACKED = [
    ("vertex", "access check (check_vertex.sh) x2", 2, 0.0000200),
    ("vertex", "preflight single request", 1, 0.0004329),
    ("developer", "preflight + spelling probes", 14, 0.0003000),
    ("developer", "rate-limit probe (230 tiny requests)", 230, 0.0000160),
]


def load_manifests() -> list[dict]:
    rows = []
    for f in sorted((REPO / "results").rglob("*-manifest.json")):
        try:
            m = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        p = m.get("provider", {})
        stages = m.get("stages", [])
        requests = sum(s.get("requests", 0) for s in stages)
        rows.append(
            {
                "label": m.get("label", f.stem),
                "backend": p.get("backend", "?"),
                "location": p.get("location", "-"),
                "model": p.get("model", "-"),
                "requests": requests,
                "cost": m.get("actual_usd", 0.0),
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
            f"  {r['label']:<18} {r['backend']:<10} {r['location']:<14} {r['requests']:>6} "
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

    evertune = by_account.get("Evertune (vertex)", {"cost": 0.0, "requests": 0})
    print(
        f"\n  Spent on Evertune's project: ${evertune['cost']:.4f} "
        f"across {evertune['requests']:,} requests"
    )
    print("  (untracked probes are estimated; everything else is reported usage.)\n")


if __name__ == "__main__":
    main()
