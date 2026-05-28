# Agent Web Stack

Self-hosted Tavily-compatible API for web search and extraction.

```
Search    → SearXNG (Google, Camoufox, Scavio, Tavily → chained fallback)
Extract   → direct HTTP → markdown.new → r.jina.ai → Camoufox → None
GitHub    → raw.githubusercontent.com / GitHub API (skip AI summarization)
Reddit    → Arctic Shift → PullPush → chained extract fallback
Protocol  → REST (Tavily-compatible) + MCP (JSON-RPC 2.0)
Port      → 33879 (tavily-shim), 9377 (Camoufox), 8880 (SearXNG)
```

## Quick Start

```bash
cp .env.example .env
# Edit .env: set TAVILY_API_KEY, SEARXNG_SECRET, and SCAVIO_API_KEY
docker compose up -d
```

## API

**`POST /search`** — Tavily-compatible search

```json
{"api_key":"...","query":"latest AI news","max_results":5}
```

**`POST /extract`** — URL content extraction (5-tier fallback chain)

```json
{"api_key":"...","urls":["https://example.com/page"]}
```

**`POST /mcp`** — MCP JSON-RPC 2.0 endpoint

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

**`GET /health`** — Health check

## Fetch Strategy

| URL Type | Strategy |
|----------|----------|
| Standard web pages | direct HTTP → markdown.new → r.jina.ai → Camoufox → None |
| GitHub | raw.githubusercontent.com / GitHub API (no AI summarizer) |
| Reddit | Arctic Shift / PullPush APIs → chained extract fallback |
| Files (.pdf, .md, etc.) | markdown.new/file-to-markdown → chained extract |

AI summarization (via LLM) runs on large content from unknown domains.
GitHub, raw.githubusercontent.com, gist.githubusercontent.com always skip it.
Add domains to SKIP_SUMMARY_SITES .env var to skip others.

## Search Fallback Chain

```
google → google camofox → scavio → tavily
```

Each engine only fires when the previous one is suspended/blocked —
paid APIs (scavio, tavily) are never called unless Google is down.

## Services

| Service | Role | Port |
|---------|------|------|
| tavily-shim | API server | 33879 |
| camofox-browser | Headless browser | 9377 |
| searxng | Meta-search engine | 8880 |
| searxng-redis | Cache | — |

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `TAVILY_API_KEY` | Yes | Auth key for the shim API |
| `SEARXNG_SECRET` | Yes | SearXNG secret key |
| `SCAVIO_API_KEY` | No | Paid Google Search API fallback |
| `CAMOFOX_ONLY_SITES` | No | Domains that always use Camoufox (default: linkedin,facebook,x,youtube) |
| `SKIP_SUMMARY_SITES` | No | Domains to skip AI summarization |
| `SUMMARIZE_EXTRACT` | No | Enable LLM content cleaner (default: true) |
| `SUMMARIZER_API_KEY` | No | API key for LLM summarizer |
