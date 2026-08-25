# Evidence

PNGs of the Grafana dashboards during the runs described in `FINDINGS.md`.

They are committed because a dashboard that only exists in a running Grafana is not
evidence anyone else can check. The underlying data is in `results/real/`; these are
the visual form of the same runs.

To see them live instead: `make obs-up`, then Grafana at http://localhost:3000, folder
*Takehome*. Dashboards are provisioned from `observability/grafana/dashboards/`, so
they load automatically. Point the time range at a run's window (each manifest in
`results/real/` records `started_at` and `finished_at`) and use Grafana's own share or
screenshot to capture a new image.
