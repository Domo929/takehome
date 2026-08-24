#!/usr/bin/env python3
"""Generate the main Grafana overview dashboard.

Written as a generator rather than hand-edited JSON because the layout is a grid of
~35 panels: computing gridPos by hand is error-prone and every future edit would risk
silently overlapping panels. Run this and commit the output.

    python observability/build_dashboards.py
"""

from __future__ import annotations

import json
import pathlib

DS = {"type": "prometheus", "uid": "prometheus"}
OUT = pathlib.Path(__file__).parent / "grafana" / "dashboards" / "overview.json"

_id = 0


def nid() -> int:
    global _id
    _id += 1
    return _id


def target(expr: str, legend: str, ref: str = "A") -> dict:
    return {"expr": expr, "legendFormat": legend, "refId": ref, "datasource": DS}


def targets(*pairs: tuple[str, str]) -> list[dict]:
    return [target(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(pairs)]


def row(title: str, y: int, desc: str = "") -> dict:
    return {
        "id": nid(),
        "type": "row",
        "title": title,
        "description": desc,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "collapsed": False,
        "panels": [],
    }


def stat(title: str, expr: str, x: int, y: int, *, unit="short", decimals=1,
         w=3, h=4, steps=None, desc="", legend="") -> dict:
    return {
        "id": nid(),
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": steps or [{"color": "text", "value": None}],
                },
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNonNull"], "fields": "", "values": False},
            "textMode": "auto",
            "graphMode": "area",
            "colorMode": "value",
        },
        "targets": [target(expr, legend or title)],
    }


def ts(title: str, tgts: list[dict], x: int, y: int, *, unit="short", w=12, h=8,
       stack=False, desc="", fill=10, decimals=None, minv=None, maxv=None,
       legend_table=False) -> dict:
    custom = {
        "fillOpacity": fill,
        "lineWidth": 1,
        "showPoints": "never",
        "spanNulls": True,
    }
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
        custom["fillOpacity"] = 70
    defaults: dict = {"unit": unit, "custom": custom}
    if decimals is not None:
        defaults["decimals"] = decimals
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    return {
        "id": nid(),
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {
                "displayMode": "table" if legend_table else "list",
                "placement": "bottom",
                "calcs": ["mean", "max"] if legend_table else [],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": tgts,
    }


def text(title: str, content: str, x: int, y: int, w=24, h=3) -> dict:
    return {
        "id": nid(),
        "type": "text",
        "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {"mode": "markdown", "content": content},
    }


GREEN_RED = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
RED_GREEN = [{"color": "red", "value": None}, {"color": "green", "value": 1}]

panels: list[dict] = []
y = 0

# ---------------------------------------------------------------- health check
panels.append(row("Health check", y, "Is it up, is it fast, is it failing, what is it costing"))
y += 1
panels += [
    stat("Served RPS", 'sum(rate(service_requests_total{outcome="success"}[1m]))', 0, y,
         unit="reqps", desc="Successful inbound requests per second."),
    stat("Error rate", 'sum(rate(service_requests_total{outcome=~"error|unusable"}[1m])) / '
         'clamp_min(sum(rate(service_requests_total[1m])), 0.001)', 3, y,
         unit="percentunit", decimals=2,
         steps=[{"color": "green", "value": None}, {"color": "yellow", "value": 0.01},
                {"color": "red", "value": 0.05}],
         desc="Errors and unusable answers as a share of all inbound requests."),
    stat("Shed rate (503)", 'sum(rate(service_admission_rejected_total[1m]))', 6, y,
         unit="reqps", desc="Deliberate backpressure, not a fault. Non-zero means at capacity."),
    stat("p50 latency", 'histogram_quantile(0.50, sum by (le) '
         '(rate(service_request_duration_seconds_bucket[1m])))', 9, y, unit="s", decimals=2),
    stat("p99 latency", 'histogram_quantile(0.99, sum by (le) '
         '(rate(service_request_duration_seconds_bucket[1m])))', 12, y, unit="s", decimals=2),
    stat("In flight", "service_inflight_requests", 15, y, decimals=0,
         desc="Inbound requests currently being served."),
    stat("Spent", "sum(llm_spend_usd_total)", 18, y, unit="currencyUSD", decimals=4,
         desc="Actual spend from reported usage metadata."),
    stat("Budget left", "llm_budget_remaining_usd", 21, y, unit="currencyUSD", decimals=4,
         steps=[{"color": "red", "value": None}, {"color": "yellow", "value": 0.5},
                {"color": "green", "value": 2}]),
]
y += 4

# ------------------------------------------------------------- latency layers
panels.append(row("Latency layers — where the time actually goes", y))
y += 1
panels.append(text(
    "",
    "`total = queue_wait + upstream + framework`. **upstream** is Vertex and is not "
    "ours to fix. **queue_wait** is our admission gate — growth here is the earliest "
    "saturation signal. **framework** is serialization, validation and event-loop "
    "scheduling. If the stack is dominated by upstream, our integration is not the "
    "problem.",
    0, y, h=3))
y += 3
panels += [
    ts("Latency composition, p99 (stacked)", targets(
        ('histogram_quantile(0.99, sum by (le) (rate(llm_request_duration_seconds_bucket[1m])))',
         "upstream (vendor)"),
        ('histogram_quantile(0.99, sum by (le) (rate(service_queue_wait_seconds_bucket[1m])))',
         "queue wait (ours)"),
        ('clamp_min(histogram_quantile(0.99, sum by (le) (rate(service_overhead_seconds_bucket[1m]))) '
         '- histogram_quantile(0.99, sum by (le) (rate(service_queue_wait_seconds_bucket[1m]))), 0)',
         "framework (ours)"),
    ), 0, y, unit="s", stack=True, legend_table=True,
        desc="Stacked p99 contributions. Quantiles are not strictly additive, so read "
             "this as proportion rather than exact arithmetic."),
    ts("Our overhead vs vendor time", targets(
        ('histogram_quantile(0.99, sum by (le) (rate(service_overhead_seconds_bucket[1m])))',
         "our overhead p99"),
        ('histogram_quantile(0.50, sum by (le) (rate(service_overhead_seconds_bucket[1m])))',
         "our overhead p50"),
        ('histogram_quantile(0.99, sum by (le) (rate(llm_request_duration_seconds_bucket[1m])))',
         "vendor p99"),
    ), 12, y, unit="s", legend_table=True,
        desc="Our overhead should stay flat as load rises. If it climbs with traffic, "
             "we are the bottleneck."),
]
y += 8
panels += [
    ts("Inbound latency percentiles", targets(
        ('histogram_quantile(0.50, sum by (le) (rate(service_request_duration_seconds_bucket[1m])))', "p50"),
        ('histogram_quantile(0.90, sum by (le) (rate(service_request_duration_seconds_bucket[1m])))', "p90"),
        ('histogram_quantile(0.99, sum by (le) (rate(service_request_duration_seconds_bucket[1m])))', "p99"),
    ), 0, y, unit="s", w=8),
    ts("Upstream latency percentiles", targets(
        ('histogram_quantile(0.50, sum by (le) (rate(llm_request_duration_seconds_bucket[1m])))', "p50"),
        ('histogram_quantile(0.90, sum by (le) (rate(llm_request_duration_seconds_bucket[1m])))', "p90"),
        ('histogram_quantile(0.99, sum by (le) (rate(llm_request_duration_seconds_bucket[1m])))', "p99"),
    ), 8, y, unit="s", w=8),
    ts("Admission queue wait", targets(
        ('histogram_quantile(0.50, sum by (le) (rate(service_queue_wait_seconds_bucket[1m])))', "p50"),
        ('histogram_quantile(0.99, sum by (le) (rate(service_queue_wait_seconds_bucket[1m])))', "p99"),
    ), 16, y, unit="s", w=8,
        desc="Rises before latency or errors do. The earliest saturation signal."),
]
y += 8

# --------------------------------------------------------- throughput/outcomes
panels.append(row("Throughput and outcomes", y))
y += 1
panels += [
    ts("Inbound requests by outcome (stacked)", targets(
        ('sum by (outcome) (rate(service_requests_total[1m]))', "{{outcome}}"),
        ('sum(rate(service_admission_rejected_total[1m]))', "shed (503)"),
    ), 0, y, unit="reqps", stack=True, legend_table=True),
    ts("Upstream calls by finish reason (stacked)", targets(
        ('sum by (finish_reason) (rate(llm_requests_total{finish_reason!=""}[1m]))',
         "{{finish_reason}}"),
    ), 12, y, unit="reqps", stack=True, legend_table=True,
        desc="MAX_TOKENS means truncated and unusable, billed in full. SAFETY is a "
             "content block. Only STOP is a complete answer."),
]
y += 8
panels += [
    ts("Errors by class", targets(
        ('sum by (error_class) (rate(llm_requests_total{error_class!=""}[1m]))', "{{error_class}}"),
    ), 0, y, unit="reqps", w=8, stack=True),
    ts("Retries by reason", targets(
        ('sum by (reason) (rate(llm_retry_attempts_total[1m]))', "{{reason}}"),
    ), 8, y, unit="reqps", w=8, stack=True,
        desc="Retries are hand-rolled rather than delegated to the SDK precisely so "
             "they appear here instead of being invisible."),
    ts("Empty / unusable responses", targets(
        ('sum by (finish_reason) (rate(llm_empty_responses_total[1m]))', "{{finish_reason}}"),
    ), 16, y, unit="reqps", w=8,
        desc="HTTP 200 with no usable text. Invisible to transport-level retry."),
]
y += 8

# ------------------------------------------------------------------------ cost
panels.append(row("Cost", y))
y += 1
panels += [
    stat("Burn rate", "sum(rate(llm_spend_usd_total[1m])) * 3600", 0, y,
         unit="currencyUSD", decimals=2, w=4, legend="$/hour",
         desc="Dollars per hour at the current rate."),
    stat("Cost per usable answer",
         'sum(rate(llm_spend_usd_total[1m])) / clamp_min(sum(rate(llm_requests_total{outcome="success"}[1m])), 0.001)',
         4, y, unit="currencyUSD", decimals=6, w=4,
         desc="Spend divided by answers we can actually use. The unit-economics number."),
    stat("Time to budget exhaustion",
         "max(llm_budget_remaining_usd) / clamp_min(sum(rate(llm_spend_usd_total[1m])), 0.0000001)",
         8, y, unit="s", decimals=0, w=4,
         steps=[{"color": "red", "value": None}, {"color": "yellow", "value": 300},
                {"color": "green", "value": 1800}],
         desc="Remaining budget at the current burn rate."),
    ts("Cumulative spend", targets(
        ('sum by (model) (llm_spend_usd_total)', "{{model}}"),
    ), 12, y, unit="currencyUSD", w=12, h=8, decimals=4),
]
y += 4
panels.append(
    ts("Spend rate by model", targets(
        ('sum by (model) (rate(llm_spend_usd_total[1m]))', "{{model}} $/s"),
    ), 0, y, unit="currencyUSD", w=12, h=4, decimals=6))
y += 4

# ---------------------------------------------------------------------- tokens
panels.append(row("Token usage", y))
y += 1
panels += [
    ts("Token rate by kind (stacked)", targets(
        ('sum(rate(llm_tokens_total{kind="input"}[1m]))', "input"),
        ('sum(rate(llm_tokens_total{kind="output"}[1m])) - sum(rate(llm_tokens_total{kind="thinking"}[1m]))',
         "visible output"),
        ('sum(rate(llm_tokens_total{kind="thinking"}[1m]))', "thinking (billed, invisible)"),
    ), 0, y, unit="short", stack=True, legend_table=True,
        desc="Thinking tokens bill at the output rate but produce no user-visible text."),
    ts("Thinking share of billed output", targets(
        ('sum(rate(llm_tokens_total{kind="thinking"}[1m])) / '
         'clamp_min(sum(rate(llm_tokens_total{kind="output"}[1m])), 0.001)', "thinking share"),
    ), 12, y, unit="percentunit", decimals=1, minv=0, maxv=1,
        desc="Measured at 83.6% with dynamic thinking on a short-answer workload. "
             "High values mean paying output rates for reasoning nobody reads."),
]
y += 8
panels.append(
    ts("Cumulative tokens", targets(
        ('sum by (kind) (llm_tokens_total)', "{{kind}}"),
    ), 0, y, unit="short", w=24, h=6, legend_table=True))
y += 6

# ------------------------------------------------------- saturation / capacity
panels.append(row("Saturation and capacity — is the ceiling us or them?", y))
y += 1
panels += [
    ts("Connection pool saturation", targets(
        ('llm_pool_saturation_ratio', "{{provider}} saturation"),
    ), 0, y, unit="percentunit", w=8, minv=0, maxv=1.2,
        desc="In-flight divided by pool size. Sustained near 1.0 means the HTTP pool "
             "is the ceiling, not the vendor. Most commonly missed bottleneck in "
             "async LLM clients."),
    ts("In flight vs pool size", targets(
        ('llm_inflight_requests', "upstream in flight"),
        ('llm_pool_size', "pool size"),
        ('service_inflight_requests', "inbound in flight"),
    ), 8, y, w=8, decimals=0),
    ts("Event loop lag", targets(
        ('llm_event_loop_lag_seconds', "loop lag"),
    ), 16, y, unit="s", w=8,
        desc="Rising lag means the Python process is the constraint. When this climbs, "
             "vendor-side latency numbers from this process are not trustworthy."),
]
y += 8
panels.append(
    ts("Retry budget remaining", targets(
        ('llm_retry_budget_tokens', "{{provider}} tokens"),
    ), 0, y, w=24, h=5,
        desc="Token bucket capping retries as a share of traffic. Draining to zero "
             "means retries are being shed rather than amplifying load against an "
             "already-struggling backend."))
y += 5

# -------------------------------------------------------------- load generator
panels.append(row("Load generator (k6) — is the rig itself the bottleneck?", y))
y += 1
panels.append(text(
    "",
    "`k6_dropped_iterations_total` **must stay at zero**. Anything above zero means k6 "
    "could not sustain the offered rate, so the run measured the generator rather than "
    "the service and every other number here is suspect. It is a k6 threshold, so such "
    "a run also exits non-zero.",
    0, y, h=3))
y += 3
panels += [
    ts("Offered vs achieved rate", targets(
        ('sum(rate(k6_http_reqs_total[30s]))', "k6 achieved"),
        ('sum(rate(service_requests_total[30s]))', "service received"),
    ), 0, y, unit="reqps", w=8),
    ts("Dropped iterations (must be zero)", targets(
        ('sum(rate(k6_dropped_iterations_total[30s])) or vector(0)', "dropped/s"),
    ), 8, y, unit="reqps", w=8,
        desc="Non-zero invalidates the run."),
    ts("Active VUs", targets(('sum(k6_vus)', "VUs"),), 16, y, w=8, decimals=0),
]
y += 8
panels += [
    ts("k6 observed latency", targets(
        ('histogram_quantile(0.50, sum(rate(k6_http_req_duration_seconds[1m])))', "p50"),
        ('histogram_quantile(0.99, sum(rate(k6_http_req_duration_seconds[1m])))', "p99"),
    ), 0, y, unit="s", w=12,
        desc="Client-observed, includes network transit the service cannot see."),
    ts("k6 request failure rate", targets(
        ('sum(k6_http_req_failed_rate) or vector(0)', "k6 failed rate"),
        ('sum(rate(k6_gemini_rate_limited_total[1m])) or vector(0)', "429/s"),
        ('sum(rate(k6_service_rejected_503_total[1m])) or vector(0)', "503 shed/s"),
    ), 12, y, unit="reqps", w=12),
]

dashboard = {
    "uid": "takehome-overview",
    "title": "Gemini Integration — Overview",
    "description": "Every metric the integration emits, organized by question.",
    "tags": ["takehome", "overview"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "5s",
    "time": {"from": "now-30m", "to": "now"},
    "panels": panels,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(dashboard, indent=2))
print(f"wrote {OUT} — {len(panels)} panels, {_id} ids")
