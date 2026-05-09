"""
Shared link-plausibility filter for board strategies.

awesome_lists, directory_crawl, and search_rotation all extract URLs from
human-curated content. They all need the same set of "obvious non-source"
skip rules. Putting the rules here means adding a new skip domain hits
every strategy at once.
"""

from urllib.parse import urlparse


# Domains that aren't job sources even when listed alongside them.
# Code hosting, social, blogging platforms, link-list infrastructure.
SKIP_DOMAINS: set[str] = {
    # Code hosting / link infra
    "github.com",
    "raw.githubusercontent.com",
    "gist.github.com",
    "gitlab.com",
    # Social
    "twitter.com",
    "x.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "reddit.com",
    "wikipedia.org",          # we may seed *from* WP, but its own links aren't sources
    # Donate/paywalls
    "patreon.com",
    "ko-fi.com",
    "buymeacoffee.com",
    "paypal.com",
    # Blogging platforms — articles, not boards
    "medium.com",
    "substack.com",
    "dev.to",
    "hashnode.com",
    "wordpress.com",
    "blogspot.com",
    "tumblr.com",
}


# Path fragments that signal an article/blog post even on otherwise-good domains.
# A jobsite.com/blog/best-practices link is not a job-board entry point.
SKIP_PATH_FRAGMENTS: tuple[str, ...] = (
    "/blog/",
    "/blogs/",
    "/article/",
    "/articles/",
    "/post/",
    "/posts/",
    "/news/",
    "/podcast/",
    "/podcasts/",
    "/wiki/",
)


# File-extension blacklist: these are downloads, not pages.
_FILE_EXT_SKIP = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".zip", ".tar.gz")


def is_plausible_board_url(url: str) -> bool:
    """Cheap pre-filter — drops obvious non-boards before verifier sees them."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False

    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    for skip in SKIP_DOMAINS:
        if domain == skip or domain.endswith("." + skip):
            return False

    path = parsed.path.lower()

    for fragment in SKIP_PATH_FRAGMENTS:
        if fragment in path:
            return False

    if path.endswith(_FILE_EXT_SKIP):
        return False

    return True