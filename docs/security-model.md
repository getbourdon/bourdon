# Bourdon security model — trust tiers, tokens, audit (v0.9.0)

Bourdon federates the memory of every agent you run. That is its value — and
its blast radius: a single compromised federation member that can read
everything exfiltrates everything. v0.9.0 replaces the implicit "everyone who
reaches the server is equal" model with explicit, server-side trust.

This page is the contract. The full design rationale lives in
[`spec/SPEC_v0.9.0.md`](../spec/SPEC_v0.9.0.md).

## Threat model (what we defend against)

| Threat | Defense |
|---|---|
| Anyone who finds the endpoint reads your federation (492 MCP servers were found exposed with zero auth in 2026) | No anonymous-access code path on a network-reachable bind: loopback-only default, startup refusal without auth, Bearer auth on every request |
| A compromised member exfiltrates every agent's memory | Trust tiers: quarantined members read ONLY explicitly-granted namespaces, deny-by-default |
| A poisoned member (e.g. a malicious OpenClaw skill) injects content that propagates to every agent | Quarantined writes are STAGED, invisible to all reads until an operator promotes them; members can only write their own namespace |
| A member is compromised mid-incident | `bourdon revoke <agent>` — token dead on the next request, no restart needed |
| "What did it touch?" forensics | Append-only audit log of every operation, allow and deny, queryable per agent, surviving revocation |
| Token theft from disk/logs | Tokens are stored as SHA-256 hashes only, compared constant-time, shown once at creation, and never logged |

Out of scope in v0.9.0 (see spec non-goals): multi-tenant federation,
encryption-at-rest, OpenClaw skill auditing, per-memory-item ACLs.

## Identities and tokens

Every federation member is registered in `~/.bourdon/federation.yaml`:

```bash
bourdon agent add openclaw                      # quarantined by default
bourdon agent add clyde --tier trusted          # owner-controlled agent
bourdon agent list                              # never shows token material
bourdon agent rotate openclaw                   # new token, old one dead
bourdon revoke openclaw                         # amputation, effective now
```

`agent add` prints the member's Bearer token **once**. Bourdon keeps only the
hash. The member presents `Authorization: Bearer <token>` on the HTTP
transport; the server resolves it to an identity (agent id, tier, grants).

Two transports, two identity rules:

- **stdio** (`bourdon serve`): the caller is your own process — implicit
  trusted `operator` identity. Exactly the pre-0.9 behavior.
- **HTTP**: Bearer token required. The legacy shared token
  (`BOURDON_PEER_TOKEN_SERVER`) still authenticates and maps to the trusted
  operator identity, so existing PC↔Mac peering survives the upgrade.

## Tiers

| | `trusted` | `quarantined` |
|---|---|---|
| Read | everything (visibility-filtered, as before) | ONLY granted namespaces; deny-by-default |
| Read surface | all tools + resources | `query_agent_memory`, `find_entity`, `list_recent_work`, `list_agents`, `export_agents` — aggregate tools (`prepare_recognition_context`, `get_deeper_context`, `get_cross_agent_summary`, `compile_codex_turn`) are denied outright |
| Write (`commit_to_federation`) | direct to the live store | staged under `<library>/staging/<member>/`, own namespace only |
| Default for new members | — | ✔ |

A **namespace** is one agent's L5 manifest (its agent id). Grant reads with:

```bash
bourdon grant openclaw claude-code     # openclaw may now read claude-code's manifest
bourdon ungrant openclaw claude-code
```

Tier enforcement is **server-side, at the request layer** — an adapter cannot
bypass it, and trusted agents don't implement any security logic client-side.

## Staging (quarantined writes)

```bash
bourdon staging list
bourdon staging promote openclaw   # merge into the live store (same validated path as trusted writes)
bourdon staging reject openclaw    # delete without promoting
```

Staged manifests live outside the `agents/` glob, so they are invisible to
every read tool, every peer, and the tray until promoted. Quarantine follows
the *content*: `bourdon openclaw export` (operator-run) also stages.

## Audit

Every federation operation — allow and deny, every transport — appends one
line to `~/.bourdon/audit.jsonl`:

```bash
bourdon audit                       # recent operations
bourdon audit --agent openclaw      # one member's trail (survives revocation)
bourdon audit --denials             # what was blocked
bourdon audit --export              # raw JSONL for external analysis
```

No token material is ever written to the audit log or any other log.

## Bind/auth startup contract

- Default HTTP bind is `127.0.0.1`. **This changed in v0.9.0** (was
  `0.0.0.0`).
- Binding a non-loopback host requires auth configured (a registered agent or
  the legacy env token) — otherwise the server **exits non-zero at startup**.
- `--allow-unauthenticated` works on loopback binds only; combined with a
  non-loopback host the server refuses to start.

See [`docs/remote-setup.md`](remote-setup.md) for the full remote recipe, and
[`docs/integrations/openclaw.md`](integrations/openclaw.md) for why OpenClaw
is quarantined-class.

## Verification

`bourdon doctor` reports federation hygiene: auth posture, members with
missing/invalid tiers, revoked-but-present tokens, and staged writes older
than 7 days. Every control above is covered by negative tests in CI
(`tests/test_federation_*.py`, `tests/test_openclaw_participant.py`).
