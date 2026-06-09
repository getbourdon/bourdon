# Bourdon × OpenClaw (quarantined class)

OpenClaw is the highest-demand integration target in the ecosystem (~355K
GitHub stars) — and its highest-risk agent class:

- **CVE-2026-25253** — one-click RCE, CVSS 8.8, first patched in **2026.1.29**
- 30,000–42,000 internet-exposed instances; ~93% without authentication
- ClawHavoc: ~1,184 malicious skills (~12–20% of the ClawHub registry)
- Moltbook leak: 1.5M agent API tokens exposed
- Auth disabled by default on port 8080

Bourdon ships the adapter anyway — **inside a quarantine**. You get OpenClaw's
context; OpenClaw does not get your federation.

> Complementary, not duplicate: the separate `bourdon-openclaw` ClawHub plugin
> runs OpenClaw-side (OpenClaw consuming Bourdon recognition). THIS adapter is
> Bourdon-side: Bourdon reading an OpenClaw instance's state into staging.

## What "quarantined" means here

1. **Hard handshake gate.** The adapter refuses — with the exact reason and
   fix — to talk to an instance that is unpatched (`< 2026.1.29`) or has
   authentication disabled. These are hard preconditions, not warnings.
2. **Reads are deny-by-default.** Registered as a federation member, OpenClaw
   reads only namespaces you explicitly `bourdon grant` it.
3. **Writes are staged.** Nothing OpenClaw contributes touches the live store
   until you `bourdon staging promote openclaw`. This includes operator-run
   `bourdon openclaw export` — quarantine follows the content.
4. **Trusted registration needs an explicit override.**
   `bourdon agent add openclaw --tier trusted` refuses without
   `--i-understand-the-risk`.

We gate on **instance hygiene** (version, auth) — not on auditing your
installed skills. Skill scanning is ClawSecure et al.'s job.

## Setup

```bash
# Point Bourdon at your OpenClaw instance (defaults shown):
export OPENCLAW_URL="http://127.0.0.1:8080"
export OPENCLAW_TOKEN="<your OpenClaw API token>"   # required: auth must be ON

bourdon openclaw doctor    # handshake gate: version + auth checks
bourdon openclaw export    # exports sessions/memories INTO STAGING
bourdon staging list
bourdon staging promote openclaw
```

If the doctor reports `blocked`, the reason tells you exactly what to fix:

| Refusal | Fix |
|---|---|
| version predates the CVE-2026-25253 patch | upgrade OpenClaw to ≥ 2026.1.29 |
| authentication DISABLED | enable auth in your OpenClaw config, restart, set `OPENCLAW_TOKEN` |
| instance unreachable | start OpenClaw locally or set `OPENCLAW_URL` |
| no parseable version | upgrade; Bourdon fails closed when it cannot verify the patch level |

## Federating OpenClaw as a live member

To let a running OpenClaw instance query the federation (rather than just
being read by it), register it and grant the namespaces it may see:

```bash
bourdon agent add openclaw            # quarantined; token shown once
bourdon grant openclaw claude-code    # the ONLY namespace it can read
bourdon serve --transport http        # loopback by default; see remote-setup.md
```

Wire the token into OpenClaw's MCP client config as
`Authorization: Bearer <token>`. Its `commit_to_federation` writes land in
staging under its own namespace only; its denied operations are visible via
`bourdon audit --agent openclaw --denials`.

Compromised instance? One command:

```bash
bourdon revoke openclaw
```

## Adapter shape (for self-authoring against)

`participants/openclaw.py` follows the standard `BourdonParticipant` protocol
(`discover` / `export_l5` / `export_sessions` / `health_check`) but is
**network-shaped** (the first of its kind — adapter contract issue #127): it
reads `GET /api/status`, `/api/sessions`, `/api/memories` from the instance
instead of on-disk artifacts.

- `discover()` calls `verify_instance(status, url)` — the handshake gate.
  Tolerated status shapes: version under `version` / `openclaw_version` /
  `app_version`; auth flag under `auth_enabled` / `authEnabled` /
  `auth.enabled`.
- All exported text passes through credential redaction
  (`_safe_native_memory_text`); the manifest's visibility policy defaults to
  TEAM, never PUBLIC.
- `QUARANTINED_CLASS = True` is the marker the CLI consults for the
  `--i-understand-the-risk` gate and the staging-not-live export routing.
- `health_check()` never raises: `ok` / `blocked` (with `proposed_fix`) /
  `degraded`.

Tests: `tests/test_openclaw_participant.py` (mocked instance; every refusal
path has a negative test). Security model: [`../security-model.md`](../security-model.md).
