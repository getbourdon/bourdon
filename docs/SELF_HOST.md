# Self-Host Bourdon (free)

Bourdon's engine is **free to run yourself, forever** — the CLI/interop layer is
Apache-2.0 and the server engine is BUSL-1.1 (source-available; you may self-host
it for your own agents or organization; only *reselling it as a hosted service*
is reserved to RADLAB, and every release converts to Apache-2.0 after its change
date). If you'd rather not run it, a managed hosted option is planned — but you
never have to wait for it.

This guide stands up a **single-instance** Bourdon MCP endpoint your agents can
connect to. Three ways, smallest to biggest:

1. [Local, stdio](#1-local-stdio-claude-desktop--claude-code) — no network, for one machine.
2. [Local, HTTP via Docker](#2-local-http-docker) — reachable on your LAN/tailnet.
3. [Always-on via Fly.io](#3-always-on-flyio) — a personal URL, TLS, wakes on demand.

> **Single-tenant.** Each instance serves **one** trust registry / library. It is
> perfect for you, your team, or one org. Hosting **many isolated customers** on
> one deployment is a different (multi-tenant) problem — not what this is.

---

## Install

```bash
pip install 'bourdon[server]'          # from PyPI — [server] adds the MCP host (fastmcp)
# or from source:
git clone https://github.com/getbourdon/bourdon && cd bourdon && pip install '.[server]'
```

Requires Python ≥ 3.10. The `[server]` extra is what makes `bourdon serve` work
(it pulls `fastmcp` + `uvicorn`); the bare `pip install bourdon` gives you the
CLI/analysis surface but not the MCP server. To federate two of your own
instances (`serve --peer …`), use the superset extra `'bourdon[federation]'`.
The Docker/Fly paths below already bake this in.

---

## 1. Local, stdio (Claude Desktop / Claude Code)

The zero-config path. No ports, no tokens — the MCP client launches the server
over stdio and talks to it directly.

**Claude Desktop** (`claude_desktop_config.json`):

```json
{ "mcpServers": { "bourdon": { "command": "bourdon", "args": ["serve"] } } }
```

**Claude Code:**

```bash
claude mcp add bourdon -- bourdon serve
```

That's it. See [`docs/integrations/`](integrations/) for other hosts.

---

## 2. Local, HTTP (Docker)

For an endpoint other machines on your LAN or tailnet can reach.

```bash
docker compose up -d --build
docker compose logs bourdon        # <- your one-time token prints here
```

The first boot mints an **owner** token and prints it once (it's stored only as
a hash, on the `bourdon-data` volume). Point your MCP client at it:

```json
{
  "mcpServers": {
    "bourdon": {
      "url": "http://<this-host>:7500/mcp",
      "headers": { "Authorization": "Bearer bdn_…" }
    }
  }
}
```

Without compose:

```bash
docker build -t bourdon .
docker run -d -p 7500:7500 -v bourdon-data:/data --name bourdon bourdon
docker logs bourdon
```

Manage tokens anytime:

```bash
docker exec bourdon bourdon agent list
docker exec bourdon bourdon agent rotate owner        # old token dies immediately
docker exec bourdon bourdon agent add teammate --tier trusted   # a second token
```

---

## 3. Always-on (Fly.io)

A personal, TLS-terminated URL that wakes on the first request and sleeps when
idle (so it's ~free at rest). Uses the repo's [`fly.toml`](../fly.toml) template.

```bash
fly launch --no-deploy --copy-config --name <your-app>
fly volumes create bourdon_data --size 1 --region <region> --app <your-app>
fly deploy --app <your-app>
fly logs   --app <your-app>         # <- your one-time token
```

Your endpoint: `https://<your-app>.fly.dev/mcp` (with `Authorization: Bearer …`).
Set `min_machines_running = 1` in `fly.toml` to keep it always warm.

Docker works the same on Render, Railway, a plain VPS, etc. — anywhere that runs
a container with a persistent volume for `/data`.

---

## Security notes

- **Auth is required on any non-loopback bind.** Binding `0.0.0.0` (as the
  container does) refuses to start unless a token exists — the entrypoint mints
  one on first boot. There is no accidental open endpoint.
- **The token is a bearer secret.** Anyone with it can read/write that library's
  memory. Treat it like a password; rotate with `bourdon agent rotate owner`.
- **Don't put a raw HTTP instance on the public internet.** Use it on a private
  network (LAN/tailnet), or front it with TLS (the Fly path does this for you).
- **Your data stays on your volume.** `/data` holds the library, the trust
  registry (hashed tokens only), and the audit log. Back up the volume to back
  up your memory.

## Federate two of your own instances (optional)

Run Bourdon on your laptop and a server, and let them share memory:

```bash
# on each side, mint a token for the other and list it as a peer
bourdon agent add other-machine --tier trusted
bourdon serve --transport http --host 0.0.0.0 \
  --peer https://your-other-instance/mcp
```

See [`config/peers.example.yaml`](../config/peers.example.yaml) for the peers
file format. Federation is depth-1 by design (a peer cannot pull your peers'
peers).
