# Agent Web Stack

Self-hosted Tavily-compatible API for web search and extraction.

```
Search    → SearXNG (Google, Bing, Brave, DDG, ...)
Extract   → markdown.new → Camoufox browser (JS-rendered fallback)
Reddit    → Camoufox browser (direct rendering)
Protocol  → REST (Tavily-compatible) + MCP (JSON-RPC 2.0)
Port      → 33879 (tavily-shim), 9377 (Camoufox), 8880 (SearXNG)
```

## Quick Start

```bash
cp .env.example .env
# Edit .env: set TAVILY_API_KEY and SEARXNG_SECRET
docker compose up -d
```

## API

**`POST /search`** — Tavily-compatible search

```json
{"api_key":"...","query":"latest AI news","max_results":5}
```

**`POST /extract`** — URL content extraction (markdown.new → Camoufox)

```json
{"api_key":"...","urls":["https://example.com/page"]}
```

**`POST /mcp`** — MCP JSON-RPC 2.0 endpoint

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

**`GET /health`** — Health check

## Fetch Strategy

| URL Type | Primary | Fallback |
|----------|---------|----------|
| Standard web pages | markdown.new | Camoufox browser |
| Reddit | Camoufox browser (direct) | — |
| Files (.pdf, .md, etc.) | markdown.new/file-to-markdown | Camoufox browser |

## Services

| Service | Role | Port |
|---------|------|------|
| tavily-shim | API server | 33879 |
| camofox-browser | Headless browser | 9377 |
| searxng | Meta-search engine | 8880 |
| searxng-redis | Cache | — |
