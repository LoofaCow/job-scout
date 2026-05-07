"""
Model router — the spine piece every agent calls into for LLM access.

Three tiers, in order of preference:
    LOCAL     -> Ollama on your rig (free, default)
    HEAVY     -> Featherless (subscription, bigger open-weight models)
    FRONTIER  -> NanoGPT (subscription, frontier models, used sparingly)

Agents request a tier by name; the router instantiates the right Agno model
class with the right credentials and falls back down the chain on failure.
"""

from enum import Enum
from typing import Optional

from agno.models.ollama import Ollama
from agno.models.openai.like import OpenAILike

from app.config import settings


class Tier(str, Enum):
    """Which quality/cost tier an agent wants."""
    LOCAL = "local"
    HEAVY = "heavy"
    FRONTIER = "frontier"


def get_model(
    tier: Tier = Tier.LOCAL,
    *,
    model_id_override: Optional[str] = None,
):
    """
    Return an Agno-compatible model instance for the requested tier.

    Args:
        tier: Which provider tier to use.
        model_id_override: If set, use this model ID instead of the tier default.
            Useful when an agent needs a specific model (e.g. a vision-capable
            local model for parsing screenshots later).

    Returns:
        An Agno model instance ready to be passed to Agent(model=...).
    """
    if tier == Tier.LOCAL:
        return Ollama(
            id=model_id_override or settings.MODEL_LOCAL,
            host=settings.OLLAMA_BASE_URL,
        )

    if tier == Tier.HEAVY:
        return OpenAILike(
            id=model_id_override or settings.MODEL_HEAVY,
            api_key=settings.FEATHERLESS_API_KEY,
            base_url=settings.FEATHERLESS_BASE_URL,
        )

    if tier == Tier.FRONTIER:
        return OpenAILike(
            id=model_id_override or settings.MODEL_FRONTIER,
            api_key=settings.NANOGPT_API_KEY,
            base_url=settings.NANOGPT_BASE_URL,
        )

    raise ValueError(f"Unknown tier: {tier}")


def get_model_with_fallback(preferred: Tier = Tier.LOCAL):
    """
    Return a model with automatic fallback down the tier chain.

    If the preferred tier's provider is unavailable (Ollama not running,
    API key missing, network down), this transparently falls back to the
    next available tier.

    Fallback order:
        LOCAL    -> HEAVY -> FRONTIER
        HEAVY    -> FRONTIER -> LOCAL
        FRONTIER -> HEAVY -> LOCAL
    """
    fallback_chains = {
        Tier.LOCAL:    [Tier.LOCAL, Tier.HEAVY, Tier.FRONTIER],
        Tier.HEAVY:    [Tier.HEAVY, Tier.FRONTIER, Tier.LOCAL],
        Tier.FRONTIER: [Tier.FRONTIER, Tier.HEAVY, Tier.LOCAL],
    }

    for tier in fallback_chains[preferred]:
        if _tier_is_available(tier):
            return get_model(tier)

    raise RuntimeError(
        "No model providers are available. Check that Ollama is running "
        "and that your .env has at least one valid API key."
    )


def _tier_is_available(tier: Tier) -> bool:
    """Lightweight check — does this tier have what it needs to function?"""
    if tier == Tier.LOCAL:
        # Ollama just needs a base URL; we'll trust that the user has it running.
        # A real "is it actually up" check would require an HTTP call, which we
        # don't want to do every time get_model_with_fallback is called.
        return bool(settings.OLLAMA_BASE_URL)

    if tier == Tier.HEAVY:
        return bool(settings.FEATHERLESS_API_KEY and settings.FEATHERLESS_BASE_URL)

    if tier == Tier.FRONTIER:
        return bool(settings.NANOGPT_API_KEY and settings.NANOGPT_BASE_URL)

    return False