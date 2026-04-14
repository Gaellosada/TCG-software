# dev/ — Developer tools

Scripts in this folder support development workflows (sharing, tunneling, etc.) and are not part of the production application.

## share.py — Share TCG with clients

A self-contained script that exposes the local TCG instance via a public URL with password protection. Designed for quick client demos without requiring them to install anything.

### How it works

```
Client browser  --->  Cloudflare Tunnel  --->  share.py (:9090)  --->  FastAPI backend (:8000)
                      (public URL)              |
                                                +--> frontend/dist/ (static files)
```

1. **Local HTTP server** on port 9090 serves the frontend production build (`frontend/dist/`) and proxies `/api/*` requests to the running FastAPI backend on port 8000.
2. **Password gate** — every request requires a session cookie obtained by entering the password on a login page. The cookie is a session cookie (cleared when the browser closes). Passwords are never stored — only their SHA-256 hash is compared.
3. **Cloudflare Tunnel** (`cloudflared`) creates a reverse tunnel so the server is accessible via a `*.trycloudflare.com` URL. No account or signup required. Nothing is uploaded — traffic is routed to your machine and the URL dies when you stop the script.

### Prerequisites

- **Backend running** on port 8000: `uvicorn tcg.core.app:app --port 8000`
- **Frontend built**: `cd frontend && npx vite build`
- **cloudflared** binary available. Install options:
  - Download directly: `curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared && chmod +x /tmp/cloudflared`
  - Or via package manager: `brew install cloudflared` / `apt install cloudflared`

### Usage

```bash
# Auto-generated password
python dev/share.py

# Custom password
python dev/share.py --password mypassword

# Local only (no tunnel)
python dev/share.py --no-tunnel

# Custom port
python dev/share.py --port 8080
```

The script prints a box with the public URL and password to share with clients:

```
  ╔══════════════════════════════════════════════╗
  ║  Share this with your clients:               ║
  ║  URL:      https://xxx.trycloudflare.com     ║
  ║  Password: xxxxxxxx                          ║
  ╚══════════════════════════════════════════════╝
```

Press `Ctrl+C` to stop. The tunnel URL becomes unreachable immediately.

### Security notes

- The password is hashed (SHA-256) before comparison — plaintext is never stored in memory beyond initial setup.
- The auth cookie is `HttpOnly` and `SameSite=Strict`.
- Cloudflare's free quick tunnels have no uptime guarantee and are meant for temporary use, not production.
- The MongoDB connection stays local — clients access data only through the backend's API, same as in normal development.
