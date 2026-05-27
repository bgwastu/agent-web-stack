#!/usr/bin/env python3
"""
Tavily-compatible API shim.

Routes search through SearXNG and extraction through Camoufox browser.
Acts as a drop-in replacement for api.tavily.com so any app that supports
Tavily can use this self-hosted stack instead.

Endpoints:
  POST /search   — Tavily-compatible web search (via SearXNG)
  POST /extract  — Tavily-compatible URL extraction (via Camoufox)
  GET  /health   — Health check
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import unescape

# ── Configuration ──────────────────────────────────────────────────────────
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
CAMOFOX_URL = os.environ.get("CAMOFOX_URL", "http://camofox-browser:9377").rstrip("/")

HOST = os.environ.get("TAVILY_SHIM_HOST", "0.0.0.0")
PORT = int(os.environ.get("TAVILY_SHIM_PORT", "33879"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tavily-shim")


# ── HTTP Server ────────────────────────────────────────────────────────────

class TavilyHandler(BaseHTTPRequestHandler):
    """HTTP handler implementing Tavily-compatible API."""

    def do_GET(self):
        if self.path == "/health":
            return self._handle_health()
        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return

        # Validate API key
        api_key = body.get("api_key", "")
        if TAVILY_API_KEY and api_key != TAVILY_API_KEY:
            self._send_json(401, {"error": "Invalid API key"})
            return

        if self.path == "/search":
            return self._handle_search(body)
        elif self.path == "/extract":
            return self._handle_extract(body)
        else:
            self._send_json(404, {"error": "Not found. Use /search or /extract"})

    # ── Health ───────────────────────────────────────────────────────────

    def _handle_health(self):
        status = {"ok": True, "engine": "tavily-shim"}
        camofox_ok = _check_camofox_health()
        searxng_ok = _check_searxng_health()
        status["upstream"] = {
            "camoufox": camofox_ok,
            "searxng": searxng_ok,
        }
        http_code = 200 if (camofox_ok and searxng_ok) else 503
        self._send_json(http_code, status)

    # ── Search ──────────────────────────────────────────────────────────

    def _handle_search(self, body):
        query = (body.get("query") or "").strip()
        if not query:
            self._send_json(400, {"error": "query is required"})
            return

        search_depth = body.get("search_depth", "basic")
        max_results = body.get("max_results", 5)
        if search_depth == "advanced":
            max_results = min(max_results, 20)
        else:
            max_results = min(max_results, 5)
        include_answer = body.get("include_answer", False)
        include_images = body.get("include_images", False)
        include_raw_content = body.get("include_raw_content", False)

        log.info("search: q=%r depth=%s max=%d", query, search_depth, max_results)

        try:
            results, answer_text = _search_searxng(query, max_results, include_answer)
        except Exception as e:
            log.error("search failed: %s", e)
            self._send_json(502, {"error": f"Search backend error: {e}"})
            return

        # Format as Tavily response
        tavily_results = []
        for r in results:
            entry = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0.5),
                "raw_content": r.get("content") if include_raw_content else None,
            }
            tavily_results.append(entry)

        response = {
            "query": query,
            "follow_up_questions": None,
            "answer": answer_text if include_answer else None,
            "images": [],
            "results": tavily_results,
        }

        if include_images:
            img_results = [r for r in results if r.get("img_src")]
            response["images"] = img_results[:max_results]

        self._send_json(200, response)

    # ── Extract ─────────────────────────────────────────────────────────

    def _handle_extract(self, body):
        urls_raw = body.get("urls", "")
        if isinstance(urls_raw, str):
            urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
        elif isinstance(urls_raw, list):
            urls = urls_raw
        else:
            urls = []

        if not urls:
            self._send_json(400, {"error": "urls is required (comma-separated string or array)"})
            return

        include_images = body.get("include_images", False)

        log.info("extract: %d urls, images=%s", len(urls), include_images)

        # Extract each URL using Camoufox
        results = []
        errors = []
        for url in urls:
            try:
                extracted = _extract_via_camoufox(url, include_images)
                if extracted:
                    results.append(extracted)
                else:
                    errors.append({"url": url, "error": "No content extracted"})
            except Exception as e:
                log.error("extract failed for %s: %s", url, e)
                errors.append({"url": url, "error": str(e)})

        response = {
            "results": results,
        }
        if errors:
            response["errors"] = errors

        self._send_json(200, response)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw)
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON body: {e}"})
            return None

    def _send_json(self, status_code, data):
        body = json.dumps(data).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "tavily-shim/1.0")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)


# ── Upstream helpers ───────────────────────────────────────────────────────

def _check_camofox_health():
    try:
        req = urllib.request.Request(f"{CAMOFOX_URL}/health", method="GET")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return data.get("ok", False)
    except Exception:
        return False


def _check_searxng_health():
    try:
        req = urllib.request.Request(
            f"{SEARXNG_URL}/search?q=health&format=json",
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return len(data.get("results", [])) >= 0
    except Exception:
        return False


def _search_searxng(query, max_results, include_answer=False):
    """Search via SearXNG JSON API and return (results, answer)."""
    params = urllib.parse.urlencode({"q": query, "format": "json"})
    url = f"{SEARXNG_URL}/search?{params}"

    req = urllib.request.Request(url, method="GET")
    resp = urllib.request.urlopen(req, timeout=20)
    data = json.loads(resp.read())

    results = []
    for item in data.get("results", []):
        results.append({
            "title": unescape(item.get("title", "")),
            "url": item.get("url", ""),
            "content": _clean_content(item.get("content", "")),
            "score": _compute_score(item),
            "img_src": item.get("img_src", ""),
            "engine": item.get("engine", ""),
        })

    # Sort by score descending, Tavily-style
    results.sort(key=lambda r: r["score"], reverse=True)

    # Generate simple answer from top results if requested
    answer = None
    if include_answer and results:
        snippets = []
        for r in results[:3]:
            snippets.append(f"{r['title']}: {r['content'][:200]}")
        answer = " | ".join(snippets)

    return results[:max_results], answer


def _extract_via_camoufox(url, include_images=False):
    """Extract page content via Camoufox browser."""
    # Create a tab
    tab_req = urllib.request.Request(
        f"{CAMOFOX_URL}/tabs",
        data=json.dumps({"userId": "tavily-shim", "sessionKey": "extract", "url": url}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    tab_resp = urllib.request.urlopen(tab_req, timeout=30)
    tab_data = json.loads(tab_resp.read())
    tab_id = tab_data.get("tabId")
    if not tab_id:
        return None

    try:
        # Navigate
        nav_req = urllib.request.Request(
            f"{CAMOFOX_URL}/tabs/{tab_id}/navigate",
            data=json.dumps({"userId": "tavily-shim", "url": url}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(nav_req, timeout=30)

        # Wait for page to render
        time.sleep(2)

        # Get snapshot
        snap_req = urllib.request.Request(
            f"{CAMOFOX_URL}/tabs/{tab_id}/snapshot?userId=tavily-shim",
            method="GET",
        )
        snap_resp = urllib.request.urlopen(snap_req, timeout=15)
        snap_data = json.loads(snap_resp.read())

        snapshot_text = snap_data.get("snapshot", "")
        title = ""
        if snapshot_text:
            title_match = re.search(r'heading\s+"([^"]+)"', snapshot_text)
            if title_match:
                title = title_match.group(1)

        # Get page title from URL metadata
        if not title:
            title = snap_data.get("title", url)

        return {
            "url": url,
            "title": title,
            "raw_content": snapshot_text,
        }
    finally:
        # Close tab
        try:
            close_req = urllib.request.Request(
                f"{CAMOFOX_URL}/tabs/{tab_id}?userId=tavily-shim",
                method="DELETE",
            )
            urllib.request.urlopen(close_req, timeout=5)
        except Exception:
            pass


def _compute_score(item):
    """Compute a relevance score (0-1) similar to Tavily's scoring."""
    base = 0.5
    engine = item.get("engine", "")
    # Boost well-known engines
    if engine in ("google",):
        base += 0.25
    elif engine in ("bing", "startpage", "brave"):
        base += 0.15
    elif engine in ("duckduckgo", "qwant"):
        base += 0.05
    # Boost by position in results
    position = item.get("position", 50)
    position_score = max(0, 1.0 - position / 50) * 0.2
    return round(min(base + position_score, 1.0), 4)


def _clean_content(text):
    """Clean up content text."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if not TAVILY_API_KEY:
        log.warning("TAVILY_API_KEY not set — authentication disabled!")

    log.info("starting tavily-shim on http://%s:%d", HOST, PORT)
    log.info("  SEARXNG_URL = %s", SEARXNG_URL)
    log.info("  CAMOFOX_URL = %s", CAMOFOX_URL)

    server = ThreadingHTTPServer((HOST, PORT), TavilyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
