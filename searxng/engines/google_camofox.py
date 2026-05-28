# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Google via Camoufox — SearXNG offline engine that runs Google searches
through the Camoufox anti-detection browser.

Searches Google via the Camoufox headless browser API, bypassing rate
limits and CAPTCHAs that affect direct HTTP fetch of Google. Uses a
multi-step approach: homepage load → type query → press Enter, to
mimic human behavior better than a direct search URL navigation.

Configuration in ``settings.yml``::

  - name: google camofox
    engine: google_camofox
    shortcut: gc
    camofox_url: http://camofox-browser:9377
    timeout: 15.0
    categories: web
    disabled: false
"""

from json import loads, dumps
from logging import getLogger
from re import compile as _compile, match as _match
from time import sleep
from urllib.request import Request, urlopen

from searx.exceptions import SearxEngineAPIException

logger = getLogger("searx.engines.google_camofox")

# ---------------------------------------------------------------------------
# Module-level variables — can be overridden in settings.yml
# ---------------------------------------------------------------------------

about = {
    "website": "https://google.com",
    "wikidata_id": "Q9366",
    "official_api_documentation": None,
    "use_official_api": False,
    "require_api_key": False,
    "results": "accessibility-tree",
}

camofox_url = "http://camofox-browser:9377"
"""URL of the Camoufox Browser Server (e.g. ``http://camofox-browser:9377``)."""

paging = False
"""Pagination not yet supported — single-page Google SERP only."""

engine_type = "offline"
"""No HTTP requests are made by the SearXNG network layer — all Camoufox
browser work is done in :py:obj:`search`."""

categories = ["web"]
"""Categories this engine belongs to. Intentionally NOT in ``general``
to avoid being triggered by SearXNG healthchecks every 30s. Users
invoke via the ``!gc`` shortcut or by selecting the ``web`` category."""

search_url = ""
"""Unused — all search logic is in :py:obj:`search`."""

# ---------------------------------------------------------------------------
# Camoufox HTTP helpers  (uses stdlib — bypasses SearXNG's HTTP layer)
# ---------------------------------------------------------------------------

# Pattern for result entry: `- link "TITLE" [eN]:`
_LINK_RE = _compile(r'^- link "(.+?)"(?:\s+\[e\d+\])?:')


def _post(path, body, timeout=15):
    """POST JSON to Camoufox, return parsed response."""
    req = Request(
        f"{camofox_url}{path}",
        data=dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return loads(resp.read().decode())


def _get(path, timeout=15, user_id=None):
    """GET from Camoufox, return parsed response.

    Optionally appends ``userId`` as query parameter (required by endpoints
    like snapshot).
    """
    url = f"{camofox_url}{path}"
    if user_id:
        sep = "&" if "?" in path else "?"
        url = f"{url}{sep}userId={user_id}"
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return loads(resp.read().decode())


def _delete(path, body, timeout=10):
    """DELETE to Camoufox. Best-effort — exceptions are swallowed."""
    req = Request(
        f"{camofox_url}{path}",
        data=dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="DELETE",
    )
    try:
        with urlopen(req, timeout=timeout):
            pass
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Snapshot parsing
# ---------------------------------------------------------------------------


def _parse_snapshot(snapshot_text):
    """Parse Google result entries from a Camoufox accessibility-tree snapshot.

    Returns a list of dicts with keys ``title``, ``url``, ``content``.
    """
    results = []
    lines = snapshot_text.split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        m = _LINK_RE.match(stripped)
        if not m:
            i += 1
            continue

        title = m.group(1)
        link_url = ""
        snippet = ""

        j = i + 1
        while j < n:
            child = lines[j]
            if not child.startswith(" ") and not child.startswith("\t"):
                break
            cs = child.strip()
            if cs.startswith("/url:"):
                link_url = cs.split("/url:", 1)[1].strip()
            elif cs.startswith("text:"):
                snippet = cs.split("text:", 1)[1].strip()
            elif cs.startswith("cite:"):
                pass
            j += 1

        if link_url:
            results.append(
                {
                    "title": title,
                    "url": link_url,
                    "content": snippet,
                }
            )
        i = j

    return results


# ---------------------------------------------------------------------------
# SearXNG engine interface  (offline engine — uses search() not request())
# ---------------------------------------------------------------------------


def search(query, params):
    """Orchestrate a Google search via Camoufox browser and return results.

    Uses the ``offline`` engine type — all browser work (tab creation,
    navigation, type, snapshot, parsing, tab cleanup) is done in this
    single function, returning parsed results directly.

    Navigation strategy:
    1. Load google.com homepage (avoids triggering Google's bot detection
       that direct search-URL navigation can cause)
    2. Find the search combobox/input via accessibility tree or CSS selector
    3. Type the query and press Enter
    4. Wait for results to render
    5. Fetch snapshot and parse result entries
    """
    if not query or not query.strip():
        return []

    user_id = "searxng"
    key = f"google_{params.get('pageno', 1)}"
    tab_id = None

    try:
        # 1. Create tab
        tab_data = _post(
            "/tabs",
            {"userId": user_id, "sessionKey": key},
            timeout=10,
        )
        tab_id = tab_data.get("tabId")
        if not tab_id:
            raise SearxEngineAPIException("Camoufox did not return a tabId")

        # 2. Navigate to Google homepage
        _post(
            f"/tabs/{tab_id}/navigate",
            {"userId": user_id, "url": "https://www.google.com"},
            timeout=15,
        )

        # 3. Wait for page to settle
        sleep(1)

        # 4. Snapshot to find search input ref
        snap_data = _get(f"/tabs/{tab_id}/snapshot", timeout=10, user_id=user_id)
        snapshot_text = snap_data.get("snapshot", "")

        # Try combobox ref first, then searchbox, then CSS selector
        search_ref = None
        for line in snapshot_text.split("\n"):
            stripped = line.strip()
            m = _match(r'^- combobox ".*?"\s+\[(e\d+)\]', stripped)
            if m:
                search_ref = m.group(1)
                break
            m = _match(r'^- searchbox ".*?"\s+\[(e\d+)\]', stripped)
            if m:
                search_ref = m.group(1)
                break

        if search_ref:
            _post(
                f"/tabs/{tab_id}/type",
                {"userId": user_id, "ref": search_ref, "text": query, "pressEnter": True},
                timeout=15,
            )
        else:
            _post(
                f"/tabs/{tab_id}/type",
                {
                    "userId": user_id,
                    "selector": 'textarea[name="q"], input[name="q"]',
                    "text": query,
                    "pressEnter": True,
                },
                timeout=15,
            )

        # 5. Wait for search results to render
        sleep(2)

        # 6. Fetch results snapshot
        snap_data = _get(f"/tabs/{tab_id}/snapshot", timeout=15, user_id=user_id)
        snapshot = snap_data.get("snapshot", "")

        if snapshot:
            results = _parse_snapshot(snapshot)
            logger.debug("Parsed %d results for query=%r", len(results), query)
            return results

        logger.warning("Empty snapshot for query=%r", query)
        return []

    except SearxEngineAPIException:
        raise

    except Exception as exc:
        logger.error("Camoufox search failed: %s", exc)
        raise SearxEngineAPIException(
            f"Camoufox search failed: {exc}"
        ) from exc

    finally:
        # Always close the tab — best effort
        if tab_id:
            _delete(f"/tabs/{tab_id}", {"userId": user_id})
