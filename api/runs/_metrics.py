"""Prometheus metrics for the runs engine.

PLAN.md:156-160 names three metrics that surface terminal-state health
to platform/eyes. Defined at module level so they're singletons across
``execute_run`` invocations and shared with the ``/metrics`` scraper
that ``prometheus_client.generate_latest()`` enumerates.

No ``harness_id`` label — cardinality explodes per the plan.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# Counter, labelled by terminal verdict. Verdict is one of:
# ``passed`` / ``failed`` / ``error``.
runs_total = Counter(
    "pagehub_evals_runs_total",
    "Total number of runs that reached terminal state, labelled by verdict.",
    labelnames=("verdict",),
)

# Wall-clock duration of a run from started_at to finished_at, in
# seconds. Bucketed to cover sub-second through the 500s implicit cap
# (per the per-collection 50-item * 10s timeout bound).
run_duration_seconds = Histogram(
    "pagehub_evals_run_duration_seconds",
    "Per-run wall-clock duration in seconds, bucketed across the plan's expected envelope.",
    labelnames=("verdict",),
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

# Per-run request count. No labels — distribution-only.
run_request_count = Histogram(
    "pagehub_evals_run_request_count",
    "Per-run count of executed requests.",
    buckets=(0, 1, 2, 5, 10, 20, 50),
)
