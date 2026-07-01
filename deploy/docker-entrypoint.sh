#!/bin/sh
# Bourdon self-host entrypoint.
#
# First boot: mints one "owner" token (trusted) if the trust registry is empty,
# prints it ONCE to the container logs, then launches the L6 MCP server over
# HTTP. The token + library persist on the /data volume across restarts.
#
# Everything is overridable by env (see the Dockerfile ENV block):
#   BOURDON_FEDERATION_CONFIG  trust registry path   (default /data/.bourdon/federation.yaml)
#   BOURDON_AUDIT_PATH         audit log path        (default /data/.bourdon/audit.jsonl)
#   BOURDON_LIBRARY            agent-library path    (default /data/agent-library)
#   BOURDON_HOST               bind host             (default 0.0.0.0)
#   BOURDON_PORT               bind port             (default 7500)
set -eu

BOURDON_FEDERATION_CONFIG="${BOURDON_FEDERATION_CONFIG:-/data/.bourdon/federation.yaml}"
BOURDON_AUDIT_PATH="${BOURDON_AUDIT_PATH:-/data/.bourdon/audit.jsonl}"
BOURDON_LIBRARY="${BOURDON_LIBRARY:-/data/agent-library}"
BOURDON_HOST="${BOURDON_HOST:-0.0.0.0}"
BOURDON_PORT="${BOURDON_PORT:-7500}"
export BOURDON_FEDERATION_CONFIG BOURDON_AUDIT_PATH

mkdir -p "$(dirname "$BOURDON_FEDERATION_CONFIG")" "$BOURDON_LIBRARY"

# First boot only: mint an owner token if no member has one yet.
if [ ! -f "$BOURDON_FEDERATION_CONFIG" ] || ! grep -q 'token_sha256' "$BOURDON_FEDERATION_CONFIG" 2>/dev/null; then
  echo "[bourdon] first boot — no trust registry yet; minting an owner token…" >&2
  _out="$(bourdon agent add owner --tier trusted --i-understand-the-risk 2>/dev/null || true)"
  _tok="$(printf '%s\n' "$_out" | sed -n 's/^token: //p')"
  cat >&2 <<BANNER
============================================================
 Bourdon self-host is ready.

   MCP URL : http://<this-host>:${BOURDON_PORT}/mcp   (https:// behind Fly/TLS)
   Token   : ${_tok:-<mint one: docker exec <ctr> bourdon agent add owner --tier trusted>}

   This token is shown ONCE (stored only as a hash on the /data volume).
   Put it in your MCP client config:

   {
     "mcpServers": {
       "bourdon": {
         "url": "http://<this-host>:${BOURDON_PORT}/mcp",
         "headers": { "Authorization": "Bearer ${_tok}" }
       }
     }
   }

   Rotate anytime:  docker exec <ctr> bourdon agent rotate owner
============================================================
BANNER
fi

exec bourdon serve --transport http \
  --host "$BOURDON_HOST" --port "$BOURDON_PORT" \
  --library "$BOURDON_LIBRARY" "$@"
