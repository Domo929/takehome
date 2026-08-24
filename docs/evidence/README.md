# Evidence

Rendered from live Prometheus data, not mocked. Regenerate with:

```bash
make obs-up
bash scripts/capture_dashboards.sh          # defaults to the last recorded run
bash scripts/capture_dashboards.sh FROM TO  # or an explicit epoch-ms window
```

Rendering runs through a `grafana-image-renderer` sidecar in
`observability/docker-compose.yml`, so the images are reproducible rather than
hand-captured.

| File | What it shows |
|---|---|
| `soak-evidence.png` | The final 20.8-minute validation run against Vertex us-central1: 47,677 requests at 36.9 rps. The p99 spike near 21:42 lines up with the retry spike below it — that correlation is the evidence the tail is a vendor event, not a queue in our process. Only `llm_*` panels, because the run drove the provider directly. |
| `overview.png` | The full 45-panel dashboard. Service and k6 panels are empty for this window by design — see below. |
| `cost.png` | Spend, burn rate and cost per usable answer. |

## Why `overview.png` has empty panels

The dashboard covers three sources: `llm_*` from the provider, `service_*` from the
HTTP service, and `k6_*` from the external generator. The sustained run drove the
provider directly, so only `llm_*` is populated for that window.

`soak-evidence.png` exists precisely to avoid the alternative, which would be
presenting a screenshot full of "No data" tiles and letting a reader assume the run
covered more than it did.
