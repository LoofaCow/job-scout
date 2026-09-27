"""
Tear Apart building — turns scraped Job rows into structured ListingAnalysis.

Importing this package registers the building's SQLModel tables with the
shared metadata, which is what init_db() needs to create them. The api
module imports `app.buildings.tear_apart` (with a noqa) for that side effect.
"""

from app.buildings.tear_apart import models  # noqa: F401  -- table registration
