"""Leaf constants for the runs subsystem — no FastAPI, no DB imports.

Lives apart from ``api/runs/routes.py`` so non-route modules (e.g.
``api/fixtures/schemas.py``, a "pure data shapes" file) can import the
shared cap without picking up a transitive dependency on the route layer.
"""

# Hard cap on items in a collection the run engine will execute. Fixture
# bundles reuse this so an imported collection can't exceed what a run
# would refuse to run. Bumped 50 -> 90 for the eval-game-hoppers
# shippability battery (native/virality/monetization/collision tiers).
COLLECTION_ITEM_CAP = 90
