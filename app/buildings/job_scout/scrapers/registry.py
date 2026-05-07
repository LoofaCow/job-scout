"""
Scraper registry — maps source names to scraper instances.

Every scraper class registers itself here so the scout can look it up by
the source name from Profile.enabled_sources without hard-coding imports.
"""

from app.buildings.job_scout.scrapers.base import Scraper
from app.buildings.job_scout.scrapers.remoteok import RemoteOKScraper


# When you add a scraper:
#   1. Create the file under scrapers/
#   2. Import the class here
#   3. Add it to the dict below keyed by its source_name
_REGISTRY: dict[str, Scraper] = {
    RemoteOKScraper.source_name: RemoteOKScraper(),
    # Future:
    # IndeedScraper.source_name: IndeedScraper(),
    # LinkedInScraper.source_name: LinkedInScraper(),
}


def get_scraper(source_name: str) -> Scraper | None:
    """Return the scraper for a source name, or None if unregistered."""
    return _REGISTRY.get(source_name)


def all_enabled_scrapers(enabled_sources: list[str]) -> list[Scraper]:
    """
    Return scraper instances for the given list of source names.
    Silently skips names that aren't registered (with a printed warning),
    so a typo in profile.py doesn't break the whole run.
    """
    scrapers = []
    for name in enabled_sources:
        scraper = get_scraper(name)
        if scraper is None:
            print(f"⚠ Unknown scraper source in profile: {name!r}")
            continue
        scrapers.append(scraper)
    return scrapers