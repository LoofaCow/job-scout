"""
Researcher building — fills info gaps that Tear Apart flagged.

The researcher reads ListingAnalysis rows where research_status == QUEUED,
runs investigation strategies against them, writes findings back to the
analysis row, and marks status = COMPLETED. Tear Apart's reanalyze workflow
then re-runs on the richer text.

Today there is one strategy: fetch_full_listing — visit the URL, extract
the main content with trafilatura, store as full_description. Future
strategies (company_lookup via SearXNG, qualifications_clarify) live as
sibling modules under strategies/.
"""
