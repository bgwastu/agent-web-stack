#!/usr/bin/env python3
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOG_LEVEL = os.environ.get("CAMOFOX_FIRECRAWL_SHIM_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("camofox_firecrawl_shim")

CAMOFOX_URL = os.environ.get("CAMOFOX_URL", "http://127.0.0.1:9377").rstrip("/")
CAMOFOX_ACCESS_KEY = os.environ.get("CAMOFOX_ACCESS_KEY", "").strip()
HOST = os.environ.get("CAMOFOX_FIRECRAWL_SHIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CAMOFOX_FIRECRAWL_SHIM_PORT", "33879"))
REQUEST_TIMEOUT = float(os.environ.get("CAMOFOX_FIRECRAWL_SHIM_TIMEOUT", "60"))
WAIT_TIMEOUT_MS = int(os.environ.get("CAMOFOX_FIRECRAWL_SHIM_WAIT_TIMEOUT_MS", "12000"))
CRAWL_WAIT_TIMEOUT_MS = int(os.environ.get("CAMOFOX_FIRECRAWL_CRAWL_WAIT_TIMEOUT_MS", "4000"))
CRAWL_TIME_BUDGET_SECONDS = float(os.environ.get("CAMOFOX_FIRECRAWL_CRAWL_TIME_BUDGET_SECONDS", "75"))
TRACE_ENABLED = os.environ.get("CAMOFOX_FIRECRAWL_TRACE", "").lower() in {"1", "true", "yes", "on"}
USER_ID_PREFIX = os.environ.get("CAMOFOX_FIRECRAWL_SHIM_USER_PREFIX", "firecrawl-shim")
FIXED_USER_ID = os.environ.get("CAMOFOX_FIRECRAWL_FIXED_USER_ID", "").strip()
SESSION_PREFIX = os.environ.get("CAMOFOX_FIRECRAWL_SHIM_SESSION_PREFIX", "scrape")
SEARCH_ENGINE_URL = os.environ.get(
    "CAMOFOX_FIRECRAWL_SEARCH_URL",
    "https://html.duckduckgo.com/html/?q={query}",
)
SEARCH_MACRO = os.environ.get("CAMOFOX_FIRECRAWL_SEARCH_MACRO", "").strip()
PANDOC_BIN = os.environ.get(
    "CAMOFOX_FIRECRAWL_PANDOC_BIN",
    "/opt/hermes-runtime/tools/mise/use-mise.sh",
)
PANDOC_TIMEOUT = float(os.environ.get("CAMOFOX_FIRECRAWL_PANDOC_TIMEOUT", "30"))

CAMOFOX_SERVER_DIR = os.environ.get("CAMOFOX_SERVER_DIR", "/opt/hermes-runtime/camofox-browser")
CAMOFOX_SERVER_COMMAND = os.environ.get("CAMOFOX_SERVER_COMMAND", "node server.js")
CAMOFOX_SERVER_START_TIMEOUT = float(os.environ.get("CAMOFOX_SERVER_START_TIMEOUT", "45"))
CAMOFOX_SERVER_LOG = os.environ.get(
    "CAMOFOX_SERVER_LOG",
    "/opt/hermes-runtime/camofox-firecrawl-shim/camofox-server.log",
)
CAMOFOX_SERVER_ERROR_LOG = os.environ.get(
    "CAMOFOX_SERVER_ERROR_LOG",
    "/opt/hermes-runtime/camofox-firecrawl-shim/camofox-server.err.log",
)
CAMOFOX_SERVER_LD_LIBRARY_PATH = os.environ.get(
    "CAMOFOX_SERVER_LD_LIBRARY_PATH",
    "/opt/hermes-runtime/camofox-deps/root/usr/lib/x86_64-linux-gnu",
)


class SimpleTextExtractor(HTMLParser):
    SKIP_TAGS = {"head", "script", "style", "noscript"}
    BLOCK_TAGS = {
        "article", "aside", "blockquote", "br", "div", "dl", "fieldset", "figcaption",
        "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody",
        "td", "th", "thead", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not data:
            return
        cleaned = re.sub(r"\s+", " ", data)
        if cleaned.strip():
            self.parts.append(cleaned)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = dict(attrs)
        href = (attr_map.get("href") or "").strip()
        if href:
            self._current_href = href
            self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None and data:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current_href is None:
            return
        text = normalize_text("".join(self._current_text_parts))
        self.links.append({"href": self._current_href, "text": text})
        self._current_href = None
        self._current_text_parts = []


@dataclass(slots=True)
class CrawlJobRecord:
    id: str
    url: str
    status: str
    completed: int
    total: int
    data: list[dict[str, Any]]
    created_at: str
    expires_at: str
    error: str | None = None


CRAWL_JOBS: dict[str, CrawlJobRecord] = {}
CRAWL_LOCK = threading.Lock()


def normalize_text(text: str) -> str:
    text = unescape(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract_text_from_html(html: str) -> str:
    parser = SimpleTextExtractor()
    try:
        parser.feed(html)
        parser.close()
        return parser.get_text()
    except Exception:
        return ""


def strip_pandoc_artifacts(markdown: str) -> str:
    text = markdown.strip()
    text = re.sub(r"^<div>\s*", "", text)
    text = re.sub(r"\s*</div>$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_markdown(html: str, title: str, fallback_text: str) -> str:
    if html.strip():
        cmd = [PANDOC_BIN, "pandoc", "-f", "html-native_divs-native_spans", "-t", "gfm", "--wrap=none"]
        try:
            result = subprocess.run(
                cmd,
                input=html,
                text=True,
                capture_output=True,
                timeout=PANDOC_TIMEOUT,
                check=True,
            )
            markdown = strip_pandoc_artifacts(result.stdout)
            if markdown:
                return markdown
        except Exception as exc:
            logger.warning("pandoc markdown conversion failed, falling back to text extraction: %s", exc)

    text = normalize_text(fallback_text) or extract_text_from_html(html)
    if title and text:
        heading = f"# {title}"
        if not text.startswith(heading):
            if text.startswith(title):
                text = text[len(title):].strip()
            return f"{heading}\n\n{text}".strip()
    if title and not text:
        return f"# {title}"
    return text.strip()


def build_eval_expression() -> str:
    return r'''(() => {
      const metaBy = (attr, value) => {
        const el = document.querySelector(`meta[${attr}="${value}"]`);
        return el ? el.getAttribute('content') : null;
      };
      const abs = (value) => {
        if (!value) return null;
        try { return new URL(value, location.href).href; } catch { return value; }
      };
      return {
        title: document.title || '',
        url: location.href,
        html: document.documentElement ? document.documentElement.outerHTML : '',
        text: document.body ? document.body.innerText || '' : '',
        metadata: {
          title: document.title || '',
          description: metaBy('name', 'description') || metaBy('property', 'og:description') || '',
          language: document.documentElement ? document.documentElement.lang || '' : '',
          favicon: abs((document.querySelector('link[rel~="icon"]') || {}).href || ''),
          robots: metaBy('name', 'robots') || '',
          keywords: metaBy('name', 'keywords') || '',
          ogTitle: metaBy('property', 'og:title') || '',
          ogDescription: metaBy('property', 'og:description') || '',
          ogUrl: metaBy('property', 'og:url') || '',
          ogImage: abs(metaBy('property', 'og:image') || ''),
          publishedTime: metaBy('property', 'article:published_time') || '',
          modifiedTime: metaBy('property', 'article:modified_time') || '',
        }
      };
    })()'''


def build_search_expression(limit: int) -> str:
    safe_limit = max(1, min(limit, 20))
    return rf'''(() => {{
      const items = Array.from(document.querySelectorAll('.result')).slice(0, {safe_limit * 3});
      return items.map((item, index) => {{
        const link = item.querySelector('a.result__a, a[data-testid="result-title-a"]');
        const snippet = item.querySelector('.result__snippet, .result-snippet, [data-result="snippet"]');
        const href = link ? (link.href || '') : '';
        return {{
          position: index + 1,
          title: link ? (link.textContent || '').trim() : '',
          url: href,
          description: snippet ? (snippet.textContent || '').trim() : '',
        }};
      }}).filter(item => item.title && item.url && !item.url.includes('duckduckgo.com/y.js')).slice(0, {safe_limit});
    }})()'''


def parse_snapshot_results(snapshot_text: str, limit: int) -> list[dict[str, Any]]:
    """Parse Google search results from a Camofox accessibility snapshot.

    Snapshot format::
        - link "TITLE" [eN]:
          - /url: https://...
          - cite: ...
          - text: DESCRIPTION
    """
    import re
    results: list[dict[str, Any]] = []
    lines = snapshot_text.split("\n")
    i = 0
    while i < len(lines) and len(results) < limit:
        line = lines[i]
        # Match: "- link "TITLE" [eN]:" or "  - link "TITLE" [eN]:"
        m = re.match(r'^[ ]*- link "([^"]*)"\s*\[e\d+\]\s*:\s*$', line)
        if not m:
            i += 1
            continue
        title = m.group(1)
        url = ""
        description = ""
        j = i + 1
        while j < len(lines) and lines[j].startswith("  ") and not re.match(r'^[ ]*- (link|heading|button|text)', lines[j]):
            # Check for /url: line
            url_m = re.match(r'^[ ]{2,}- /url:\s*(.+)$', lines[j])
            if url_m:
                url = url_m.group(1).strip()
            # Check for text: line (description)
            text_m = re.match(r'^[ ]{2,}- text:\s*(.+)$', lines[j])
            if text_m:
                desc = text_m.group(1).strip()
                if description:
                    description += " " + desc
                else:
                    description = desc
            j += 1
        # Skip google-internal links and navigation
        if url and title:
            parsed_url = urllib.parse.urlparse(url)
            host = parsed_url.netloc.lower()
            # Skip google subdomains (maps, books, flights, etc.) but keep main search results
            skip_domains = {"google.com", "www.google.com"}
            is_google = host in skip_domains or host.endswith(".google.com")
            if is_google:
                i = j
                continue
            results.append({
                "position": len(results) + 1,
                "title": title,
                "url": url,
                "description": description,
            })
        i = j
    return results[:limit]


def extract_links_from_html(base_url: str, html: str) -> list[dict[str, str]]:
    parser = LinkExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []

    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parser.links:
        href = item.get("href") or ""
        absolute = urllib.parse.urljoin(base_url, href)
        normalized = normalize_url(absolute)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        links.append({
            "url": normalized,
            "title": item.get("text") or "",
            "description": "",
        })
    return links


def normalize_url(url: str, ignore_query_parameters: bool = False) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return ""
    fragmentless = parsed._replace(fragment="")
    if ignore_query_parameters:
        fragmentless = fragmentless._replace(query="")
    return urllib.parse.urlunparse(fragmentless)


def same_origin(url: str, root_url: str, allow_subdomains: bool, allow_external_links: bool) -> bool:
    if allow_external_links:
        return True
    target = urllib.parse.urlparse(url)
    root = urllib.parse.urlparse(root_url)
    if not target.netloc or not root.netloc:
        return False
    if target.netloc == root.netloc:
        return True
    if allow_subdomains and target.hostname and root.hostname:
        return target.hostname.endswith(f".{root.hostname}")
    return False


def path_allowed(url: str, include_paths: list[str], exclude_paths: list[str], regex_on_full_url: bool) -> bool:
    subject = url if regex_on_full_url else (urllib.parse.urlparse(url).path or "/")
    if include_paths and not any(re.search(pattern, subject) for pattern in include_paths):
        return False
    if exclude_paths and any(re.search(pattern, subject) for pattern in exclude_paths):
        return False
    return True


def make_iso_timestamp(offset_seconds: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds))


def make_crawl_status_payload(record: CrawlJobRecord) -> dict[str, Any]:
    payload = {
        "success": True,
        "status": record.status,
        "completed": record.completed,
        "total": record.total,
        "creditsUsed": record.completed,
        "expiresAt": record.expires_at,
        "next": None,
        "data": record.data,
    }
    if record.error:
        payload["error"] = record.error
    return payload


def parse_command(command: str) -> list[str]:
    """Parse a configured command without invoking a shell."""
    parts = shlex.split(command)
    if not parts:
        raise ValueError("configured command must not be empty")
    return parts


class CamofoxClient:
    def __init__(self, base_url: str, timeout: float, access_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.access_key = access_key.strip()
        self.eval_expression = build_eval_expression()
        self._launch_lock = threading.Lock()
        self._launched_process: subprocess.Popen[str] | None = None

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = None
        headers = {"Accept": "application/json"}
        if self.access_key:
            headers["Authorization"] = f"Bearer {self.access_key}"
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, headers=headers, data=body)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"camofox {method} {path} failed: HTTP {exc.code}: {body_text[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"camofox {method} {path} failed: {exc}") from exc

    def wait_for_health(self) -> dict[str, Any]:
        return self.request("GET", "/health")

    def ensure_available(self) -> dict[str, Any]:
        try:
            health = self.wait_for_health()
            if health.get("ok"):
                return health
        except Exception as first_error:
            logger.info("camofox unavailable, attempting lazy start: %s", first_error)
        self._start_server_if_needed()
        return self.wait_for_health()

    def _start_server_if_needed(self) -> None:
        with self._launch_lock:
            if self._process_is_live(self._launched_process):
                logger.info("camofox start skipped; launched child already running")
            else:
                self._launched_process = self._launch_server_process()
            self._wait_for_server_ready()

    def _launch_server_process(self) -> subprocess.Popen[str]:
        env = os.environ.copy()
        existing_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = self._join_path(CAMOFOX_SERVER_LD_LIBRARY_PATH, existing_ld)
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.port:
            env["CAMOFOX_PORT"] = str(parsed.port)
        os.makedirs(os.path.dirname(CAMOFOX_SERVER_LOG), exist_ok=True)
        stdout_handle = open(CAMOFOX_SERVER_LOG, "a", encoding="utf-8")
        stderr_handle = open(CAMOFOX_SERVER_ERROR_LOG, "a", encoding="utf-8")
        logger.info("starting camofox server in %s with command: %s", CAMOFOX_SERVER_DIR, CAMOFOX_SERVER_COMMAND)
        return subprocess.Popen(
            parse_command(CAMOFOX_SERVER_COMMAND),
            cwd=CAMOFOX_SERVER_DIR,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            start_new_session=True,
        )

    def _wait_for_server_ready(self) -> None:
        deadline = time.time() + CAMOFOX_SERVER_START_TIMEOUT
        last_error: Exception | None = None
        while time.time() < deadline:
            if self._launched_process and self._launched_process.poll() is not None:
                raise RuntimeError(
                    f"camofox server exited during startup with code {self._launched_process.returncode}. "
                    f"See {CAMOFOX_SERVER_LOG} and {CAMOFOX_SERVER_ERROR_LOG}."
                )
            try:
                health = self.wait_for_health()
                if health.get("ok"):
                    logger.info("camofox server is ready")
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(1)
        raise RuntimeError(
            "timed out waiting for camofox server to become healthy"
            + (f": {last_error}" if last_error else "")
        )

    @staticmethod
    def _process_is_live(process: subprocess.Popen[str] | None) -> bool:
        return process is not None and process.poll() is None

    @staticmethod
    def _join_path(first: str, second: str) -> str:
        if first and second:
            return f"{first}:{second}"
        return first or second

    def scrape(
        self,
        url: str,
        wait_timeout_ms: int | None = None,
        extract_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_available()
        user_id = FIXED_USER_ID or f"{USER_ID_PREFIX}-{uuid.uuid4().hex[:8]}"
        session_key = f"{SESSION_PREFIX}-{uuid.uuid4().hex[:8]}"
        tab_id = None
        timeout_ms = wait_timeout_ms or WAIT_TIMEOUT_MS
        try:
            create_payload: dict[str, Any] = {
                "userId": user_id,
                "sessionKey": session_key,
                "url": url,
            }
            if TRACE_ENABLED:
                create_payload["trace"] = True
            created = self.request("POST", "/tabs", create_payload)
            tab_id = created["tabId"]
            self.request("POST", f"/tabs/{tab_id}/wait", {
                "userId": user_id,
                "timeout": timeout_ms,
                "waitForNetwork": True,
            })
            structured_extract = None
            if extract_schema:
                self.request("GET", f"/tabs/{tab_id}/snapshot?userId={urllib.parse.quote(user_id)}")
                extracted = self.request("POST", f"/tabs/{tab_id}/extract", {
                    "userId": user_id,
                    "schema": extract_schema,
                })
                if not extracted.get("ok"):
                    raise RuntimeError(f"camofox structured extract failed: {extracted}")
                data = extracted.get("data")
                if not isinstance(data, dict):
                    raise RuntimeError("camofox structured extract returned no data object")
                structured_extract = data
            evaluated = self.request("POST", f"/tabs/{tab_id}/evaluate", {
                "userId": user_id,
                "expression": self.eval_expression,
            })
            if not evaluated.get("ok"):
                raise RuntimeError(f"camofox evaluate failed: {evaluated}")
            result = evaluated.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("camofox evaluate returned no result object")
            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            if TRACE_ENABLED:
                metadata = {
                    **metadata,
                    "camofoxTraceEnabled": True,
                    "camofoxTraceUserId": user_id,
                    "camofoxTraceSessionKey": session_key,
                }
                result["metadata"] = metadata
            if structured_extract is not None:
                result["extract"] = structured_extract
            return result
        finally:
            self._cleanup(user_id, tab_id)

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.ensure_available()
        user_id = FIXED_USER_ID or f"{USER_ID_PREFIX}-{uuid.uuid4().hex[:8]}"
        session_key = f"search-{uuid.uuid4().hex[:8]}"
        tab_id = None
        try:
            if SEARCH_MACRO:
                # Macro-based search (Google, etc.) via Camofox macro navigation
                created = self.request("POST", "/tabs", {
                    "userId": user_id,
                    "sessionKey": session_key,
                    "url": "https://example.com",
                })
                tab_id = created["tabId"]
                self.request("POST", f"/tabs/{tab_id}/navigate", {
                    "userId": user_id,
                    "macro": SEARCH_MACRO,
                    "query": query,
                })
                self.request("POST", f"/tabs/{tab_id}/wait", {
                    "userId": user_id,
                    "timeout": WAIT_TIMEOUT_MS,
                    "waitForNetwork": True,
                })
                snap = self.request("GET", f"/tabs/{tab_id}/snapshot?userId={urllib.parse.quote(user_id)}")
                snap_text = snap.get("snapshot", "")
                raw_results = parse_snapshot_results(snap_text, limit)
            else:
                # URL-based search (DuckDuckGo HTML, etc.) via evaluate
                search_url = SEARCH_ENGINE_URL.format(query=urllib.parse.quote_plus(query))
                created = self.request("POST", "/tabs", {
                    "userId": user_id,
                    "sessionKey": session_key,
                    "url": search_url,
                })
                tab_id = created["tabId"]
                self.request("POST", f"/tabs/{tab_id}/wait", {
                    "userId": user_id,
                    "timeout": WAIT_TIMEOUT_MS,
                    "waitForNetwork": True,
                })
                evaluated = self.request("POST", f"/tabs/{tab_id}/evaluate", {
                    "userId": user_id,
                    "expression": build_search_expression(limit),
                })
                if not evaluated.get("ok"):
                    raise RuntimeError(f"camofox search evaluate failed: {evaluated}")
                raw_results = evaluated.get("result")
                if not isinstance(raw_results, list):
                    raise RuntimeError("camofox search returned unexpected result shape")
            normalized = [self._normalize_search_result(item) for item in raw_results if isinstance(item, dict)]
            return [item for item in normalized if item]
        finally:
            self._cleanup(user_id, tab_id)

    def map_url(
        self,
        url: str,
        *,
        limit: int = 5000,
        include_subdomains: bool = False,
        ignore_query_parameters: bool = False,
        search_term: str | None = None,
    ) -> list[dict[str, str]]:
        extracted = self.scrape(url)
        final_url = str(extracted.get("url") or url)
        html = str(extracted.get("html") or "")
        links = extract_links_from_html(final_url, html)
        root = normalize_url(final_url, ignore_query_parameters=ignore_query_parameters)
        filtered: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in links:
            link_url = normalize_url(item.get("url") or "", ignore_query_parameters=ignore_query_parameters)
            if not link_url or link_url in seen:
                continue
            if not same_origin(link_url, root, include_subdomains, allow_external_links=False):
                continue
            haystack = f"{item.get('title', '')} {link_url}".lower()
            if search_term and search_term.lower() not in haystack:
                continue
            seen.add(link_url)
            filtered.append({
                "url": link_url,
                "title": item.get("title") or "",
                "description": item.get("description") or "",
            })
            if len(filtered) >= max(1, limit):
                break
        return filtered

    def crawl_url(self, start_url: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        limit = max(1, int(options.get("limit") or 20))
        max_discovery_depth = max(0, int(options.get("maxDiscoveryDepth") or 2))
        allow_external_links = bool(options.get("allowExternalLinks") or False)
        allow_subdomains = bool(options.get("allowSubdomains") or False)
        ignore_query_parameters = bool(options.get("ignoreQueryParameters") or False)
        include_paths = [str(item) for item in (options.get("includePaths") or []) if str(item)]
        exclude_paths = [str(item) for item in (options.get("excludePaths") or []) if str(item)]
        regex_on_full_url = bool(options.get("regexOnFullURL") or False)
        crawl_started = time.monotonic()

        root_url = normalize_url(start_url, ignore_query_parameters=ignore_query_parameters) or start_url
        queue: list[tuple[str, int]] = [(root_url, 0)]
        seen: set[str] = set()
        collected_urls: set[str] = set()
        documents: list[dict[str, Any]] = []

        while queue and len(documents) < limit:
            if documents and (time.monotonic() - crawl_started) >= CRAWL_TIME_BUDGET_SECONDS:
                logger.info(
                    "crawl time budget reached after %d page(s); returning partial results",
                    len(documents),
                )
                break
            current_url, depth = queue.pop(0)
            normalized_current = normalize_url(current_url, ignore_query_parameters=ignore_query_parameters)
            if not normalized_current or normalized_current in seen:
                continue
            seen.add(normalized_current)
            if not same_origin(normalized_current, root_url, allow_subdomains, allow_external_links):
                continue
            if not path_allowed(normalized_current, include_paths, exclude_paths, regex_on_full_url):
                continue

            try:
                extracted = self.scrape(normalized_current, wait_timeout_ms=CRAWL_WAIT_TIMEOUT_MS)
            except Exception as exc:
                logger.warning("crawl page failed for %s: %s", normalized_current, exc)
                if not documents:
                    raise
                continue
            final_page_url = normalize_url(str(extracted.get("url") or normalized_current), ignore_query_parameters=ignore_query_parameters) or normalized_current
            seen.add(final_page_url)
            if final_page_url in collected_urls:
                continue

            document = make_document(final_page_url, extracted)
            documents.append(document)
            collected_urls.add(final_page_url)

            if depth >= max_discovery_depth or len(documents) >= limit:
                continue

            html = str(extracted.get("html") or "")
            for item in extract_links_from_html(final_page_url, html):
                candidate = normalize_url(item.get("url") or "", ignore_query_parameters=ignore_query_parameters)
                if not candidate or candidate in seen:
                    continue
                if not same_origin(candidate, root_url, allow_subdomains, allow_external_links):
                    continue
                if not path_allowed(candidate, include_paths, exclude_paths, regex_on_full_url):
                    continue
                queue.append((candidate, depth + 1))

        return documents

    def _normalize_search_result(self, result: dict[str, Any]) -> dict[str, Any] | None:
        raw_url = str(result.get("url") or "")
        if not raw_url:
            return None
        parsed = urllib.parse.urlparse(raw_url)
        qs = urllib.parse.parse_qs(parsed.query)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        # DuckDuckGo tracking URLs: resolve uddg or u3 param to actual URL
        final_url = qs.get("uddg", qs.get("u3", [raw_url]))[0]
        final_parsed = urllib.parse.urlparse(final_url)
        if "duckduckgo.com" in final_parsed.netloc.lower() and final_parsed.path.lower().endswith("/y.js"):
            return None
        return {
            "url": final_url,
            "title": str(result.get("title") or ""),
            "description": normalize_text(str(result.get("description") or "")),
            "position": int(result.get("position") or 0) or None,
        }

    def _cleanup(self, user_id: str, tab_id: str | None) -> None:
        if tab_id:
            try:
                self.request("DELETE", f"/tabs/{tab_id}?userId={urllib.parse.quote(user_id)}")
            except Exception as exc:
                logger.warning("failed to close tab %s: %s", tab_id, exc)
        try:
            self.request("DELETE", f"/sessions/{urllib.parse.quote(user_id)}")
        except Exception:
            pass


CLIENT = CamofoxClient(CAMOFOX_URL, REQUEST_TIMEOUT, access_key=CAMOFOX_ACCESS_KEY)


def extract_schema_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("jsonOptions", "json_options", "extract", "extractOptions", "extract_options"):
        options = payload.get(key)
        if isinstance(options, dict) and isinstance(options.get("schema"), dict):
            return options["schema"]
    if isinstance(payload.get("schema"), dict):
        return payload["schema"]
    return None


def make_document(url: str, extracted: dict[str, Any]) -> dict[str, Any]:
    title = str(extracted.get("title") or "")
    final_url = str(extracted.get("url") or url)
    html = str(extracted.get("html") or "")
    text = normalize_text(str(extracted.get("text") or ""))
    metadata = extracted.get("metadata") if isinstance(extracted.get("metadata"), dict) else {}

    markdown = html_to_markdown(html, title, text)
    metadata = {
        **metadata,
        "title": title,
        "sourceURL": final_url,
        "url": final_url,
        "statusCode": 200,
        "contentType": "text/html",
        "proxyUsed": "basic",
    }
    for key in ["keywords", "description", "robots", "ogTitle", "ogDescription", "ogUrl", "ogImage"]:
        if metadata.get(key) == "":
            metadata.pop(key, None)

    document = {
        "markdown": markdown,
        "html": html,
        "rawHtml": html,
        "metadata": metadata,
    }
    if isinstance(extracted.get("extract"), dict):
        document["extract"] = extracted["extract"]
    return document


def start_crawl_job(url: str, options: dict[str, Any]) -> CrawlJobRecord:
    created_at = make_iso_timestamp()
    record = CrawlJobRecord(
        id=f"crawl-{uuid.uuid4().hex[:12]}",
        url=url,
        status="scraping",
        completed=0,
        total=0,
        data=[],
        created_at=created_at,
        expires_at=make_iso_timestamp(24 * 60 * 60),
    )
    with CRAWL_LOCK:
        CRAWL_JOBS[record.id] = record

    worker = threading.Thread(target=_run_crawl_job, args=(record.id, url, dict(options)), daemon=True)
    worker.start()
    return record


def _run_crawl_job(job_id: str, url: str, options: dict[str, Any]) -> None:
    try:
        documents = CLIENT.crawl_url(url, options)
        with CRAWL_LOCK:
            record = CRAWL_JOBS.get(job_id)
            if record is None or record.status == "cancelled":
                return
            record.data = documents
            record.completed = len(documents)
            record.total = len(documents)
            record.status = "completed"
            record.error = None
    except Exception as exc:
        with CRAWL_LOCK:
            record = CRAWL_JOBS.get(job_id)
            if record is None or record.status == "cancelled":
                return
            record.status = "failed"
            record.error = str(exc)
            record.data = []
            record.completed = 0
            record.total = 0


def get_crawl_job(job_id: str) -> CrawlJobRecord | None:
    with CRAWL_LOCK:
        return CRAWL_JOBS.get(job_id)


def cancel_crawl_job(job_id: str) -> CrawlJobRecord | None:
    with CRAWL_LOCK:
        record = CRAWL_JOBS.get(job_id)
        if record is None:
            return None
        record.status = "cancelled"
        return record


def shim_health() -> dict[str, Any]:
    health = CLIENT.ensure_available()
    parsed = urllib.parse.urlparse(CAMOFOX_URL)
    camofox_port = parsed.port or 9377
    return {
        "ok": bool(health.get("ok")),
        "success": bool(health.get("ok")),
        "engine": "camofox",
        "shim": {"host": HOST, "port": PORT},
        "upstream": health,
        "upstream_port_open": is_tcp_port_open(parsed.hostname or "127.0.0.1", camofox_port),
    }


def is_tcp_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "camofox-firecrawl-shim/0.3"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/health", "/v2/health"}:
            try:
                self.send_json(200, shim_health())
            except Exception as exc:
                self.send_json(503, {"ok": False, "success": False, "error": str(exc)})
            return
        if parsed.path.startswith("/v2/crawl/"):
            self.handle_crawl_status(parsed.path.rsplit("/", 1)[-1])
            return
        self.send_json(404, {"success": False, "error": "Not found"})

    def do_POST(self) -> None:
        if self.path == "/v2/scrape":
            self.handle_scrape()
            return
        if self.path == "/v2/search":
            self.handle_search()
            return
        if self.path == "/v2/map":
            self.handle_map()
            return
        if self.path == "/v2/crawl":
            self.handle_crawl_start()
            return
        self.send_json(404, {"success": False, "error": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/v2/crawl/"):
            self.handle_crawl_cancel(parsed.path.rsplit("/", 1)[-1])
            return
        self.send_json(404, {"success": False, "error": "Not found"})

    def handle_scrape(self) -> None:
        try:
            payload = self.read_json()
            url = str(payload.get("url") or "").strip()
            if not url:
                self.send_json(400, {"success": False, "error": "url is required"})
                return
            extract_schema = extract_schema_from_payload(payload)
            extracted = CLIENT.scrape(url, extract_schema=extract_schema)
            document = make_document(url, extracted)
            self.send_json(200, {"success": True, "data": document})
        except Exception as exc:
            logger.exception("scrape failed")
            self.send_json(500, {"success": False, "error": str(exc)})

    def handle_search(self) -> None:
        try:
            payload = self.read_json()
            query = str(payload.get("query") or "").strip()
            if not query:
                self.send_json(400, {"success": False, "error": "query is required"})
                return
            limit = int(payload.get("limit") or 5)
            results = CLIENT.search(query, limit)
            self.send_json(200, {"success": True, "data": {"web": results}})
        except Exception as exc:
            logger.exception("search failed")
            self.send_json(500, {"success": False, "error": str(exc)})

    def handle_map(self) -> None:
        try:
            payload = self.read_json()
            url = str(payload.get("url") or "").strip()
            if not url:
                self.send_json(400, {"success": False, "error": "url is required"})
                return
            links = CLIENT.map_url(
                url,
                limit=int(payload.get("limit") or 5000),
                include_subdomains=bool(payload.get("includeSubdomains") or False),
                ignore_query_parameters=bool(payload.get("ignoreQueryParameters") or False),
                search_term=str(payload.get("search") or "").strip() or None,
            )
            self.send_json(200, {"success": True, "links": links})
        except Exception as exc:
            logger.exception("map failed")
            self.send_json(500, {"success": False, "error": str(exc)})

    def handle_crawl_start(self) -> None:
        try:
            payload = self.read_json()
            url = str(payload.get("url") or "").strip()
            if not url:
                self.send_json(400, {"success": False, "error": "url is required"})
                return
            record = start_crawl_job(url, payload)
            self.send_json(200, {"success": True, "id": record.id, "url": record.url})
        except Exception as exc:
            logger.exception("crawl start failed")
            self.send_json(500, {"success": False, "error": str(exc)})

    def handle_crawl_status(self, job_id: str) -> None:
        record = get_crawl_job(job_id)
        if record is None:
            self.send_json(404, {"success": False, "error": f"crawl job not found: {job_id}"})
            return
        self.send_json(200, make_crawl_status_payload(record))

    def handle_crawl_cancel(self, job_id: str) -> None:
        record = cancel_crawl_job(job_id)
        if record is None:
            self.send_json(404, {"success": False, "error": f"crawl job not found: {job_id}"})
            return
        self.send_json(200, {"success": True, "status": record.status})

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    logger.info("starting shim on http://%s:%s -> %s", HOST, PORT, CAMOFOX_URL)
    server = Server((HOST, PORT), Handler)
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            server.handle_request()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        stop.set()
        try:
            urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=1)
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
