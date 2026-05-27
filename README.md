# Agent Web Stack 🕸️

Self-hosted web infrastructure for AI agents — a Camoufox anti-detection browser + Firecrawl-compatible shim, orchestrated with Docker Compose.

## Architecture

```
Hermes Agent / AI tooling
    │
    ├── web_search ──► SearXNG (external)
    │
    ├── web_extract ──► camofox-firecrawl-shim (:33879)
    │                          │
    │                          └──► camofox-browser (:9377)
    │                                    │
    │                                    └──► Residential proxy (10.0.0.2:8888)
    │
    └── browser_navigate/click/type ──► camofox-browser REST API (:9377)
```

### Services

| Service | Port | Role |
|---------|------|------|
| **camofox-browser** | `9377` | Anti-detection headless Firefox fork (Camoufox). REST API for navigation, snapshots, clicks, typing. |
| **camofox-firecrawl-shim** | `33879` | Firecrawl-compatible API bridge. Routes `web_extract`/`web_crawl` through Camofox for JS-rendered pages. |

## Quick Start

### Prerequisites

- Docker & Docker Compose v2
- A residential HTTP proxy (for captcha-free browsing from datacenter IPs)

### Setup

```bash
# 1. Clone
git clone https://github.com/bgwastu/agent-web-stack.git
cd agent-web-stack

# 2. Configure
cp .env.example .env
# Edit .env with your proxy and preferences

# 3. Build and start
docker compose build
docker compose up -d

# 4. Check health
docker compose ps
# Both services should show "Up (healthy)"

# 5. Test the browser
curl -s http://localhost:9377/health

# 6. Test the shim
curl -s http://127.0.0.1:33879/health
```

### Quick Test

```bash
# Create a browser tab and navigate
TAB_ID=$(curl -s -X POST http://localhost:9377/tabs \
  -H "Content-Type: application/json" \
  -d '{"userId":"test","url":"https://example.com"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tabId'])")
echo "Tab: $TAB_ID"

# Get page snapshot
curl -s "http://localhost:9377/tabs/$TAB_ID/snapshot?userId=test" | head -10

# Close tab
curl -s -X DELETE "http://localhost:9377/tabs/$TAB_ID?userId=test"
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_HOST` | `10.0.0.2` | Residential proxy hostname/IP |
| `PROXY_PORT` | `8888` | Residential proxy port |
| `CAMOUFOX_VERSION` | `135.0.1` | Camoufox binary version (for rebuilds) |
| `CAMOUFOX_RELEASE` | `beta.24` | Camoufox release channel |
| `CAMOFOX_FIRECRAWL_FIXED_USER_ID` | `agent` | User ID for session persistence |

## Upgrading

```bash
# Pull latest code
git pull

# Rebuild with updated Camoufox version (edit .env first)
docker compose build --pull

# Restart
docker compose up -d
```

To update the Camoufox browser version:
1. Check the latest at [Camoufox releases](https://github.com/daattali/camoufox-js/releases) (or Camoufox upstream)
2. Set `CAMOUFOX_VERSION` and `CAMOUFOX_RELEASE` in `.env`
3. `docker compose build camofox-browser && docker compose up -d`

## API Reference

### Camofox Browser (`:9377`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/tabs` | Create a new tab |
| POST | `/tabs/:id/navigate` | Navigate to URL or use search macro |
| GET | `/tabs/:id/snapshot` | Get accessibility tree with element refs |
| POST | `/tabs/:id/click` | Click an element by ref |
| POST | `/tabs/:id/type` | Type text into an input |
| POST | `/tabs/:id/scroll` | Scroll the page |
| DELETE | `/tabs/:id` | Close a tab |
| DELETE | `/sessions/:userId` | Clear user session data |

See [Camofox Browser docs](https://github.com/jo-inc/camofox-browser) for full API details.

## Hermes Agent Integration

Add to your Hermes profile `.env`:

```env
CAMOFOX_URL=http://localhost:9377
FIRECRAWL_API_URL=http://127.0.0.1:33879
```

And to `config.yaml`:

```yaml
web:
  backend: searxng
  extract_backend: firecrawl
```

## Migrating from systemd

If you were running Camofox as systemd services:

```bash
# Stop native services
sudo systemctl stop camofox-firecrawl-shim camofox-browser
sudo systemctl disable camofox-firecrawl-shim camofox-browser

# Start Docker stack
docker compose up -d

# Verify health
docker compose ps

# After confirming stable, remove old unit files
sudo rm /etc/systemd/system/camofox-browser.service
sudo rm /etc/systemd/system/camofox-firecrawl-shim.service
sudo systemctl daemon-reload
```

## Files

```
agent-web-stack/
├── docker-compose.yml         # Main compose file (both services)
├── .env.example               # Template for configuration
├── firecrawl-shim/
│   ├── Dockerfile             # Container image for the shim
│   ├── shim.py                # Firecrawl-compatible Python shim
│   └── requirements.txt       # Python deps (stdlib only)
└── README.md
```

## License

MIT
