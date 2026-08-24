# Convenience targets. These also serve as the canonical invocations — every flag
# here exists for a reason documented in RUNBOOK.md.

PY := .venv/bin/python
K6 := k6
export PYTHONPATH := .

.PHONY: help venv test mock-up mock-down obs-up obs-down obs-logs \
        k6-smoke k6-ramp k6-constant sweep-mock pool-experiment clean

help:
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

venv: ## Create the venv and install dependencies
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test: ## Run the test suite (no network, no spend)
	$(PY) -m pytest tests/ -q

mock-up: ## Start the fake Vertex endpoint on :8088
	$(PY) -m uvicorn mock.fake_vertex:app --host 127.0.0.1 --port 8088 --log-level warning &
	@sleep 2 && curl -sf http://127.0.0.1:8088/__stats >/dev/null && echo "fake vertex up on :8088"

mock-down: ## Stop the fake Vertex endpoint
	@pid=$$(ss -lptnH 'sport = :8088' 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1); \
	if [ -n "$$pid" ]; then kill $$pid && echo "stopped $$pid"; else echo "not running"; fi

obs-up: ## Start Prometheus (:9090) and Grafana (:3000)
	cd observability && docker compose up -d
	@echo "grafana http://localhost:3000  prometheus http://localhost:9090"

obs-down: ## Stop the observability stack
	cd observability && docker compose down

obs-logs: ## Tail observability logs
	cd observability && docker compose logs -f --tail=50

# --- load tests against the mock ($0) ---------------------------------------

sweep-mock: ## Closed-loop concurrency sweep against the mock
	GEMINI_BACKEND=vertex GOOGLE_CLOUD_PROJECT=fake GEMINI_BASE_URL=http://127.0.0.1:8088 \
	$(PY) -m harness.run --mode closed --concurrency 4 16 64 --requests 200 \
		--budget-usd 5 --confirm --metrics-port 9464 --label mock-sweep

pool-experiment: ## Show the connection pool is the throughput ceiling
	@for p in 8 16 64 128; do \
		echo "--- pool=$$p ---"; \
		GEMINI_BACKEND=vertex GOOGLE_CLOUD_PROJECT=fake GEMINI_BASE_URL=http://127.0.0.1:8088 \
		$(PY) -m harness.run --mode closed --concurrency 64 --requests 300 \
			--max-connections $$p --budget-usd 5 --confirm --label pool$$p 2>&1 | grep -E '^    [0-9]+/'; \
	done

# --- k6 control harness -----------------------------------------------------
# NOTE: k6 v2 renamed the native-histogram toggle. K6_PROMETHEUS_RW_TREND_AS_NATIVE_HISTOGRAM
# still works but logs a deprecation; K6_FEATURES=native-histograms is the current spelling.

K6_PROM := K6_PROMETHEUS_RW_SERVER_URL=http://localhost:9090/api/v1/write \
           K6_FEATURES=native-histograms

k6-smoke: ## k6 smoke test against the mock
	TARGET=mock SCENARIO=smoke $(K6) run --quiet loadtest/k6/gemini.js

k6-constant: ## k6 constant arrival rate against the mock, into Prometheus
	$(K6_PROM) TARGET=mock SCENARIO=constant RATE=20 DURATION=60s \
		$(K6) run --quiet --out experimental-prometheus-rw loadtest/k6/gemini.js

k6-ramp: ## k6 step ramp to find the knee, into Prometheus
	$(K6_PROM) TARGET=mock SCENARIO=ramp \
		$(K6) run --quiet --out experimental-prometheus-rw loadtest/k6/gemini.js

clean: ## Remove generated results
	rm -rf results/*.jsonl results/*.json
