# v0.9.0 — Remote federation transport, trust tiers & the OpenClaw adapter

**v0.8.0 made Codex recognition active. v0.9.0 makes the federation safe to
expose.**

Bourdon's federation previously ran on an implicit trust model: every caller
that reached the MCP server got equivalent access to all federated memory.
This release replaces it with explicit, server-side trust — per-agent
identities, two trust tiers, staged quarantined writes, one-command
revocation, and a full audit trail — and ships the first quarantined-class
adapter: OpenClaw. The memory layer built for the year agent security fell
apart.

## What's new

### Trust tiers (server-side, on every transport)

Every federation member is `trusted` or `quarantined`. Quarantined members:

- read **only** explicitly-granted namespaces (a namespace = one agent's L5
  manifest), deny-by-default, on an allowlisted tool surface
- write into a **staging area** the live store never reads — content
  propagates only after `bourdon staging promote`
- can only write their **own** namespace (manifest spoofing blocked)
- are denied the whole-store aggregate tools outright

stdio callers and legacy shared-token peers resolve to the trusted operator
identity — current federations migrate with zero behavior change.

### Per-agent token auth

```bash
bourdon agent add openclaw            # quarantined by default; token shown ONCE
bourdon agent add mac --tier trusted
bourdon agent rotate mac
bourdon grant openclaw claude-code
bourdon revoke openclaw               # dead on the very next request
```

Tokens are stored as SHA-256 hashes only, compared constant-time, never
logged. Revocation takes effect on a running server without a restart.

### Hardened bind/auth contract — **BREAKING**

- HTTP default bind flipped `0.0.0.0` → **`127.0.0.1`**. Tailnet peer servers
  must now pass `--host 0.0.0.0` explicitly.
- Non-loopback binds **refuse to start** without auth configured.
- `--allow-unauthenticated` is honored on loopback only.
- There is no anonymous-access code path on a network-reachable bind.

### Audit log

Append-only JSONL of every federation operation (allow + deny):
`bourdon audit [--agent X] [--denials] [--export]`. A revoked member's
history remains queryable.

### OpenClaw adapter (quarantined class)

`participants/openclaw.py` — Bourdon's first network-shaped participant
(reads the instance API, not disk). Hard handshake gate: refuses instances
unpatched for CVE-2026-25253 (< 2026.1.29) or running with auth disabled,
with the exact reason and fix. `bourdon openclaw export` stages; registering
OpenClaw as trusted requires `--i-understand-the-risk`.

### Doctor + docs

`bourdon doctor` now reports federation hygiene (auth posture, missing tiers,
revoked-but-present tokens, staged writes older than 7 days). New docs:
[`docs/security-model.md`](docs/security-model.md),
[`docs/remote-setup.md`](docs/remote-setup.md),
[`docs/integrations/openclaw.md`](docs/integrations/openclaw.md).

## Upgrade notes

| If you… | Then… |
|---|---|
| serve HTTP on a Tailnet (`bourdon serve --transport http`) | add `--host 0.0.0.0` AND keep `BOURDON_PEER_TOKEN_SERVER` set (or migrate to `bourdon agent add`) |
| use stdio only | nothing changes |
| use `--allow-unauthenticated` on a network bind | this now refuses to start — register a token |
| want tiered access / revocation | migrate peers from the shared env token to per-agent tokens |

## Quality

1,005 tests (985 passed + integration skips at release cut), including
negative tests for every security control: anonymous 401s, revoked-token
401s, non-granted-namespace denials, staged-write invisibility, foreign-
namespace write spoofing, startup refusals, no-token-material-in-logs greps,
and mocked OpenClaw handshake refusals.

Spec: [`spec/SPEC_v0.9.0.md`](spec/SPEC_v0.9.0.md) (reconciled against the
v0.8.0 codebase before implementation — the spec is the contract).

## Deferred (P1/P2)

Per-agent rate limiting; doctor probe of a registered OpenClaw instance's
external exposure; `observer` tier; item-level ACLs; mTLS; multi-operator
federation.
