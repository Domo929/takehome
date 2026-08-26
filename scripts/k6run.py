#!/usr/bin/env python3
"""Run k6 and mark the run so the dashboard can jump straight to it.

Grafana is built around a rolling time window, which is the wrong shape for discrete
load tests: after a few runs you are left hunting for "which 45-second window was the
ramp?" This wrapper closes that gap by recording each run as a Grafana **region
annotation**, which gives two things at once:

* every panel shows a shaded band for the run, so a spike is immediately attributable
  to a specific configuration rather than to an unlabelled moment in time
* the "Recent runs" panel lists those annotations, and clicking one moves the whole
  dashboard to that window

It also appends a line to ``results/runs.jsonl`` and prints a deep link, so a run can
be revisited from a terminal or a PR comment without touching the UI.

Usage:
    python scripts/k6run.py --scenario ramp --target service
    python scripts/k6run.py --scenario constant --rate 40 --duration 30s --note "pool=16"
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
GRAFANA = os.getenv("GRAFANA_URL", "http://localhost:3000")
DASH_UID = "takehome-overview"
RUNS = REPO / "results" / "runs.jsonl"
# Padding either side of the window so ramp-up and drain stay visible rather than
# being clipped exactly at the first and last request.
PAD_S = 15


def annotate(start_ms: int, end_ms: int, text: str, tags: list[str]) -> int | None:
    body = json.dumps(
        {
            "dashboardUID": DASH_UID,
            "time": start_ms,
            "timeEnd": end_ms,
            "tags": tags,
            "text": text,
        }
    ).encode()
    req = urllib.request.Request(
        f"{GRAFANA}/api/annotations",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("id")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Annotation is a convenience; never fail a load test over it.
        print(f"  (could not annotate: {exc})", file=sys.stderr)
        return None


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--scenario", default="constant")
    p.add_argument("--target", default="service", choices=["service", "vertex", "mock"])
    p.add_argument("--rate", type=float, default=None)
    p.add_argument("--duration", default=None)
    p.add_argument("--max-vus", type=int, default=None)
    p.add_argument("--thinking-budget", type=int, default=None)
    p.add_argument("--note", default="", help="Free-text label shown on the annotation.")
    p.add_argument("--no-prometheus", action="store_true", help="Skip remote-write.")
    p.add_argument("k6_args", nargs="*", help="Extra args passed through to k6.")
    args = p.parse_args()

    env = os.environ.copy()
    env["TARGET"] = args.target
    env["SCENARIO"] = args.scenario
    if args.rate is not None:
        env["RATE"] = str(args.rate)
    if args.duration:
        env["DURATION"] = args.duration
    if args.max_vus:
        env["MAX_VUS"] = str(args.max_vus)
    if args.thinking_budget is not None:
        env["GEMINI_THINKING_BUDGET"] = str(args.thinking_budget)

    summary = REPO / "results" / f"k6-{int(time.time())}.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    env["K6_SUMMARY_OUT"] = str(summary)

    cmd = ["k6", "run", "--quiet"]
    if not args.no_prometheus:
        env.setdefault("K6_PROMETHEUS_RW_SERVER_URL", "http://localhost:9090/api/v1/write")
        env.setdefault("K6_FEATURES", "native-histograms")
        cmd += ["--out", "experimental-prometheus-rw"]
    cmd += [*args.k6_args, str(REPO / "loadtest" / "k6" / "gemini.js")]

    label_bits = [args.scenario, f"->{args.target}"]
    if args.rate is not None:
        label_bits.append(f"{args.rate:g}rps")
    if args.duration:
        label_bits.append(args.duration)
    if args.thinking_budget is not None:
        label_bits.append(f"tb={args.thinking_budget}")
    if args.note:
        label_bits.append(args.note)
    label = " ".join(label_bits)

    print(f"\n=== {label} ===")
    started = time.time()
    proc = subprocess.run(cmd, env=env, cwd=REPO)
    ended = time.time()

    stats: dict = {}
    if summary.exists():
        try:
            stats = json.loads(summary.read_text())
        except json.JSONDecodeError:
            pass

    # Prometheus scrapes on an interval, so a run's last samples can land a beat after
    # k6 exits. Padding keeps them inside the window.
    from_ms = int((started - PAD_S) * 1000)
    to_ms = int((ended + PAD_S) * 1000)

    detail = []
    if stats:
        detail.append(f"{stats.get('requests', 0)} req")
        lat = stats.get("latency_ms") or {}
        if lat.get("p99"):
            detail.append(f"p99 {lat['p99']:.0f}ms")
        if stats.get("dropped_iterations"):
            detail.append(f"DROPPED {stats['dropped_iterations']}")
        if stats.get("cost_usd"):
            detail.append(f"${stats['cost_usd']:.4f}")
    if proc.returncode != 0:
        detail.append(f"exit={proc.returncode}")

    text = label + (f": {', '.join(detail)}" if detail else "")
    tags = ["k6run", args.scenario, args.target]
    if proc.returncode != 0:
        tags.append("failed")
    annotate(from_ms, to_ms, text, tags)

    RUNS.parent.mkdir(parents=True, exist_ok=True)
    with RUNS.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "label": label,
                    "scenario": args.scenario,
                    "target": args.target,
                    "started": started,
                    "ended": ended,
                    "duration_s": round(ended - started, 1),
                    "exit_code": proc.returncode,
                    "from_ms": from_ms,
                    "to_ms": to_ms,
                    "summary_file": str(summary.relative_to(REPO)),
                    "stats": {
                        k: stats.get(k)
                        for k in ("requests", "dropped_iterations", "cost_usd", "latency_ms")
                    },
                }
            )
            + "\n"
        )

    url = f"{GRAFANA}/d/{DASH_UID}/?orgId=1&from={from_ms}&to={to_ms}"
    print(f"\n  dashboard window for this run:\n  {url}\n")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
