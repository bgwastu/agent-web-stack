#!/usr/bin/env python3
"""
Tavily-compatible API shim with webfetch improvements.

Routes:
  POST /search   — Tavily-compatible web search (via SearXNG)
  POST /extract  — URL extraction (markdown.new → Camoufox fallback)
  POST /mcp      — MCP JSON-RPC endpoint (1:1 with Tavily tools)
  GET  /health   — Health check

Fetch strategy (extract):
  1. Camoufox-only (CAMOFOX_ONLY_SITES) → Camoufox browser
  2. Reddit URLs → Arctic Shift → PullPush → chained fallback
  3. GitHub URLs → raw.githubusercontent.com / GitHub API (skip_summary=True)
  4. File URLs → markdown.new/file-to-markdown → chained fallback
  5. Normal URLs → direct HTTP → markdown.new → r.jina.ai → Camoufox → None

AI summarization is automatically skipped for:
  - GitHub source results (github-readme, github-raw, github-api-*, github-wiki)
  - raw.githubusercontent.com, gist.githubusercontent.com domains
  - Any domain listed in SKIP_SUMMARY_SITES env var
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
from urllib.parse import urlparse

# ── Configuration ──────────────────────────────────────────────────────────
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
CAMOFOX_URL = os.environ.get("CAMOFOX_URL", "http://camofox-browser:9377").rstrip("/")

HOST = os.environ.get("TAVILY_SHIM_HOST", "0.0.0.0")
PORT = int(os.environ.get("TAVILY_SHIM_PORT", "33879"))

# Domains that always route through Camoufox browser (comma-separated)
# Matches the domain and all its subdomains/paths.
_CAMOFOX_ONLY_RAW = os.environ.get("CAMOFOX_ONLY_SITES", "").strip()
CAMOFOX_ONLY_SITES = {s.strip().lower() for s in _CAMOFOX_ONLY_RAW.split(",") if s.strip()}

# Domains that skip AI summarization (raw content returned as-is)
# Comma-separated, matches domain + all subdomains.
# Sources starting with "github-" also always skip summarization.
_SKIP_SUMMARY_SITES_RAW = os.environ.get("SKIP_SUMMARY_SITES", "").strip()
SKIP_SUMMARY_SITES = {s.strip().lower() for s in _SKIP_SUMMARY_SITES_RAW.split(",") if s.strip()}


# ── AI Summarizer Configuration ─────────────────────────────────────────────
# When enabled, extracted content is summarized via an OpenAI-compatible API
# before being returned. The summary replaces raw_content so Hermes' web_extract
# sees content < 5000 chars and passes it through without re-summarizing.
SUMMARIZE_EXTRACT = os.environ.get("SUMMARIZE_EXTRACT", "true").lower() in ("1", "true", "yes")
SUMMARIZER_BASE_URL = os.environ.get("SUMMARIZER_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
SUMMARIZER_API_KEY = os.environ.get("SUMMARIZER_API_KEY", "")
SUMMARIZER_MODEL = os.environ.get("SUMMARIZER_MODEL", "deepseek-v4-flash")
SUMMARIZER_MAX_CHARS = int(os.environ.get("SUMMARIZER_MAX_CHARS", "4900"))
SUMMARIZER_MAX_TOKENS = int(os.environ.get("SUMMARIZER_MAX_TOKENS", "8000"))  # 8K output tokens (reasoning suppressed → fits comfortably)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tavily-shim")

MAX_CONTENT_LENGTH = 500_000  # 500KB max extracted content


# ── HTTP Server ────────────────────────────────────────────────────────────

class TavilyHandler(BaseHTTPRequestHandler):
    """HTTP handler implementing Tavily-compatible API + MCP."""

    def do_GET(self):
        if self.path == "/health":
            return self._handle_health()
        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return

        # Validate API key (skip for health and MCP)
        if self.path not in ("/mcp",):
            api_key = body.get("api_key", "")
            if TAVILY_API_KEY and api_key != TAVILY_API_KEY:
                self._send_json(401, {"error": "Invalid API key"})
                return

        if self.path == "/search":
            return self._handle_search(body)
        elif self.path == "/extract":
            return self._handle_extract(body)
        elif self.path == "/mcp":
            return self._handle_mcp(body)
        else:
            self._send_json(404, {"error": "Not found. Use /search, /extract, or /mcp"})

    def do_HEAD(self):
        """Healtcheck via HEAD."""
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "Not found"})

    # ── Health ───────────────────────────────────────────────────────────

    def _handle_health(self):
        status = {"ok": True, "engine": "tavily-shim"}
        camofox_ok = _check_camofox_health()
        searxng_ok = _check_searxng_health()
        status["upstream"] = {
            "camoufox": camofox_ok,
            "searxng": searxng_ok,
            "markdown_new": True,  # external service, always reports true
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

        results = []
        errors = []
        for url in urls:
            try:
                extracted = _fetch_url(url, include_images)
                if extracted:
                    results.append(extracted)
                else:
                    errors.append({"url": url, "error": "No content extracted"})
            except Exception as e:
                log.error("extract failed for %s: %s", url, e)
                errors.append({"url": url, "error": str(e)})

        response = {"results": results}
        if errors:
            response["errors"] = errors

        self._send_json(200, response)

    # ── MCP Endpoint ─────────────────────────────────────────────────────

    def _handle_mcp(self, body):
        """MCP JSON-RPC 2.0 endpoint.

        Supports:
          - tools/list  → list available tools
          - tools/call  → call a tool by name
          - ping        → health check
        """
        req_id = body.get("id", None)
        method = body.get("method", "")
        params = body.get("params", {})

        # ── tools/list ───────────────────────────────────────────────
        if method == "tools/list":
            tools = [
                {
                    "name": "search",
                    "description": "Search the web. Returns results with titles, URLs, and content snippets.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "search_depth": {"type": "string", "enum": ["basic", "advanced"], "default": "basic"},
                            "max_results": {"type": "integer", "default": 5},
                            "include_answer": {"type": "boolean", "default": False},
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "extract",
                    "description": "Extract content from URLs. Uses markdown.new first, falls back to Camoufox browser for JS-rendered content.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "urls": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "URLs to extract",
                            },
                            "include_images": {"type": "boolean", "default": False},
                        },
                        "required": ["urls"],
                    },
                },
            ]
            return self._send_json_rpc(200, {"tools": tools}, req_id)

        # ── tools/call ───────────────────────────────────────────────
        elif method == "tools/call":
            tool_name = (params.get("name") or "").strip()
            arguments = params.get("arguments", {})

            if tool_name == "search":
                # Run search
                try:
                    results, answer = _search_searxng(
                        arguments.get("query", ""),
                        min(arguments.get("max_results", 5), 20),
                        arguments.get("include_answer", False),
                    )
                    tavily_results = []
                    for r in results:
                        tavily_results.append({
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "content": r.get("content", ""),
                            "score": r.get("score", 0.5),
                        })
                    content = json.dumps({
                        "query": arguments.get("query"),
                        "answer": answer if arguments.get("include_answer") else None,
                        "results": tavily_results,
                    })
                    return self._send_json_rpc(200, {
                        "content": [{"type": "text", "text": content}],
                    }, req_id)
                except Exception as e:
                    return self._send_json_rpc(200, {
                        "isError": True,
                        "content": [{"type": "text", "text": f"Search failed: {e}"}],
                    }, req_id)

            elif tool_name == "extract":
                urls = arguments.get("urls", [])
                if not urls:
                    return self._send_json_rpc(200, {
                        "isError": True,
                        "content": [{"type": "text", "text": "urls is required"}],
                    }, req_id)

                try:
                    all_results = []
                    for url in urls:
                        extracted = _fetch_url(url, arguments.get("include_images", False))
                        if extracted:
                            all_results.append(extracted)

                    content = json.dumps({"results": all_results})
                    return self._send_json_rpc(200, {
                        "content": [{"type": "text", "text": content}],
                    }, req_id)
                except Exception as e:
                    return self._send_json_rpc(200, {
                        "isError": True,
                        "content": [{"type": "text", "text": f"Extract failed: {e}"}],
                    }, req_id)
            else:
                return self._send_json_rpc(404, {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                }, req_id)

        # ── ping ──────────────────────────────────────────────────────
        elif method == "ping":
            return self._send_json_rpc(200, {}, req_id)

        else:
            return self._send_json_rpc(404, {
                "isError": True,
                "content": [{"type": "text", "text": f"Unknown method: {method}"}],
            }, req_id)

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
        self.send_header("Server", "tavily-shim/2.0")
        self.end_headers()
        self.wfile.write(body)

    def _send_json_rpc(self, status_code, result_or_error, req_id):
        """Send JSON-RPC 2.0 response."""
        data = {
            "jsonrpc": "2.0",
            "id": req_id,
        }
        if "isError" in result_or_error and result_or_error["isError"]:
            data["error"] = {
                "code": -32000,
                "message": result_or_error.get("content", [{}])[0].get("text", "Unknown error"),
            }
        else:
            data["result"] = result_or_error
        self._send_json(status_code, data)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)


# ── AI Summarization ────────────────────────────────────────────────────────

def _summarize_content(content, url="", title=""):
    """Extract the main article content from a web page using an LLM.

    Truncates content to 15K chars before sending. Retries up to 3 times
    on transient failures. Falls back to raw content if all retries fail.
    Returns (content, was_summarized) tuple.
    """
    if not SUMMARIZE_EXTRACT or not content or not SUMMARIZER_API_KEY:
        return content, False

    content_len = len(content)
    if content_len < 500:
        return content, False  # too short to bother

    max_chars = SUMMARIZER_MAX_CHARS
    if content_len <= max_chars * 2:
        return content, False  # already compact enough

    # Truncate input to 15K chars to avoid overwhelming the model
    MAX_INPUT_CHARS = 15000
    if content_len > MAX_INPUT_CHARS:
        truncated = content[:MAX_INPUT_CHARS]
        log.info(
            "truncated content from %d to %d chars for %s",
            content_len, MAX_INPUT_CHARS, title or url,
        )
    else:
        truncated = content

    prompt = (
        "You are a precise content extractor. Extract the main article content "
        "from the following page and remove all navigation, sidebars, ads, footers, "
        "cookie banners, related links, and other non-content elements. "
        "Preserve ALL important details: specific numbers, statistics, quotes, "
        "dates, names, technical terms, and nuanced arguments. "
        "Return the content as clean markdown keeping its original structure — "
        "headings, paragraphs, lists, tables, code blocks in their original order. "
        "Do NOT rewrite or restructure — just remove the junk.\n\n"
        f"Aim for about {max_chars} characters. "
        f"Hard maximum: {max_chars} characters. "
        "If the source is already markdown, keep its structure. "
        "If it's a forum post or discussion, preserve the arguments and counterpoints.\n\n"
        "IMPORTANT: Do NOT show your reasoning or thinking process. "
        "Output ONLY the extracted content directly, without any preamble or explanation. "
        "Skip the chain-of-thought and go straight to the answer.\n\n"
        "PAGE CONTENT:\n"
        f"{'─' * 60}\n"
        f"Title: {title}\n"
        f"URL: {url}\n"
        f"{'─' * 60}\n"
        f"{truncated}\n"
        f"{'─' * 60}"
    )

    body = json.dumps({
        "model": SUMMARIZER_MODEL,
        "messages": [
            {"role": "system", "content": "You extract article content from web pages. Remove navigation, sidebar, footer, ads. Return only the article in the original format. No summaries, no rewrites, no explanations."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": SUMMARIZER_MAX_TOKENS if SUMMARIZER_MAX_TOKENS else int(max_chars * 1.8),
        "temperature": 0.0,  # deterministic once thinking is off
        "thinking": {"type": "disabled"},  # disable reasoning/thinking for speed
    }).encode()

    # Retry up to 3 times with exponential backoff
    max_retries = 3
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                f"{SUMMARIZER_BASE_URL}/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {SUMMARIZER_API_KEY}",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=300)
            data = json.loads(resp.read())
            choice = data["choices"][0]["message"]
            summary = (choice.get("content") or choice.get("reasoning_content") or choice.get("reasoning") or "").strip()

            # Enforce hard cap
            if len(summary) > max_chars:
                summary = summary[:max_chars].rsplit(" ", 1)[0] + " […]"

            compression = (1 - len(summary) / content_len) * 100
            log.info(
                "summarized %s -> %d chars (%.1f%% compression) using %s (attempt %d/%d)",
                title or url, len(summary), compression,
                SUMMARIZER_MODEL, attempt, max_retries,
            )
            return summary, True  # True = summarization succeeded

        except Exception as e:
            last_error = e
            if attempt < max_retries:
                backoff = 2 ** attempt  # 2, 4, 8 seconds
                log.warning(
                    "summarization attempt %d/%d failed for %s: %s — retrying in %ds",
                    attempt, max_retries, url, e, backoff,
                )
                time.sleep(backoff)

    log.warning(
        "all %d summarization attempts failed for %s: %s — returning raw content",
        max_retries, url, last_error,
    )
    return content, False  # all retries exhausted, return raw


# ── Fetch Strategy ─────────────────────────────────────────────────────────

def _should_skip_summary(result):
    """Check if summarization should be skipped for this result.

    Skips for:
    - Results explicitly marked with skip_summary=True
    - Sources starting with "github-" (already clean raw/markdown)
    - Domains in SKIP_SUMMARY_SITES config
    """
    if result.get("skip_summary"):
        return True
    source = result.get("source", "")
    if source.startswith("github-"):
        return True
    url = result.get("url", "")
    if url:
        domain = urlparse(url).netloc.lower()
        # Always skip summarization for raw GitHub/gist content
        if domain in ("raw.githubusercontent.com", "gist.githubusercontent.com"):
            return True
        if SKIP_SUMMARY_SITES:
            parts = domain.split(".")
            for i in range(len(parts)):
                if ".".join(parts[i:]) in SKIP_SUMMARY_SITES:
                    return True
    return False


def _apply_summarizer(result):
    """Run a fetch result through the AI content cleaner.

    Strips navigation, sidebars, ads, footers, and other non-content boilerplate
    from large pages. The clean content is returned as `raw_content` (≤4900 chars)
    so the Tavily provider sees content < 5000 chars and Hermes skips its own LLM
    summarization. Small content passes through untouched.

    Skips summarization entirely for GitHub sources and domains in
    SKIP_SUMMARY_SITES (raw content returned as-is).
    """
    if not result or not result.get("raw_content"):
        return result

    if _should_skip_summary(result):
        raw = result["raw_content"]
        result["content"] = raw
        result["summarized"] = False
        return result

    raw = result["raw_content"]
    title = result.get("title", "")
    url = result.get("url", "")

    # Summarize if enabled and content is large enough
    needs_summary = SUMMARIZE_EXTRACT and len(raw) > 5000
    if needs_summary:
        summarized, was_summarized = _summarize_content(raw, url, title)
        result["raw_content"] = summarized
        result["content"] = summarized  # provider uses content if raw_content absent
        result["summarized"] = was_summarized  # True only if LLM actually succeeded
        result["original_size"] = len(raw)
    else:
        # Still return as `content` so provider has a clean path
        result["content"] = raw

    return result


def _fetch_url(url, include_images=False):
    """Extract content from a URL using a multi-tier fallback chain.

    Normal sites (outside CAMOFOX_ONLY_SITES):
      Tier 1: Direct HTTP GET (fastest, static pages)
      Tier 2: markdown.new/<url> (JS rendering, clean markdown)
      Tier 3: r.jina.ai/<url> (JS rendering, markdown, fallback reader)
      Tier 4: Manual Camoufox browser (full JS + proxy for block-heavy sites)
      Tier 5: All exhausted → None

    CAMOFOX_ONLY_SITES (linkedin, facebook, x.com, etc.):
      Camoufox browser directly (bypasses tiers 1-3)

    Reddit URLs get their own Arctic Shift / PullPush API chain first,
    then fall through to the chained extract above.

    File URLs get markdown.new/file-to-markdown first,
    then fall through to the chained extract above.

    After fetching, if SUMMARIZE_EXTRACT is enabled and content is large,
    runs it through an LLM summarizer. Returns `content` (not `raw_content`)
    so the Tavily provider picks it up as-is (< 5000 chars → Hermes skips LLM).
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    # ── Camoufox-only sites (configured via CAMOFOX_ONLY_SITES) ──────
    if _is_camofox_only_site(domain):
        log.info("camofox-only fetch: %s (via Camoufox, enforced by CAMOFOX_ONLY_SITES)", url)
        result = _extract_via_camoufox(url, include_images)
        if result:
            return _apply_summarizer(result)
        return None

    # ── Reddit special handling ──────────────────────────────────────
    if _is_reddit_url(domain, url):
        log.info("reddit fetch: %s (custom scraper via Camoufox)", url)
        result = _fetch_reddit_custom(url)
        if result:
            return _apply_summarizer(result)
        log.info("reddit scraper returned empty, fallback to chained extract: %s", url)

    # ── GitHub special handling ────────────────────────────────────
    if _is_github_url(domain):
        log.info("github fetch: %s (recognized GitHub domain, passthrough)", url)
        # First-tier handler returns None → falls through to chained fallback
        result = _fetch_via_github(url)
        if result:
            return _apply_summarizer(result)

    # ── Detect downloadable files ────────────────────────────────────
    if _is_file_url(url):
        log.info("file fetch: %s (via markdown.new/file-to-markdown)", url)
        result = _fetch_via_markdown_file(url)
        if result:
            return _apply_summarizer(result)
        log.info("file-to-markdown failed, fallback to chained extract: %s", url)

    # ── Chained fallback for normal URLs ─────────────────────────────
    # Tier 1: Direct HTTP fetch (fastest, free, static pages)
    log.info("tier-1 direct fetch: %s", url)
    result = _fetch_direct(url)
    if result:
        return _apply_summarizer(result)

    # Tier 2: markdown.new (renders JS, returns clean markdown)
    log.info("tier-2 markdown.new: %s", url)
    result = _fetch_via_markdown_new(url)
    if result:
        return _apply_summarizer(result)

    # Tier 3: r.jina.ai Reader (JS rendering, markdown output)
    log.info("tier-3 r.jina.ai: %s", url)
    result = _fetch_via_jina(url)
    if result:
        return _apply_summarizer(result)

    # Tier 4: Manual Camoufox browser (full JS, proxy for block-heavy sites)
    log.info("tier-4 Camoufox browser: %s", url)
    result = _extract_via_camoufox(url, include_images)
    if result:
        return _apply_summarizer(result)

    # Tier 5: All exhausted — nothing could extract this URL
    log.warning("all 5 extract tiers failed for: %s", url)
    return None


def _is_reddit_url(domain, url):
    """Check if a URL is a Reddit URL."""
    reddit_domains = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com",
                      "i.reddit.com", "redd.it", "reddit.app.link"}
    base = domain
    # Handle subdomains
    parts = domain.split(".")
    if len(parts) >= 2:
        base = ".".join(parts[-2:])
    return base in reddit_domains


def _is_camofox_only_site(domain):
    """Check if a domain is in the CAMOFOX_ONLY_SITES list (incl. subdomains)."""
    if not CAMOFOX_ONLY_SITES:
        return False
    # Check exact match + subdomain match
    # e.g. "x.com" matches "x.com", "www.x.com", "api.x.com", "pbs.twimg.com"
    parts = domain.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in CAMOFOX_ONLY_SITES:
            return True
    return False


# ── GitHub Handler ─────────────────────────────────────────────────────────

_GITHUB_DOMAINS = {"github.com", "raw.githubusercontent.com",
                   "gist.github.com", "gist.githubusercontent.com"}


def _is_github_url(domain):
    """Check if a domain is a GitHub domain."""
    if not domain:
        return False
    parts = domain.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in _GITHUB_DOMAINS:
            return True
    return False


def _fetch_via_github(url):
    """Extract content from GitHub URLs using raw/API strategy.

    Handles:
      raw.githubusercontent.com / gist.githubusercontent.com
        → Returns None (falls through to chained fallback tier 1 direct fetch)

      github.com/user/repo                      → README via raw.githubusercontent.com
      github.com/user/repo/blob/branch/path     → raw file
      github.com/user/repo/tree/branch/path     → directory listing (GitHub API)
      github.com/user/repo/issues/N             → issue body + comments (GitHub API)
      github.com/user/repo/pull/N               → PR body + comments (GitHub API)
      github.com/user/repo/wiki[/Page]          → wiki page (raw.githubusercontent.com/wiki)
      gist.github.com/user/hash                 → raw gist content

    All results are marked skip_summary=True to bypass the AI summarizer
    (GitHub content is already clean markdown/code, no LLM needed).
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    # raw / gist content URLs → direct fetch in tier 1 (already works)
    if domain in ("raw.githubusercontent.com", "gist.githubusercontent.com"):
        return None

    # ── Gist URLs: gist.github.com/user/hash ──
    if domain == "gist.github.com":
        match = re.match(r"^/([^/]+)/([^/]+)", path)
        if not match:
            return None
        return _github_fetch_gist(match.group(1), match.group(2), url)

    # ── Standard github.com: /user/repo/rest ──
    match = re.match(r"^/([^/]+)/([^/]+)(/.*)?$", path)
    if not match:
        return None
    user = match.group(1)
    repo = match.group(2)
    rest = (match.group(3) or "").lstrip("/")

    if not rest:
        return _github_fetch_readme(user, repo, url)
    if rest.startswith("blob/"):
        parts = rest.split("/", 2)
        if len(parts) >= 3:
            return _github_fetch_raw_file(user, repo, parts[1], parts[2], url)
    if rest.startswith("tree/"):
        parts = rest.split("/", 2)
        dirpath = parts[2] if len(parts) >= 3 else ""
        return _github_fetch_tree(user, repo, parts[1], dirpath, url)
    issue_match = re.match(r"^(issues|pull)/(\d+)/?$", rest)
    if issue_match:
        return _github_fetch_issue(user, repo, issue_match.group(1),
                                    issue_match.group(2), url)
    if rest.startswith("wiki"):
        return _github_fetch_wiki(user, repo, rest, url)

    return None


def _github_raw_fetch(raw_url, original_url):
    """Fetch raw content from a URL, returning result keyed to original_url."""
    try:
        req = urllib.request.Request(
            raw_url, method="GET",
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                              " (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                "Accept": "text/html,text/plain,*/*",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        content = resp.read().decode("utf-8", errors="replace")
        if len(content.strip()) < 20:
            return None
        if len(content) > MAX_CONTENT_LENGTH:
            content = content[:MAX_CONTENT_LENGTH] + "\n\n[... content truncated ...]"
        return {
            "url": original_url,
            "title": _extract_title_from_url(original_url),
            "raw_content": content,
            "source": "github-raw",
            "skip_summary": True,
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        log.info("github raw HTTP error for %s: %s", raw_url, e)
        return None
    except Exception as e:
        log.info("github raw fetch failed for %s: %s", raw_url, e)
        return None


def _github_fetch_readme(user, repo, original_url):
    """Fetch README.md from repo root (tries main → master)."""
    for branch in ("main", "master"):
        raw_url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/README.md"
        result = _github_raw_fetch(raw_url, original_url)
        if result:
            result["source"] = "github-readme"
            result["title"] = f"{user}/{repo}"
            return result
    return None


def _github_fetch_raw_file(user, repo, branch, filepath, original_url):
    """Fetch a file via raw.githubusercontent.com."""
    raw_url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{filepath}"
    return _github_raw_fetch(raw_url, original_url)


def _github_fetch_tree(user, repo, branch, dirpath, original_url):
    """Fetch directory listing via GitHub API."""
    api_url = f"https://api.github.com/repos/{user}/{repo}/contents/{dirpath}?ref={branch}"
    try:
        req = urllib.request.Request(
            api_url, method="GET",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "tavily-shim/2.0",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        if isinstance(data, dict):
            download_url = data.get("download_url")
            if download_url:
                return _github_raw_fetch(download_url, original_url)
            return _github_fetch_raw_file(user, repo, branch, dirpath, original_url)

        lines = [f"# {user}/{repo}/{dirpath}"]
        if isinstance(data, list):
            for item in data:
                name = item.get("name", "")
                icon = "📁" if item.get("type") == "dir" else "📄"
                lines.append(f"- {icon} {name}")
        return {
            "url": original_url,
            "title": f"{user}/{repo}/{dirpath}",
            "raw_content": "\n".join(lines),
            "source": "github-api-tree",
            "skip_summary": True,
        }
    except urllib.error.HTTPError as e:
        if e.code == 403:
            log.warning("github API rate-limited for tree fetch")
            return None
        log.info("github API tree fetch failed: %s", e)
        return None
    except Exception as e:
        log.info("github API tree fetch failed: %s", e)
        return None


def _github_fetch_issue(user, repo, issue_type, issue_num, original_url):
    """Fetch issue/PR body + comments via GitHub API."""
    api_url = f"https://api.github.com/repos/{user}/{repo}/{issue_type}/{issue_num}"
    try:
        req = urllib.request.Request(
            api_url, method="GET",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "tavily-shim/2.0",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        title = data.get("title", "")
        body = data.get("body") or ""
        state = data.get("state", "")
        author = data.get("user", {}).get("login", "") if data.get("user") else ""
        labels = [l.get("name", "") for l in data.get("labels", [])]

        lines = [
            f"# {title}",
            f"**{issue_type[:-1].title()} #{issue_num}** · {state} · by @{author}",
        ]
        if labels:
            lines.append(f"Labels: {', '.join(labels)}")
        lines.append("")
        lines.append(body if body else "*(No description)*")

        # Fetch top comments
        comments_url = data.get("comments_url", "")
        if comments_url:
            try:
                req2 = urllib.request.Request(
                    comments_url,
                    headers={
                        "Accept": "application/vnd.github.v3+json",
                        "User-Agent": "tavily-shim/2.0",
                    },
                )
                comments = json.loads(urllib.request.urlopen(req2, timeout=10).read())
                if comments:
                    lines.append("")
                    lines.append(f"## Comments ({len(comments)})")
                    for c in comments[:20]:
                        c_author = c.get("user", {}).get("login", "unknown")
                        c_body = (c.get("body") or "")[:2000]
                        lines.append("")
                        lines.append(f"**@{c_author}**")
                        lines.append(c_body)
                    if len(comments) > 20:
                        lines.append("")
                        lines.append(f"*... and {len(comments) - 20} more comments*")
            except Exception:
                pass

        return {
            "url": original_url,
            "title": title,
            "raw_content": "\n".join(lines),
            "source": f"github-api-{issue_type}",
            "skip_summary": True,
        }
    except urllib.error.HTTPError as e:
        if e.code == 403:
            log.warning("github API rate-limited for issue/PR fetch")
            return None
        log.info("github API issue fetch failed: %s", e)
        return None
    except Exception as e:
        log.info("github API issue fetch failed: %s", e)
        return None


def _github_fetch_wiki(user, repo, rest, original_url):
    """Fetch wiki page via raw.githubusercontent.com/wiki."""
    wiki_path = rest.replace("wiki", "", 1).lstrip("/")
    if wiki_path:
        raw_url = f"https://raw.githubusercontent.com/wiki/{user}/{repo}/{wiki_path}.md"
        result = _github_raw_fetch(raw_url, original_url)
        if result:
            result["source"] = "github-wiki"
            return result
    raw_url = f"https://raw.githubusercontent.com/wiki/{user}/{repo}/Home.md"
    result = _github_raw_fetch(raw_url, original_url)
    if result:
        result["source"] = "github-wiki"
    return result


def _github_fetch_gist(user, gist_hash, original_url):
    """Fetch gist content via gist.githubusercontent.com."""
    raw_url = f"https://gist.githubusercontent.com/{user}/{gist_hash}/raw"
    return _github_raw_fetch(raw_url, original_url)


def _fetch_reddit_custom(url):
    """Fetch Reddit content using the Hermes reddit-extract plugin approach.

    Data sources (tried in order):
    1. Arctic Shift  — free API, best coverage, recent data
    2. PullPush      — free mirror, older data only
    3. Camoufox      — browser fallback (uses our proxy if configured)

    Returns formatted Markdown content.
    """
    import html as _html
    from typing import Any, Dict, List, Optional

    ARCTIC_SHIFT_BASE = "https://arctic-shift.photon-reddit.com"
    PULLPUSH_API_BASE = "https://api.pullpush.io/reddit"
    REDDIT_MAX_COMMENTS = 30
    TIMEOUT = 30

    def _fetch_json(url_s: str) -> Optional[Any]:
        req = urllib.request.Request(
            url_s,
            headers={
                "Accept": "application/json",
                "User-Agent": "tavily-shim-reddit/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def _dehtml(text: str) -> str:
        return _html.unescape(text).strip()

    def _squeeze_lines(text: str) -> str:
        return re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()

    def _read_path(obj: Any, *path: str) -> Any:
        cur = obj
        for key in path:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        return cur

    def _extract_post_id(pathname: str) -> Optional[str]:
        m = re.search(r"/comments/([a-z0-9]+)\b", pathname, re.IGNORECASE)
        return m.group(1).lower() if m else None

    def _extract_subreddit_name(pathname: str) -> Optional[str]:
        m = re.match(r"^/r/([A-Za-z0-9_]+)\b", pathname)
        return m.group(1) if m else None

    # ── Tier 1: Arctic Shift ───────────────────────────────────
    def _arctic_fetch_post(post_id: str) -> Optional[Dict[str, Any]]:
        data = _fetch_json(f"{ARCTIC_SHIFT_BASE}/api/posts/ids?ids={post_id}")
        posts = _read_path(data, "data")
        return posts[0] if isinstance(posts, list) and posts else None

    def _arctic_fetch_comments(post_id: str) -> List[Dict[str, Any]]:
        data = _fetch_json(
            f"{ARCTIC_SHIFT_BASE}/api/comments/search"
            f"?link_id=t3_{post_id}&sort=desc&limit={REDDIT_MAX_COMMENTS}"
        )
        comments = _read_path(data, "data")
        return comments if isinstance(comments, list) else []

    def _arctic_fetch_subreddit(subreddit: str) -> List[Dict[str, Any]]:
        data = _fetch_json(
            f"{ARCTIC_SHIFT_BASE}/api/posts/search"
            f"?subreddit={subreddit}&sort=desc&limit=20"
        )
        posts = _read_path(data, "data")
        return posts if isinstance(posts, list) else []

    # ── Tier 2: PullPush ───────────────────────────────────────
    def _pullpush_fetch_post(post_id: str) -> Optional[Dict[str, Any]]:
        params = urllib.parse.urlencode({"ids": post_id})
        data = _fetch_json(f"{PULLPUSH_API_BASE}/search/submission/?{params}")
        submissions = _read_path(data, "data") or []
        return submissions[0] if isinstance(submissions, list) and submissions else None

    def _pullpush_fetch_comments(post_id: str) -> List[Dict[str, Any]]:
        params = urllib.parse.urlencode({
            "link_id": post_id, "sort": "desc",
            "sort_type": "score", "size": REDDIT_MAX_COMMENTS,
        })
        data = _fetch_json(f"{PULLPUSH_API_BASE}/search/comment/?{params}")
        return _read_path(data, "data") or []

    def _pullpush_fetch_subreddit(subreddit: str) -> List[Dict[str, Any]]:
        params = urllib.parse.urlencode({
            "subreddit": subreddit, "sort": "desc",
            "sort_type": "score", "size": 20,
        })
        data = _fetch_json(f"{PULLPUSH_API_BASE}/search/submission/?{params}")
        return _read_path(data, "data") or []

    # ── Formatting ─────────────────────────────────────────────
    def _format_post(submission: Dict[str, Any],
                     comments: List[Dict[str, Any]],
                     *, source_url: str = "") -> str:
        title = _dehtml(str(submission.get("title", "Reddit post")))
        subreddit = str(submission.get("subreddit", "?")).replace("r/", "", 1)
        author = str(submission.get("author", "unknown"))
        score = submission.get("score", 0) or 0
        num_comments = submission.get("num_comments", len(comments)) or len(comments)
        selftext = str(submission.get("selftext", "")).strip()
        if not source_url:
            permalink = str(submission.get("permalink", "")).strip()
            source_url = f"https://www.reddit.com{permalink}" if permalink else (
                f"https://www.reddit.com/comments/{submission.get('id','')}"
                if submission.get("id") else "https://www.reddit.com"
            )
        lines: List[str] = [
            f"# {title}",
            f"r/{subreddit} · by u/{author} · {score} points · {num_comments} comments",
            f"Source: {source_url}",
        ]
        if selftext:
            lines.append("")
            lines.append(_dehtml(selftext))
        if comments:
            lines.append("")
            lines.append("## Top Comments")
            for c in comments:
                body = str(c.get("body", "")).strip()
                if not body:
                    continue
                c_author = str(c.get("author", "unknown"))
                c_score = c.get("score", 0) or 0
                lines.append(f"- **{c_author}** ({c_score}): {_dehtml(body)}")
        return _squeeze_lines("\n".join(lines))

    def _format_subreddit(submissions: List[Dict[str, Any]], subreddit: str) -> str:
        lines: List[str] = [f"# r/{subreddit} — Top Posts", ""]
        for item in submissions[:20]:
            post_title = _dehtml(str(item.get("title", "(untitled)")))
            permalink = str(item.get("permalink", "")).strip()
            post_id = str(item.get("id", "")).strip()
            link = (f"https://www.reddit.com{permalink}" if permalink else
                    f"https://www.reddit.com/comments/{post_id}" if post_id else
                    "https://www.reddit.com")
            sr = str(item.get("subreddit", subreddit)).replace("r/", "", 1)
            score = item.get("score", 0) or 0
            num_comments = item.get("num_comments", 0) or 0
            post_hint = str(item.get("post_hint", "")).strip()
            is_video = item.get("is_video", False)
            gallery = item.get("gallery_data")
            icon = ""
            if gallery and isinstance(gallery, dict) and gallery.get("items"):
                icon = " 🖼️"
            elif is_video or post_hint == "hosted:video":
                icon = " 🎬"
            elif post_hint == "image" or str(item.get("domain", "")) in (
                "i.redd.it", "i.redditmedia.com", "i.imgur.com"
            ):
                icon = " 📷"
            lines.append(
                f"- [{post_title}]({link}) — r/{sr} "
                f"({score} pts, {num_comments} 💬){icon}"
            )
        return _squeeze_lines("\n".join(lines))

    # ── Main logic ─────────────────────────────────────────────
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    hostname = (parsed.hostname or "").lower().replace("www.", "").replace("old.", "", 1)
    if hostname != "reddit.com":
        return None

    pathname = parsed.path
    post_id = _extract_post_id(pathname)
    subreddit = _extract_subreddit_name(pathname) if not post_id else None

    # Try Tier 1: Arctic Shift
    if post_id:
        submission = _arctic_fetch_post(post_id)
        if submission:
            comments = _arctic_fetch_comments(post_id)
            permalink = str(submission.get("permalink", "")).strip()
            source_url = f"https://www.reddit.com{permalink}" if permalink else url
            return {
                "url": url,
                "title": _dehtml(str(submission.get("title", "Reddit post"))),
                "raw_content": _format_post(submission, comments, source_url=source_url),
                "source": "reddit-arctic-shift",
            }

    if subreddit:
        submissions = _arctic_fetch_subreddit(subreddit)
        if submissions:
            title = _dehtml(str(submissions[0].get("subreddit", subreddit)))
            return {
                "url": url,
                "title": f"r/{title}",
                "raw_content": _format_subreddit(submissions, subreddit),
                "source": "reddit-arctic-shift",
            }

    # Try Tier 2: PullPush
    if post_id:
        submission = _pullpush_fetch_post(post_id)
        if submission:
            comments = _pullpush_fetch_comments(post_id)
            return {
                "url": url,
                "title": _dehtml(str(submission.get("title", "Reddit post"))),
                "raw_content": _format_post(submission, comments),
                "source": "reddit-pullpush",
            }

    if subreddit:
        submissions = _pullpush_fetch_subreddit(subreddit)
        if submissions:
            title = _dehtml(str(submissions[0].get("subreddit", subreddit)))
            return {
                "url": url,
                "title": f"r/{title}",
                "raw_content": _format_subreddit(submissions, subreddit),
                "source": "reddit-pullpush",
            }

    # Fallback: Camoufox browser
    log.info("reddit API sources failed, fallback to Camoufox: %s", url)
    return _extract_via_camoufox(url, include_images=False)


def _is_file_url(url):
    """Detect if a URL likely points to a downloadable file."""
    file_extensions = {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".toml",
        ".md", ".rst", ".txt", ".log", ".conf", ".cfg", ".ini",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
        ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac", ".ogg",
        ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".h",
        ".sql", ".db", ".sqlite",
    }
    path = urlparse(url).path.lower()
    for ext in file_extensions:
        if path.endswith(ext):
            return True
    return False


def _fetch_via_markdown_new(url):
    """Fetch page content via markdown.new API.

    Returns dict with url, title, raw_content or None on failure.
    """
    md_url = f"https://markdown.new/{url}"
    try:
        req = urllib.request.Request(md_url, method="GET",
                                      headers={"User-Agent": "tavily-shim/2.0"})
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read().decode("utf-8", errors="replace")

        # Check for JSON error response
        if raw.startswith("{"):
            try:
                err_data = json.loads(raw)
                if not err_data.get("success", True):
                    log.info("markdown.new error: %s", err_data.get("error", "unknown"))
                    return None
            except json.JSONDecodeError:
                pass

        # Parse markdown.new response format:
        # Title: ...
        # URL Source: ...
        # Markdown Content:
        # <actual content>
        title = ""
        content = raw

        # Extract title from first line
        title_match = re.search(r"^Title:\s*(.+)$", raw, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()

        # Extract content after "Markdown Content:" marker
        content_match = re.split(r"^Markdown Content:\s*$", raw, flags=re.MULTILINE)
        if len(content_match) > 1:
            content = content_match[1].strip()

        # Truncate if too large
        if len(content) > MAX_CONTENT_LENGTH:
            content = content[:MAX_CONTENT_LENGTH] + "\n\n[... content truncated ...]"

        # Detect empty/meaningless responses (markdown.new returns non-JSON but
        # essentially empty content for blocked pages). If the markdown body is
        # just "# " or a few chars of navigation boilerplate, treat as failure.
        body_stripped = content.strip().strip("#").strip()
        if len(body_stripped) < 50:
            log.info("markdown.new returned empty content (%d chars) for %s, will fallback", len(content), url)
            return None

        return {
            "url": url,
            "title": title or _extract_title_from_url(url),
            "raw_content": content,
            "source": "markdown.new",
        }
    except Exception as e:
        log.info("markdown.new fetch failed for %s: %s", url, e)
        return None


def _fetch_via_markdown_file(url):
    """Fetch file content via markdown.new/file-to-markdown.

    Returns dict with url, title, raw_content or None on failure.
    """
    md_url = f"https://markdown.new/file-to-markdown/{url}"
    try:
        req = urllib.request.Request(md_url, method="GET",
                                      headers={"User-Agent": "tavily-shim/2.0"})
        resp = urllib.request.urlopen(req, timeout=60)
        raw = resp.read().decode("utf-8", errors="replace")

        # Check for JSON error response
        if raw.startswith("{"):
            try:
                err_data = json.loads(raw)
                if not err_data.get("success", True):
                    log.info("markdown.new/file error: %s", err_data.get("error", "unknown"))
                    return None
            except json.JSONDecodeError:
                pass

        # Parse same format as markdown.new
        title = ""
        content = raw
        title_match = re.search(r"^Title:\s*(.+)$", raw, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()
        content_match = re.split(r"^Markdown Content:\s*$", raw, flags=re.MULTILINE)
        if len(content_match) > 1:
            content = content_match[1].strip()

        if len(content) > MAX_CONTENT_LENGTH:
            content = content[:MAX_CONTENT_LENGTH] + "\n\n[... content truncated ...]"

        return {
            "url": url,
            "title": title or _extract_title_from_url(url),
            "raw_content": content,
            "source": "markdown.new/file-to-markdown",
        }
    except Exception as e:
        log.info("markdown.new/file fetch failed for %s: %s", url, e)
        return None


def _fetch_direct(url):
    """Fetch page content via direct HTTP GET with browser-like User-Agent.

    First tier in the fallback chain — fastest, free, works for static pages.
    Strips HTML tags, extracts content. Returns None on failure or if content
    is too short (< 100 chars — likely a captcha/block page).
    """
    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                              " (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        )
        # Do NOT follow redirects that change protocol (http→https) ourselves;
        # urllib handles redirects, but set to cap at reasonable depth
        resp = urllib.request.urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8", errors="replace")

        # Extract title
        title = ""
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', raw, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = unescape(title_match.group(1).strip())

        # Strip script/style blocks, then strip HTML tags
        content = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<[^>]+>', ' ', content)
        content = re.sub(r'\s+', ' ', content).strip()

        # Too little content → likely blocked / captcha
        if len(content) < 100:
            log.info("direct fetch returned too little content (%d chars) for %s", len(content), url)
            return None

        if len(content) > MAX_CONTENT_LENGTH:
            content = content[:MAX_CONTENT_LENGTH] + "\n\n[... content truncated ...]"

        return {
            "url": url,
            "title": title or _extract_title_from_url(url),
            "raw_content": content,
            "source": "direct-fetch",
        }
    except Exception as e:
        log.info("direct fetch failed for %s: %s", url, e)
        return None


def _fetch_via_jina(url):
    """Fetch page content via r.jina.ai Reader API (free, no auth needed).

    Jina Reader renders JS and returns clean markdown. Response format is
    nearly identical to markdown.new: Title: / URL Source: / Markdown Content:
    markers. Returns None on failure or empty content.
    """
    jina_url = f"https://r.jina.ai/{url}"
    try:
        req = urllib.request.Request(
            jina_url,
            method="GET",
            headers={
                "User-Agent": "tavily-shim/2.0",
                "Accept": "text/plain,text/markdown,*/*",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read().decode("utf-8", errors="replace")

        # Parse format: Title: ... | URL Source: ... | Markdown Content: ...
        title = ""
        title_match = re.search(r"^Title:\s*(.+)$", raw, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()

        # Content after "Markdown Content:" marker (same as markdown.new)
        content_match = re.split(r"^Markdown Content:\s*$", raw, flags=re.MULTILINE)
        content = content_match[1].strip() if len(content_match) > 1 else raw

        if len(content) > MAX_CONTENT_LENGTH:
            content = content[:MAX_CONTENT_LENGTH] + "\n\n[... content truncated ...]"

        # Empty / too-short content
        if len(content.strip()) < 50:
            log.info("jina reader returned empty content for %s", url)
            return None

        return {
            "url": url,
            "title": title or _extract_title_from_url(url),
            "raw_content": content,
            "source": "r.jina.ai",
        }
    except Exception as e:
        log.info("jina reader fetch failed for %s: %s", url, e)
        return None


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

    # Sort by score descending
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

        # Wait for page to render (longer for JS-heavy sites)
        time.sleep(3)

        # Get snapshot
        snap_req = urllib.request.Request(
            f"{CAMOFOX_URL}/tabs/{tab_id}/snapshot?userId=tavily-shim",
            method="GET",
        )
        snap_resp = urllib.request.urlopen(snap_req, timeout=15)
        snap_data = json.loads(snap_resp.read())

        snapshot_text = snap_data.get("snapshot", "")
        title = snap_data.get("title", "")

        # Extract title from snapshot heading if available
        if not title and snapshot_text:
            title_match = re.search(r'heading\s+"([^"]+)"', snapshot_text)
            if title_match:
                title = title_match.group(1)

        if not title:
            title = _extract_title_from_url(url)

        return {
            "url": url,
            "title": title,
            "raw_content": snapshot_text,
            "source": "camoufox",
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
    if engine in ("google", "google failover"):
        base += 0.25
    elif engine in ("bing", "startpage", "brave"):
        base += 0.15
    elif engine in ("duckduckgo", "qwant"):
        base += 0.05
    position = item.get("position", 50)
    position_score = max(0, 1.0 - position / 50) * 0.2
    return round(min(base + position_score, 1.0), 4)


def _clean_content(text):
    """Clean up content text."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _extract_title_from_url(url):
    """Extract a readable title from URL path."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return parsed.netloc
    # Get last meaningful segment
    segments = [s for s in path.split("/") if s and not s.startswith("?")]
    title = segments[-1] if segments else parsed.netloc
    # Decode URL encoding
    title = urllib.parse.unquote(title.replace("-", " ").replace("_", " "))
    return title[:100]


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if not TAVILY_API_KEY:
        log.warning("TAVILY_API_KEY not set — authentication disabled!")

    log.info("starting tavily-shim 2.0 on http://%s:%d", HOST, PORT)
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
