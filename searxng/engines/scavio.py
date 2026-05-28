# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Scavio Google Search API — SearXNG offline engine for Scavio's paid Google API.

Smart fallback: checks whether any Google-named engine is suspended
(CAPTCHA'd, rate-limited) before calling the Scavio API. Returns empty
results when Google works fine, preserving API credits.

Fallback chain: google → google camofox → scavio → tavily

Configuration in ``settings.yml``::

  - name: scavio
    engine: scavio
    shortcut: scv
    scavio_api_key: $SCAVIO_API_KEY
    timeout: 10.0
    weight: 1.5
    disabled: false
"""

from json import loads, dumps
from logging import getLogger
from time import time
from urllib.request import Request, urlopen

from searx.exceptions import SearxEngineAPIException

logger = getLogger("searx.engines.scavio")

# ---------------------------------------------------------------------------
# Module-level variables — overridable in settings.yml
# ---------------------------------------------------------------------------

about = {
    "website": "https://scavio.dev",
    "wikidata_id": None,
    "official_api_documentation": "https://scavio.dev/docs/search-api",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

scavio_api_key = ""
"""Scavio API key. Set in ``settings.yml`` via the ``scavio_api_key`` field
(use ``$SCAVIO_API_KEY`` env var reference)."""

paging = False
"""Scavio search returns a single page of results."""

engine_type = "offline"
"""Makes a direct stdlib HTTP POST to the Scavio API, bypassing SearXNG's
network layer entirely."""

categories = ["general", "web"]
"""Available across general and web categories. The ``search()`` function
returns empty when no Google engine is suspended — only calls the API when
a fallback is genuinely needed."""

search_url = ""
"""Unused — all logic in :py:obj:`search`."""

# ---------------------------------------------------------------------------
# SearXNG engine interface
# ---------------------------------------------------------------------------


def search(query, params):
    """Query Scavio Google Search API and return results.

    Smart fallback: checks whether any Google-named engine is currently
    suspended. Only calls the Scavio API when a Google engine needs a
    fallback. Returns empty otherwise, preserving API credits.
    """
    if not query or not query.strip():
        return []

    # Skip if no Google engine is suspended — Google works fine
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
                        "Google engine %r suspended — Scavio will respond",
                        eng_name,
                    )
                    break
    except ImportError:
        logger.warning("Could not check Google suspension — Scavio always queries")
        google_suspended = True

    if not google_suspended:
        logger.debug("No Google engines suspended — Scavio skipping API call")
        return []

    return _call_scavio(query)


def _call_scavio(query):
    """Make the actual Scavio API call."""
    if not scavio_api_key or not scavio_api_key.strip():
        logger.error(
            "Scavio API key not configured — set scavio_api_key in settings.yml"
        )
        return []

    api_key = scavio_api_key.strip()

    try:
        body = dumps({"query": query}).encode()

        req = Request(
            "https://api.scavio.dev/api/v1/google",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
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

        logger.debug("Scavio returned %d results for query=%r", len(results), query)
        return results

    except Exception as exc:
        logger.error("Scavio search failed: %s", exc)
        raise SearxEngineAPIException(f"Scavio search failed: {exc}") from exc
