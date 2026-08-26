"""Re-derive every headline number in FINDINGS.md from committed data.

Written after a run of avoidable mistakes, each of the same shape: a number was
correct when it was written, something upstream changed, and the prose kept the old
value. Nothing errored. Re-reading did not catch any of them, because a wrong number
reads exactly like a right one.

So this does not read the prose. It recomputes each figure from the raw records and
compares against the value FINDINGS claims. A mismatch exits non-zero.

    python scripts/verify_findings.py

Costs nothing and touches no vendor. Run it before shipping any edit to FINDINGS.

What it deliberately does NOT check: prose claims that aren't numbers, and anything
whose source is an external doc. `scripts/verify_pricing.py` covers the vendor rates
against Google's billing catalog.
"""

from __future__ import annotations

import json
import pathlib
import re
import statistics
import sys
from collections import Counter, defaultdict

REPO = pathlib.Path(__file__).resolve().parent.parent
FINDINGS = REPO / "FINDINGS.md"

# Grounded requests recorded before the SKU rate was verified carry $25/1k inside
# their cost_usd. Backing it out and re-adding the verified rate is the only way to
# compare those runs with newer ones. Forgetting this double-counts the SKU, which is
# a mistake this file exists to make impossible to repeat.
LEGACY_GROUNDING_PER_PROMPT = 0.025

failures: list[str] = []
checks = 0


def check(label: str, got, want, tol: float = 0.0) -> None:
    """Compare a recomputed value against what FINDINGS claims."""
    global checks
    checks += 1
    if isinstance(got, (int, float)) and isinstance(want, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        print(f"  ok    {label:<52} {got}")
    else:
        print(f"  FAIL  {label:<52} got {got!r}, FINDINGS says {want!r}")
        failures.append(label)


def findings_contains(label: str, needle: str) -> None:
    """Assert a literal string is present, for figures with no other home."""
    global checks
    checks += 1
    if needle in FINDINGS.read_text():
        print(f"  ok    {label:<52} found")
    else:
        print(f"  FAIL  {label:<52} missing: {needle!r}")
        failures.append(label)


def jsonl(pattern: str) -> list[dict]:
    rows = []
    for f in sorted(REPO.glob(pattern)):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return rows


def manifest(pattern: str) -> dict:
    matches = sorted(REPO.glob(pattern))
    if not matches:
        raise FileNotFoundError(pattern)
    return json.loads(matches[-1].read_text())


def main() -> int:
    print(__doc__.split("\n")[0])
    print()

    print("Thinking (section 1)")
    off = jsonl("results/real/model/think-off-n100-c8.jsonl")
    dyn = jsonl("results/real/model/think-dyn-n100-c8.jsonl")
    check("thinking off, mean output tokens", round(statistics.mean(r["output_tokens"] for r in off), 1), 145.3)
    check("thinking dyn, mean output tokens", round(statistics.mean(r["output_tokens"] for r in dyn), 1), 533.6)
    check("thinking dyn, mean thinking tokens", round(statistics.mean(r["thinking_tokens"] for r in dyn), 1), 411.2)
    c_off = statistics.mean(r["cost_usd"] for r in off)
    c_dyn = statistics.mean(r["cost_usd"] for r in dyn)
    check("thinking off, mean cost", round(c_off, 6), 0.000374)
    check("thinking dyn, mean cost", round(c_dyn, 6), 0.001344)
    check("thinking cost ratio", round(c_dyn / c_off, 2), 3.60, tol=0.01)
    share = statistics.mean(r["thinking_tokens"] for r in dyn) / statistics.mean(
        r["output_tokens"] for r in dyn
    )
    check("thinking share of billed output", round(share * 100, 1), 77.1, tol=0.1)
    # p50 must come from the manifest, because FINDINGS once quoted a different
    # estimator here and disagreed with its own committed artifact by 46 ms.
    check("thinking off p50 (manifest)", round(manifest("results/real/model/think-off-n100-manifest.json")["stages"][0]["latency_ms"]["p50"]), 1527)
    check("thinking dyn p50 (manifest)", round(manifest("results/real/model/think-dyn-n100-manifest.json")["stages"][0]["latency_ms"]["p50"]), 3778)

    print("\nGrounding, paired n=20 (section 2)")
    gm = manifest("results/real/measurement/grounding-*-manifest.json")
    cond = {c["condition"]: c for c in gm["conditions"]}
    check("ungrounded output tokens", round(cond["ungrounded"]["mean_output_tokens"], 1), 160.7)
    check("grounded output tokens", round(cond["grounded"]["mean_output_tokens"], 1), 299.4)
    check("input tokens identical", cond["ungrounded"]["mean_input_tokens"], cond["grounded"]["mean_input_tokens"])
    check("ungrounded p95 latency", round(cond["ungrounded"]["p95_latency_ms"]), 3256)
    check("grounded p95 latency", round(cond["grounded"]["p95_latency_ms"]), 10076)
    check("grounded truncation at 512", cond["grounded"]["truncated"], 10)

    print("\nCost model (section 5)")
    from llm.pricing import GROUNDING_USD_PER_1K_PROMPTS, PRICING

    p = PRICING["gemini-2.5-flash"]
    ung = (34.5 * p.input_per_1m + 145.3 * p.output_per_1m) / 1e6
    sku = GROUNDING_USD_PER_1K_PROMPTS / 1000.0
    check("ungrounded unit cost", round(ung, 8), 0.00037360)
    check("grounded / ungrounded ratio", round((ung + sku) / ung), 95)
    # Shares come from the paired run, the only one that measured both arms on the
    # same prompts.
    u_tok = (35.2 * p.input_per_1m + 160.7 * p.output_per_1m) / 1e6
    g_tok = (35.2 * p.input_per_1m + 299.4 * p.output_per_1m) / 1e6
    bill = u_tok + g_tok + sku
    check("grounding SKU share of bill", round(sku / bill * 100, 2), 96.76, tol=0.01)
    check("all token levers, max share", round((u_tok + g_tok) / bill * 100, 1), 3.2, tol=0.05)
    arm = 100 * 100
    check("one report, one refresh", round(arm * (ung + sku) + arm * ung, 0), 357.0, tol=1.0)
    for name, n, want in (("Daily", 365, 25_822_728), ("Monthly", 12, 848_966)):
        check(f"{name.lower()} grounded/yr", round(200 * n * arm * (ung + sku)), want, tol=2)

    print("\nProduction unit, n=100 per arm (section 2)")
    pu = jsonl("results/real/measurement/production-unit-*.jsonl")
    g = [r for r in pu if r["arm"] == "grounded"]
    u = [r for r in pu if r["arm"] == "ungrounded"]
    check("grounded arm size", len(g), 100)
    tokens = sum(r["cost_usd"] for r in u) + sum(
        r["cost_usd"] - LEGACY_GROUNDING_PER_PROMPT for r in g
    )
    check("production unit total, verified rate", round(tokens + len(g) * sku, 2), 3.67, tol=0.01)

    sys.path.insert(0, str(REPO))
    from scripts.production_unit import brands

    rate = {}
    for arm, rows in (("g", g), ("u", u)):
        c: Counter = Counter()
        for r in rows:
            for b in brands(r["answer"]):
                c[b] += 1
        rate[arm] = {b: n / len(rows) * 100 for b, n in c.items()}
    for brand, want in (("dreame", 92), ("ecovacs", 41), ("irobot", -15), ("anker", -15)):
        got = rate["g"].get(brand, 0) - rate["u"].get(brand, 0)
        check(f"{brand} delta", round(got), want, tol=1)

    # Same numbers with product lines resolved to their parent company. Two of the
    # rows above change and one changes sign, which is the point of that section.
    resolve = {"roomba": "irobot", "deebot": "ecovacs", "yeedi": "ecovacs", "eufy": "anker"}
    res = {}
    for arm, rows_ in (("g", g), ("u", u)):
        c2: Counter = Counter()
        for r in rows_:
            for b in {resolve.get(x, x) for x in brands(r["answer"])}:
                c2[b] += 1
        res[arm] = {b: n / len(rows_) * 100 for b, n in c2.items()}
    for brand, want in (("irobot", -2), ("anker", 34), ("ecovacs", 36)):
        got = res["g"].get(brand, 0) - res["u"].get(brand, 0)
        check(f"{brand} delta, resolved", round(got), want, tol=1)

    print("\nTemperature sweep, 3,300 requests (section 2)")
    tm = manifest("results/real/measurement/temperature-multi-*-manifest.json")
    check("sweep request count", tm["requests"], 3300)
    check("sweep cost", round(tm["modelled_cost_usd"], 2), 1.08)
    rows = jsonl("results/real/measurement/temperature-multi-*.jsonl")
    band_by_temp = {}
    for t in sorted({r["temperature"] for r in rows}):
        sub = [r for r in rows if abs(r["temperature"] - t) < 1e-9 and r.get("brands") is not None]
        seen: dict = defaultdict(Counter)
        tot: Counter = Counter()
        for r in sub:
            tot[r["category"]] += 1
            for b in set(r["brands"]):
                seen[r["category"]][b] += 1
        pairs = sum(len(v) for v in seen.values())
        band = sum(
            1 for cat, bs in seen.items() for b, n in bs.items() if 0.10 <= n / tot[cat] <= 0.90
        )
        band_by_temp[t] = (pairs, band)
    check("temp 0.0: pairs", band_by_temp[0.0][0], 103)
    check("temp 0.0: in 10-90 band", band_by_temp[0.0][1], 0)
    check("temp 0.7: band", band_by_temp[0.7][1], 74)
    check("temp 1.0: band", band_by_temp[1.0][1], 81)
    check("temp 1.4: band (plateau)", band_by_temp[1.4][1], 81)

    print("\nFlash-Lite comparison (section 1)")
    lite = [r for r in jsonl("results/real/model/flash-lite-*.jsonl") if abs(r["temperature"] - 1.0) < 1e-9]
    flash = [r for r in rows if abs(r["temperature"] - 1.0) < 1e-9]
    check("flash mean cost", round(statistics.mean(r["cost_usd"] for r in flash), 6), 0.000309)
    check("flash-lite mean cost", round(statistics.mean(r["cost_usd"] for r in lite), 6), 0.000027)
    check("cost ratio", round(statistics.mean(r["cost_usd"] for r in flash) / statistics.mean(r["cost_usd"] for r in lite), 1), 11.5, tol=0.1)

    print("\nCapacity (sections 3 and 4)")
    soak = manifest("results/real/capacity/vertex-soak-long-manifest.json")["stages"][0]
    check("soak requests", soak["requests"], 47677)
    check("soak throughput", round(soak["throughput_rps"], 1), 36.9)
    check("soak retry rate %", round(soak["retries"] / soak["requests"] * 100, 3), 0.050, tol=0.001)
    check("soak truncation %", round(soak["finish_reasons"]["MAX_TOKENS"] / soak["requests"] * 100, 2), 3.26, tol=0.01)
    knee = {s["concurrency"]: s for s in manifest("results/real/capacity/vertex-knee-manifest.json")["stages"]}
    check("peak rps at c=128", round(knee[128]["throughput_rps"], 1), 73.7)
    ext = {s["concurrency"]: s for s in manifest("results/real/capacity/vertex-extreme-manifest.json")["stages"]}
    check("rps at c=256 (collapse)", round(ext[256]["throughput_rps"], 1), 63.0)
    check("rps at c=1024", round(ext[1024]["throughput_rps"], 1), 43.7)

    k6 = json.loads((REPO / "results/real/capacity/k6-vertex-ceiling.json").read_text())
    check("k6 ceiling target", k6["target"], "vertex")
    check("k6 rate limits", k6["rate_limited"], 0)
    check("k6 dropped iterations", k6.get("dropped_iterations", 0), 0)
    check("k6 requests", k6["requests"], 27443)
    k6_tok = k6["metrics"]["gemini_input_tokens"]["values"]["avg"] + k6["metrics"]["gemini_output_tokens"]["values"]["avg"]
    check("k6 tokens/request", round(k6_tok, 1), 87.6, tol=0.1)
    check("k6 TPM at 550 rps (millions)", round(550 * k6_tok * 60 / 1e6, 2), 2.89, tol=0.01)
    # Token baselines are Google's published Standard PayGo tiers.
    check("Tier 1 sustained rps", round(2e6 / (34.5 + 145.3) / 60), 185)
    check("Tier 3 sustained rps", round(10e6 / (34.5 + 145.3) / 60), 927)

    mp = json.loads((REPO / "results/real/local/multiprocess-experiment.json").read_text())
    check("1 process at c=512", round(mp["control_one_process_c512"]["rps"], 1), 67.0)
    check("4 processes combined", round(mp["combined_rps"], 1), 307.1)
    notls = {s["concurrency"]: s for s in json.loads((REPO / "results/real/local/notls-sweep-manifest.json").read_text())["stages"]}
    check("no-TLS rps at c=256", round(notls[256]["throughput_rps"], 1), 147.0)
    check("no-TLS is billable", json.loads((REPO / "results/real/local/notls-sweep-manifest.json").read_text())["provider"]["billable"], False)

    print("\nTool refusal, 50 paired (section 2)")
    tr = manifest("results/real/measurement/tool-refusal-*-manifest.json")
    tc = {c["condition"]: c for c in tr["conditions"]}
    check("refusals with tool", tc["tool_attached"]["refusals"], 0)
    check("refusals without tool", tc["no_tool"]["refusals"], 0)
    check("discordant pairs", len(tr["discordant_pairs"]), 0)
    check("truncation with tool", tc["tool_attached"]["truncated"], 22)
    check("truncation without", tc["no_tool"]["truncated"], 2)
    # Two multipliers, because chars and tokens disagree and only one is billed.
    verb_chars = tc["tool_attached"]["mean_answer_chars"] / tc["no_tool"]["mean_answer_chars"]
    check("verbosity multiplier, chars", round(verb_chars, 2), 2.42, tol=0.01)
    tr_rec = json.loads(sorted(REPO.glob("results/real/measurement/tool-refusal-*-records.json"))[-1].read_text())
    tok = {
        k: statistics.mean(r["output_tokens"] for r in v if "error" not in r)
        for k, v in tr_rec.items()
    }
    check("verbosity multiplier, tokens", round(tok["tool_attached"] / tok["no_tool"], 2), 2.25, tol=0.01)

    print("\nDocument hygiene")
    text = FINDINGS.read_text()
    non_ascii = sorted({ch for ch in text if ord(ch) > 127})
    check("FINDINGS is plain ASCII", non_ascii, [])
    stale = re.findall(r"results/real/(?!model/|measurement/|capacity/|local/|README)[a-z0-9*-]+", text)
    check("no stale flat-layout paths", sorted(set(stale)), [])
    for path in sorted(set(re.findall(r"results/real/[a-z]+/[A-Za-z0-9_.*-]+", text))):
        if not list(REPO.glob(path + "*")):
            failures.append(f"cited file missing: {path}")
    check("every cited evidence file exists", [f for f in failures if "cited file" in f], [])

    # The table of contents is the first thing a reader touches, so a dead anchor
    # there is worse than a wrong number buried on page 30.
    def slug(h: str) -> str:
        return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", h.lower()).strip())

    heads = {slug(m.group(2)) for m in re.finditer(r"^(#{1,4}) (.+)$", text, re.M)}
    broken = sorted(set(re.findall(r"\]\(#([a-z0-9-]+)\)", text)) - heads)
    check("every anchor link resolves", broken, [])

    print()
    print(f"{checks} checks, {len(failures)} failed")
    if failures:
        print("\nFailed:")
        for f in failures:
            print(f"  - {f}")
        print("\nEither the data changed or FINDINGS drifted. Fix whichever is wrong.")
        return 1
    print("Every headline number in FINDINGS reproduces from committed data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
