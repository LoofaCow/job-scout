"""
Discovery strategy protocol — every strategy plugin implements this interface.

Strategies are intentionally narrow. They know one way to find sources (or
gigs, in WildHunter's case) and emit candidates. They don't verify, persist,
or score — that's the agent's job. A strategy that scrapes an awesome-list
GitHub repo is one strategy. A strategy that polls r/forhire is another.

Hunters orchestrate strategies; strategies don't know about hunters.
"""

from typing import TYPE_CHECKING, Any, AsyncIterator, Optional, Protocol

from pydantic import BaseModel, Field

from app.buildings.sourcing.models import SourcePipeline, SourceType

if TYPE_CHECKING:
    from app.buildings.sourcing.http_client import PoliteFetcher


class SourceCandidate(BaseModel):
    """
    A possibly-real source produced by a discovery strategy.

    The verifier turns these into Source rows (or rejects them). A candidate
    captures what the strategy saw, not what the registry needs — fields the
    strategy can't know (quality_score, scraper_hint structure) are filled
    in later by the verifier.
    """
    url: str
    name: Optional[str] = None
    suggested_type: SourceType
    target_pipeline: SourcePipeline
    discovery_context: str = Field(
        description="One-sentence reason the strategy thinks this is a real source."
    )
    raw_evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Anything the strategy gathered that the verifier might want.",
    )


class GigCandidate(BaseModel):
    """
    A possibly-real direct gig produced by a wild-side strategy.

    WildHunter strategies emit either SourceCandidates (recurring places) or
    GigCandidates (single opportunities). The hunter's classifier decides
    which the strategy meant if it's ambiguous.
    """
    title: str
    description: str
    url: Optional[str] = None
    external_id: Optional[str] = None
    poster: Optional[str] = None
    pay_text: Optional[str] = None
    contact_url: Optional[str] = None
    contact_method: Optional[str] = None
    raw_evidence: dict[str, Any] = Field(default_factory=dict)


class DiscoveryStrategy(Protocol):
    """
    A discovery strategy plugin. Identified by `name`, which must be unique
    across all strategies registered to a hunter.

    Each strategy targets one of the two pipelines (career or gig) and emits
    a stream of candidates as it finds them. Async iterator so a slow strategy
    (paginating an API) yields incrementally instead of buffering.
    """

    name: str
    target_pipeline: SourcePipeline

    def discover(
        self,
        fetcher: "PoliteFetcher",
    ) -> AsyncIterator[SourceCandidate | GigCandidate]:
        """
        Yield candidates as the strategy finds them. May yield zero items.

        The strategy:
            - MUST use the provided fetcher for any HTTP work
            - SHOULD log progress at INFO
            - SHOULD NOT touch the DB; that's the hunter's job
            - SHOULD raise on hard failures (auth broken, API down)
            - SHOULD NOT raise on partial failures (one item parsed weirdly)
        """
        ...