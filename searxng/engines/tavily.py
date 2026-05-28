# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tavily Search API — SearXNG offline engine that queries the Tavily search API.

Used as a reliable fallback when Google (direct or Camoufox) is CAPTCHA'd
or rate-limited. Tavily is a paid API aggregating Google search results.

Configuration in ``settings.yml``::

  - name: tavily
    engine: tavily
    shortcut: tv
    tavily_api_key: your_key_here
    timeout: 10.0
    categories: general, web
    weight: 0.5
    disabled: false
"""

from json import loads, dumps
from logging import getLogger
from time import time
from urllib.request import Request, urlopen

from searx.exceptions import SearxEngineAPIException

logger = getLogger("searx.engines.tavily")

# ---------------------------------------------------------------------------
# Module-level variables — overridable in settings.yml
# ---------------------------------------------------------------------------

about = {
    "website": "https://tavily.com",
    "wikidata_id": None,
    "official_api_documentation": "https://docs.tavily.com",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

tavily_api_key = ""
"""Tavily API key. Set in ``settings.yml`` via the ``tavily_api_key`` field."""

paging = False
"""Tavily search is not paginated — single page of results."""

engine_type = "offline"
"""Makes a direct stdlib HTTP POST to the Tavily API, bypassing SearXNG's
network layer entirely."""

categories = ["general", "web"]
"""Available across general and web categories. The ``search()`` function
returns empty results when no Google engine is suspended — Tavily only
fires its API call when Google engines need a fallback. Use ``!tv`` to
force a Tavily search regardless."""

search_url = ""
"""Unused — all logic in :py:obj:`search`."""

# ---------------------------------------------------------------------------
# SearXNG engine interface
# ---------------------------------------------------------------------------


def search(query, params):
    """Query Tavily Search API and return results.

    Smart fallback: checks whether any Google-named engine is currently
    suspended (CAPTCHA'd, rate-limited). Tavily only calls the API when
    a Google engine needs a fallback. Returns empty otherwise, preserving
    API quota.

    Use the ``!tv`` shortcut to manually trigger a Tavily search.
    """
    if not query or not query.strip():
        return []

    # Check if any Google engine is currently suspended — if not, skip
    # Tavily entirely to conserve API quota.
    google_suspended = False
    try:
        from searx.engines import engines as _engines  # noqa: PLC0415

        now = time()
        for eng_name, eng_mod in _engines.items():
            eng_lower = str(eng_name).lower()
            if "google" in eng_lower:
                end_time = getattr(eng_mod, "suspend_end_time", 0)
                if end_time > now:
                    google_suspended = True
                    logger.debug(
                        "Google engine %r suspended until %d — Tavily will respond",
                        eng_name,
                        end_time,
                    )
                    break
    except ImportError:
        logger.warning("Could not check Google suspension — falling back to always-on")
        google_suspended = True  # Be safe: try Tavily

    if not google_suspended:
        logger.debug(
            "No Google engines suspended — Tavily skipping API call to save quota"
        )
        return []

    return _call_tavily(query)


def _call_tavily(query):
    """Make the actual Tavily API call. Extracted for clarity."""
    if not tavily_api_key or not tavily_api_key.strip():
        logger.error("Tavily API key not configured — set tavily_api_key in settings.yml")
        return []

    api_key = tavily_api_key.strip()

    try:
        body = dumps(
            {
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
                "include_images": False,
            }
        ).encode()

        req = Request(
            "https://api.tavily.com/search",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urlopen(req, timeout=10) as resp:
            data = loads(resp.read().decode())

        raw_results = data.get("results", [])
        results = []
        for r in raw_results:
            title = r.get("title", "").strip()
            url = r.get("url", "").strip()
            content = r.get("content", "").strip()
            if title and url:
                results.append(
                    {
                        "title": title,
                        "url": url,
                        "content": content,
                    }
                )

        logger.debug("Tavily returned %d results for query=%r", len(results), query)
        return results

    except Exception as exc:
        logger.error("Tavily search failed: %s", exc)
        raise SearxEngineAPIException(f"Tavily search failed: {exc}") from exc
