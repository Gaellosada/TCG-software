#!/usr/bin/env python3
"""Share TCG locally via a public tunnel.

Starts a lightweight server that:
  - Serves the frontend production build
  - Proxies /api/* to the running FastAPI backend
  - Requires a password (passed as CLI arg or auto-generated)
  - Opens a cloudflared tunnel to expose it publicly

Usage:
    python dev/share.py                  # auto-generated password
    python dev/share.py --password foo   # custom password

Prerequisites:
    - Backend running on :8000 (uvicorn tcg.core.app:app)
    - Frontend built (cd frontend && npx vite build)
    - cloudflared installed (see dev/README.md)
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
BACKEND_URL = "http://localhost:8000"
SHARE_PORT = 9090


def check_prerequisites() -> None:
    if not DIST_DIR.is_dir():
        print("ERROR: frontend/dist/ not found. Run: cd frontend && npx vite build")
        sys.exit(1)
    try:
        urllib.request.urlopen(f"{BACKEND_URL}/docs", timeout=2)
    except Exception:
        print("ERROR: Backend not running on :8000. Run: uvicorn tcg.core.app:app --port 8000")
        sys.exit(1)
    cloudflared = shutil.which("cloudflared") or "/tmp/cloudflared"
    if not Path(cloudflared).is_file():
        print("ERROR: cloudflared not found. Install it or place the binary at /tmp/cloudflared")
        sys.exit(1)


def make_handler(password_hash: str):
    """Create a request handler with password protection."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DIST_DIR), **kwargs)

        # -- Auth helpers --------------------------------------------------

        def _has_valid_cookie(self) -> bool:
            for cookie in self.headers.get("Cookie", "").split(";"):
                cookie = cookie.strip()
                if cookie.startswith("tcg_auth="):
                    return cookie.split("=", 1)[1] == password_hash
            return False

        def _send_login_page(self, error: bool = False):
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TCG — Login</title>
<style>
  body {{ font-family: system-ui; background: #0d0f18; color: #e4e8f0; display: flex;
         justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
  .box {{ background: #161922; border: 1px solid #2a2e3e; border-radius: 8px;
          padding: 32px; width: 300px; text-align: center; }}
  h1 {{ font-size: 1.2rem; margin-bottom: 24px; }}
  input {{ width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #2a2e3e;
           background: #0d0f18; color: #e4e8f0; font-size: 14px; box-sizing: border-box;
           margin-bottom: 12px; }}
  button {{ width: 100%; padding: 10px; border-radius: 6px; border: none;
            background: #0ea5e9; color: white; font-size: 14px; cursor: pointer; }}
  button:hover {{ background: #0284c7; }}
  .err {{ color: #ef4444; font-size: 13px; margin-bottom: 12px; }}
</style></head><body>
<div class="box">
  <h1>Trajectoire CAP</h1>
  {"<p class='err'>Wrong password</p>" if error else ""}
  <form method="POST" action="/__login__">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Enter</button>
  </form>
</div></body></html>"""
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- Request handlers ----------------------------------------------

        def do_GET(self):
            if self.path == "/__login__":
                self._send_login_page()
                return
            if not self._has_valid_cookie():
                self._send_login_page()
                return
            if self.path.startswith("/api/"):
                self._proxy_to_backend("GET")
                return
            self._serve_spa()

        def do_POST(self):
            if self.path == "/__login__":
                self._handle_login()
                return
            if not self._has_valid_cookie():
                self._send_login_page()
                return
            if self.path.startswith("/api/"):
                self._proxy_to_backend("POST")
                return
            self.send_error(404)

        def _handle_login(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else ""
            pw = ""
            for part in body.split("&"):
                if part.startswith("password="):
                    pw = part.split("=", 1)[1]
                    pw = urllib.parse.unquote_plus(pw)
            h = hashlib.sha256(pw.encode()).hexdigest()
            if h == password_hash:
                self.send_response(302)
                self.send_header("Set-Cookie", f"tcg_auth={h}; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self._send_login_page(error=True)

        def _proxy_to_backend(self, method: str):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else None
                req = urllib.request.Request(
                    f"{BACKEND_URL}{self.path}",
                    data=body,
                    method=method,
                )
                req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_body = resp.read()
                    self.send_response(resp.status)
                    for key, val in resp.getheaders():
                        if key.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(key, val)
                    self.end_headers()
                    self.wfile.write(resp_body)
            except Exception as e:
                self.send_error(502, f"Backend error: {e}")

        def _serve_spa(self):
            rel = self.path.lstrip("/")
            file = DIST_DIR / rel
            if rel and file.is_file():
                super().do_GET()
            else:
                self.path = "/index.html"
                super().do_GET()

        def log_message(self, format, *args):
            pass

    return Handler


def start_tunnel(port: int) -> str | None:
    """Start cloudflared tunnel and return the public URL."""
    cloudflared = shutil.which("cloudflared") or "/tmp/cloudflared"
    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # cloudflared prints the URL to stderr in a line like:
    #   INF |  https://xxx.trycloudflare.com  |
    for _ in range(60):
        line = proc.stderr.readline()
        if "trycloudflare.com" in line:
            for word in line.replace("|", " ").split():
                if word.startswith("https://"):
                    return word.strip()
        time.sleep(0.5)
    return None


def main():
    parser = argparse.ArgumentParser(description="Share TCG via public tunnel")
    parser.add_argument("--password", "-p", help="Access password (auto-generated if omitted)")
    parser.add_argument("--port", type=int, default=SHARE_PORT, help="Local port (default: 9090)")
    parser.add_argument("--no-tunnel", action="store_true", help="Skip tunnel, local only")
    args = parser.parse_args()

    password = args.password or secrets.token_urlsafe(8)
    password_hash = hashlib.sha256(password.encode()).hexdigest()

    check_prerequisites()

    handler = make_handler(password_hash)
    server = http.server.HTTPServer(("0.0.0.0", args.port), handler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"  Local server running on http://localhost:{args.port}")

    if args.no_tunnel:
        print(f"  Password: {password}")
        print("\n  Press Ctrl+C to stop.\n")
    else:
        print("  Starting tunnel...")
        url = start_tunnel(args.port)
        if url:
            print(f"\n  ╔══════════════════════════════════════════════╗")
            print(f"  ║  Share this with your clients:               ║")
            print(f"  ║  URL:      {url:<34s} ║")
            print(f"  ║  Password: {password:<34s} ║")
            print(f"  ╚══════════════════════════════════════════════╝\n")
            print("  Press Ctrl+C to stop.\n")
        else:
            print("  ERROR: Tunnel failed to start. Running locally only.")
            print(f"  Password: {password}\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
