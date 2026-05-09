"""
Strategy registry — maps strategy names to instances.

Every discovery strategy registers itself here so hunters can look up
strategies by name. To add a strategy:
    1. Create the file under strategies/board/ or strategies/wild/
    2. Import the class here
    3. Add it to the appropriate registry list below
"""

from app.buildings.sourcing.strategies.base import DiscoveryStrategy
from app.buildings.sourcing.strategies.board.awesome_lists import AwesomeListsStrategy


# === Board strategies — feed the career pipeline ===
_BOARD_STRATEGIES: list[DiscoveryStrategy] = [
    AwesomeListsStrategy(),
    # Pass 2b:
    # DirectoryCrawlStrategy(),
    # SearchRotationStrategy(),
]


# === Wild strategies — feed the gig pipeline (Pass 3) ===
_WILD_STRATEGIES: list[DiscoveryStrategy] = [
    # RedditWatcher(),
    # HnWhoIsHiring(),
    # ForumSeeder(),
]


def get_board_strategies() -> list[DiscoveryStrategy]:
    """Return all registered board strategies."""
    return _BOARD_STRATEGIES


def get_wild_strategies() -> list[DiscoveryStrategy]:
    """Return all registered wild strategies."""
    return _WILD_STRATEGIES


def get_strategy(name: str) -> DiscoveryStrategy | None:
    """Look up any strategy by name across both pipelines."""
    for s in (*_BOARD_STRATEGIES, *_WILD_STRATEGIES):
        if s.name == name:
            return s
    return None