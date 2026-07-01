#!/usr/bin/env python3
"""Container healthcheck for the Bourdon L6 HTTP server.

The server is "up" as soon as it answers on /mcp. Because a healthy server
enforces auth, an unauthenticated probe gets an HTTP error (e.g. 401/403/406)
-- that still means the process is alive and serving, so we treat any HTTP
response as healthy. Only a connection failure / timeout is unhealthy.
"""
import os
import sys
import urllib.error
import urllib.request

port = os.environ.get("BOURDON_PORT", "7500")
url = f"http://127.0.0.1:{port}/mcp"

try:
    urllib.request.urlopen(url, timeout=4)
except urllib.error.HTTPError:
    sys.exit(0)  # server answered (auth challenge) -> alive
except Exception:
    sys.exit(1)  # connection refused / timeout -> not ready
sys.exit(0)
