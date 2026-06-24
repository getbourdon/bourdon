# Remote federation setup (v0.9.0)

Make the Bourdon MCP server reachable from other machines and remote agent
surfaces (Claude web/Desktop, cloud agents) — with authentication that is
enforced, not suggested.

## TL;DR

```bash
# 1. Register a member identity for each remote caller (token shown ONCE):
bourdon agent add mac --tier trusted
bourdon agent add openclaw                       # quarantined by default

# 2. Serve on the network — non-loopback binds REQUIRE auth configured:
bourdon serve --transport http --host 0.0.0.0 --port 7500

# 3. Callers authenticate every request:
#    Authorization: Bearer bdn_<token>
```

## Bind rules (changed in v0.9.0)

| Bind | Auth configured? | Result |
|---|---|---|
| `127.0.0.1` (default) | any | serves; unauthenticated requests 503 (or use `--allow-unauthenticated`) |
| `127.0.0.1` + `--allow-unauthenticated` | — | serves; local callers get trusted operator access |
| `0.0.0.0` / Tailnet IP | yes | serves with Bearer auth on every request |
| `0.0.0.0` / Tailnet IP | no | **exits non-zero at startup** |
| `0.0.0.0` + `--allow-unauthenticated` | any | **exits non-zero at startup** — no anonymous network access, ever |

**Upgrading from ≤0.8.x:** the default bind flipped from `0.0.0.0` to
`127.0.0.1`. Tailnet peer servers must now pass `--host 0.0.0.0` explicitly
AND have auth configured. Your existing `BOURDON_PEER_TOKEN_SERVER` /
`BOURDON_PEER_TOKEN` pair keeps working unchanged (it authenticates as the
trusted operator identity) — but per-agent tokens are better: tiered access,
rotation, and one-command revocation.

## Per-agent tokens (recommended)

```bash
bourdon agent add mac --tier trusted
# token: bdn_2f0a...   <- store this on the Mac, e.g.:
#   export BOURDON_PEER_TOKEN_PC="bdn_2f0a..."
```

Peer config on the Mac (`~/.bourdon/peers.yaml`):

```yaml
peers:
  - name: pc
    url: http://pc.tailnet:7500
    token_env: BOURDON_PEER_TOKEN_PC
```

Rotation and amputation:

```bash
bourdon agent rotate mac      # new token; old token dead immediately
bourdon revoke mac            # 401 on the very next request; audit trail kept
```

## Remote MCP from Claude web / Desktop

Expose the authenticated HTTP endpoint (Tailscale Funnel, Cloudflare Tunnel,
or any TLS-terminating proxy in front of `127.0.0.1:7500`), then register the
MCP endpoint with `Authorization: Bearer <token>` as a custom header. The
dogfood milestone for this release: a Claude web session answering "where are
we with X" from Bourdon, not platform memory.

For quarantined members, grant the namespaces they may read first:

```bash
bourdon grant openclaw claude-code
```

## Verifying

```bash
bourdon doctor          # federation section: auth posture, tiers, stale staging
bourdon audit --limit 20
curl -s -o /dev/null -w '%{http_code}' http://pc.tailnet:7500/mcp   # 401 = good
```

Security model details: [`docs/security-model.md`](security-model.md).
