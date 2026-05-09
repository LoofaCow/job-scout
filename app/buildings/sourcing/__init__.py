"""
Sourcing department — discovers and registers places that produce job listings
and direct gigs. Two top-level agents (BoardHunter, WildHunter) feed a shared
Source registry; WildHunter additionally emits Gigs into a parallel pipeline.

Importing this module registers the sourcing tables with SQLModel so that
init_db() picks them up.
"""

from app.buildings.sourcing import models  # noqa: F401  -- side effect: registers tables