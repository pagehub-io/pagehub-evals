"""Leaf constants for the runs subsystem — no FastAPI, no DB imports.

Lives apart from ``api/runs/routes.py`` so non-route modules (e.g.
``api/fixtures/schemas.py``, a "pure data shapes" file) can import the
shared cap without picking up a transitive dependency on the route layer.
"""

# Hard cap on items in a collection the run engine will execute. Fixture
# bundles reuse this so an imported collection can't exceed what a run
# would refuse to run.
COLLECTION_ITEM_CAP = 200  # raised from 90 for serve-role-screenshots (170 items); aligns with _MAX_REQUESTS

# Wall-clock budget for one run's HTTP loop, in seconds. Checked before each
# item and before each retry attempt; once exceeded, remaining items are
# recorded as skipped and the run finishes ``error``. With the per-attempt
# ceiling in the engine this bounds a run at budget + one backoff + one
# attempt ceiling.
RUN_BUDGET_SECONDS = 3600
