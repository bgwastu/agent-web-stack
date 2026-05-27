# Agent Web Stack 🕸️

All-in-one self-hosted web infrastructure for AI agents — **Camoufox browser** + **Firecrawl shim** + **SearXNG meta-search**, orchestrated with Docker Compose.

## Architecture

```
AI Agent / Hermes Gateway
    │
    ├── web_search ──► tavily-shim (:33879)
    │                       └── SearXNG (backend)
    │
    ├── web_extract ──► tavily-shim (:33879)
    │                       └── camofox-browser (:9377)
    │
    └── browser tools ──► camofox-browser REST API (:9377)

### Tavily API Compatibility

The `tavily-shim` implements the [Tavily Search API](https://docs.tavily.com/) so any
app or tool built for Tavily can use this self-hosted stack instead:

| Tavily Endpoint | Our Backend | Status |
|----------------|-------------|--------|
| `POST /search` | SearXNG (Google, Bing, Brave, DDG, ...) | ✅ |
| `POST /extract` | Camoufox browser (JS rendering) | ✅ |

### Services

| Service | Port | Role |
|---------|------|------|
| **camofox-browser** | `9377` | Anti-detection headless Firefox (Camoufox). REST API for navigation, snapshots, clicks, typing. |
| **tavily-shim** | `33879` | Tavily-compatible API bridge — routes search to SearXNG, extract to Camoufox. |
| **searxng** | `8880` | Self-hosted meta-search engine (Google, Bing, Brave, DDG, Qwant, Startpage, technical engines). |
| **searxng-redis** | — | Redis for SearXNG rate limiting and result caching. |

## Quick Start

### Prerequisites

- Docker & Docker Compose v2
- Camoufox browser project checked out (for building the browser image)
- A residential HTTP proxy (for captcha-free browsing from datacenter IPs)

### Setup

```bash
# 1. Clone
git clone https://github.com/bgwastu/agent-web-stack.git
cd agent-web-stack

# 2. Configure
cp .env.example .env
# Edit .env with your proxy, secrets, and paths

# 3. Generate SearXNG secret
echo "SEARXNG_SECRET=$(openssl rand -hex 64)" >> .env

# 4. Create external Docker volumes (first time only)
docker volume create searxng_config
docker volume create searxng_cache
docker volume create redis-data

# 5. Build and start
docker compose build
docker compose up -d

# 6. Verify all services healthy
docker compose ps
```

### Verify

```bash
# Camofox browser
curl -s http://localhost:9377/health

# Firecrawl shim
curl -s http://127.0.0.1:33879/health

# SearXNG
curl -s "http://localhost:8880/search?q=test&format=json" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d.get(\"results\",[]))} results')"

# End-to-end browser test
TAB_ID=$(curl -s -X POST http://localhost:9377/tabs \
  -H "Content-Type: application/json" \
  -d '{"userId":"test","sessionKey":"test","url":"https://example.com"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tabId'])")
curl -s "http://localhost:9377/tabs/$TAB_ID/snapshot?userId=test" | head -5
curl -s -X DELETE "http://localhost:9377/tabs/$TAB_ID?userId=test"
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROXY_HOST` | No | `10.0.0.2` | Residential proxy hostname/IP |
| `PROXY_PORT` | No | `8888` | Residential proxy port |
| `CAMOUFOX_VERSION` | No | `135.0.1` | Camoufox binary version |
| `CAMOUFOX_RELEASE` | No | `beta.24` | Camoufox release channel |
| `CAMOFOX_BROWSER_PATH` | No | — | Path to camofox-browser project for Docker build |
| `CAMOFOX_FIRECRAWL_FIXED_USER_ID` | No | `agent` | User ID for session persistence |
| `SEARXNG_SECRET` | **Yes** | — | Generate with `openssl rand -hex 64` |

## Upgrading

### Camoufox Browser (auto-update)

A script checks the latest Camoufox release daily and rebuilds if newer:

```bash
# Manual run
./scripts/check-camoufox-version.sh

# Dry run (no changes)
./scripts/check-camoufox-version.sh --dry-run
```

**Set up daily auto-update via cron:**

```bash
crontab -e
# Add:
0 6 * * * /path/to/agent-web-stack/scripts/check-camoufox-version.sh >> /var/log/camoufox-update.log 2>&1
```

### Full stack upgrade

```bash
git pull
docker compose pull searxng
docker compose build camofox-browser camofox-firecrawl-shim
docker compose up -d
```

### SearXNG

```bash
# Pull latest image
docker compose pull searxng

# Restart
docker compose up -d searxng
```

## Hermes Agent Integration

Add to your Hermes profile `.env`:

```env
CAMOFOX_URL=http://localhost:9377
TAVILY_API_KEY=tavily-shim-dev-key
TAVILY_API_URL=http://127.0.0.1:33879
```

If using the Tavily extract backend:

```yaml
web:
  extract_backend: tavily
```

## Migrating from Systemd / Standalone

### Camofox browser

```bash
# Stop native services
sudo systemctl stop camofox-browser
sudo systemctl disable camofox-browser

# Start Docker stack
docker compose up -d camofox-browser tavily-shim

# After confirming stable, remove old unit files
sudo rm /etc/systemd/system/camofox-browser.service
sudo systemctl daemon-reload
```

### Standalone SearXNG

If you already have a standalone SearXNG Docker Compose:

```bash
# Stop the old stack
docker compose -f /old/path/docker-compose.yml down

# Start from this repo
docker compose up -d searxng searxng-redis
```

Note: the volumes (`searxng_config`, `searxng_cache`, `redis-data`) are marked `external: true` — they persist across stack switches. Your existing SearXNG config and cache carry over automatically.

## Repository Structure

```
agent-web-stack/
├── docker-compose.yml         # All 4 services
├── .env.example               # Configuration template
├── searxng/
│   ├── settings.yml           # SearXNG engine config (version-controlled)
│   └── limiter.toml           # Rate limiting whitelist
├── tavily-shim/
│   ├── Dockerfile
│   ├── shim.py                # Tavily-compatible search + extract API
│   └── requirements.txt
└── README.md
```

## API Reference

### Camofox Browser (`:9377`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/tabs` | Create a new tab |
| `POST` | `/tabs/:id/navigate` | Navigate to URL or search macro |
| `GET` | `/tabs/:id/snapshot` | Accessibility tree with element refs |
| `POST` | `/tabs/:id/click` | Click element by ref |
| `POST` | `/tabs/:id/type` | Type into input |
| `POST` | `/tabs/:id/scroll` | Scroll page |
| `DELETE` | `/tabs/:id` | Close tab |
| `DELETE` | `/sessions/:userId` | Clear session data |

### Tavily Shim (`:33879`)

Full Tavily Search API compatibility:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/search` | Web search (via SearXNG). Params: `query`, `search_depth`, `max_results`, `include_answer`, `include_images` |
| `POST` | `/extract` | URL content extraction (via Camoufox). Params: `urls` (string or array), `include_images` |
| `GET` | `/health` | Health check with upstream status |

See [Tavily API Docs](https://docs.tavily.com/) for full request/response schemas.

### SearXNG (`:8880`)

Standard SearXNG JSON API:
```
GET /search?q=<query>&format=json
```

## License

MIT
