# SPEC — Bourdon v0.9.0

## Remote Federation Transport Hardening, Trust Tiers & OpenClaw Adapter (Quarantined Class)

**Status:** Reconciled against repo state 2026-06-09 (Step 0 complete) · **Approved by:** RADMAN
**Baseline:** v0.8.0 → target v0.9.0
**Original draft:** Claude web session 2026-06-09; this in-repo version is the contract.

---

## 0. Step-0 reconciliation — what the original draft got wrong

The draft was written outside the repo and assumed no remote-transport or auth
work existed. Reality as of `main` (v0.8.0, 934 tests):

| Draft assumption | Actual repo state | Consequence for this spec |
|---|---|---|
| Repo is `getcontinuo/continuo` | `getbourdon/bourdon` | — |
| No remote transport exists | **HTTP transport shipped** (#114): `bourdon serve --transport http --host --port`, streamable-HTTP via uvicorn, plus peer federation (`--peer`/`--peers-config`, Phase 1.6/1.7) | R1 becomes a **hardening** task, not greenfield |
| No auth exists | **Single shared Bearer token** via `BOURDON_PEER_TOKEN_SERVER` env; fail-closed 503 when unset; `--allow-unauthenticated` escape hatch | R2 upgrades shared-secret → **per-agent identity tokens**; legacy env token kept as a migration path mapped to the operator identity |
| "Namespaces" exist as a memory concept | No namespace concept. Memory = flat per-agent L5 manifests (`~/agent-library/agents/<id>.l5.yaml`) served by `L6Store` | **Namespace ≡ agent manifest (agent_id)**. Tier grants are per-agent-id |
| Adapter set: "Codex, Cascade, Copilot SWE, Clyde, Claude" | 13 scan-discovered participants (claude-code, claude-code-automations, claude-desktop-code, claude-desktop-cowork, codex, codex-automations, cursor, cursor-automations, copilot, copilot-cli, copilot-vscode, copilot-automations, cascade); Clyde federates via its own publisher | R4 follows the participant protocol (`participants/base.py`), scan-registered |
| No trust/audit concepts | A **visibility** model exists (PUBLIC/TEAM/PRIVATE per entity/session) | Visibility (content sensitivity) is **orthogonal** to trust tiers (caller identity). Both apply; tiers are enforced first, then visibility filtering as today |
| `bourdon-openclaw` unknown | A `bourdon-openclaw` **OpenClaw-side plugin** (v0.1.0) ships separately on ClawHub | R4 here is the **Bourdon-side adapter** (network-shaped participant, aligns with issue #127). The two are complementary |

**Queued 0.9.0 stack (open PRs/issues), disposition:**
- PR #124 (cascade parity), #103 (adapter contract v0.2), #102 (codex metrics), drafts #104/#106 — independent; not folded; merge separately.
- Issue #127 (network-shaped adapters) — **partially delivered** by R4 (OpenClaw is the first network-shaped participant); contract generalization stays open.
- Issues #122/#115/#101/#80/#57 — out of scope.

---

## 1. Problem statement

Bourdon's federation has an implicit trust model: any caller that reaches the
MCP server (or holds the one shared peer token) gets equivalent access to all
federated memory. Two things break that:

1. **Remote transport is already live.** The HTTP transport defaults to
   `0.0.0.0` and `--allow-unauthenticated` works on any bind. Internet-reachable
   MCP endpoints are an actively exploited class (492 servers found exposed with
   zero auth, Trend Micro 2026).
2. **OpenClaw.** Highest-demand adapter target (~355K stars), highest-risk
   class: CVE-2026-25253 (one-click RCE, CVSS 8.8, patched 2026.1.29),
   30–42K exposed instances (~93% unauthenticated), ~1,184 malicious ClawHub
   skills, Moltbook leak (1.5M agent tokens), auth off by default on :8080.

Cost of not solving: one compromised member exfiltrates every agent's shared
memory. Upside: no competitor (Mem0, Zep, Letta, Supermemory, Hindsight) has a
federation trust model, because none of them federate.

## 2. Goals

1. Network-reachable MCP server with **enforced** auth — zero anonymous-access
   code paths on non-loopback binds.
2. Every federation member carries an explicit **trust tier**; propagation
   respects tier boundaries automatically, **server-side**.
3. OpenClaw adapter ships **quarantined**, refusing handshake with
   unpatched/unauthenticated instances.
4. One-command amputation (`bourdon revoke`) + append-only audit trail.
5. All verifiable via `bourdon doctor` + negative tests in CI.

## 3. Non-goals (v0.9.0)

- Multi-user/multi-tenant federation (single-operator remains; don't preclude).
- Encryption-at-rest for the library (separate ticket).
- OpenClaw skill/plugin scanning — we gate on **instance hygiene** (version,
  auth), not skill audits.
- Item-level ACLs (tiers are namespace-level = per-agent-manifest; design so
  item-level is possible later).
- UI/dashboard (CLI + MCP only; the tray reads the same surfaces).

## 4. Architecture decisions (reconciled)

### D1 — Namespace ≡ agent manifest
A "namespace" is one agent's L5 manifest (its `agent_id`). Grants are lists of
agent_ids. Item-level ACLs (P2) would nest inside this without breaking it.

### D2 — Identity registry at `~/.bourdon/federation.yaml`
Single-operator config-dir file (Open Question 3 resolved: config dir, not
keyring — keyring is per-OS pain with zero marginal benefit while the registry
stores only **SHA-256 hashes**). Shape:

```yaml
version: 1
agents:
  openclaw:
    tier: quarantined          # trusted | quarantined
    token_sha256: "<hex>"
    created_at: "2026-06-09T18:00:00Z"
    revoked: false
    grants: []                 # agent_id namespaces readable when quarantined
```

### D3 — Caller identity plumbing
- **stdio transport:** caller is the operator's own process → implicit
  `operator` identity, tier `trusted`. (Existing behavior unchanged — this is
  the "existing agents migrate to trusted automatically" acceptance.)
- **HTTP transport:** Bearer token → registry lookup (constant-time hash
  compare) → `AgentIdentity(agent_id, tier, grants)` bound to a contextvar for
  the request; tools read it. Legacy `BOURDON_PEER_TOKEN_SERVER` token maps to
  the `operator` identity (trusted) so existing PC↔Mac peering survives the
  upgrade unchanged.

### D4 — Quarantined read surface is an allowlist of tools
Deny-by-default made concrete: quarantined callers may call
`query_agent_memory`, `find_entity`, `list_recent_work`, `list_agents`,
`export_agents` — each filtered to **granted namespaces only** — plus
`commit_to_federation` (staged; see D5). All other tools and all MCP resources
return a structured denial (and audit a `deny`). Rationale: the recognition /
turn-compiler surfaces (`prepare_recognition_context`, `get_deeper_context`,
`compile_codex_turn`) aggregate across the whole store and cannot be
namespace-filtered without rebuilding their internals; v0.9.0 denies them for
quarantined callers rather than shipping a leaky filter.

### D5 — Staging
Quarantined writes land in `~/agent-library/staging/<caller>/<agent_id>.l5.yaml`
(atomic `write_l5_dict`), which `L6Store` never loads. A quarantined caller may
only commit under its **own** registered agent_id (prevents manifest spoofing —
a poisoned OpenClaw cannot stage content as `claude-code`).
`bourdon staging promote <agent>` merges the staged manifest into the live
store via the existing `commit_l5(mode="merge")`; `reject` deletes it.

### D6 — Operator-run exports of quarantined-class agents also stage
`bourdon openclaw export` writes to staging, not `agents/`. The content
originates from the quarantined instance regardless of who invokes the export;
quarantine follows the content. Promotion is the one gate.

### D7 — Audit log at `~/.bourdon/audit.jsonl`
Append-only JSONL, one line per federation operation:
`{"ts", "agent", "op", "namespace", "decision", "detail?"}`. Written by the
enforcement layer for every tool call (allow and deny) on both transports.
No token material, ever. `bourdon audit [--agent X] [--denials] [--limit N]
[--export]` queries it (`--export` emits raw JSONL — P1 delivered).

### D8 — Bind/auth startup contract (R1 hardening)
- Default bind flips `0.0.0.0` → **`127.0.0.1`** (both `bourdon serve` and
  `python -m core.l6_server`). **BREAKING** for Tailnet peers — they now pass
  `--host 0.0.0.0` explicitly (release notes call this out).
- Non-loopback bind requires auth configured (registry has ≥1 active agent
  OR legacy env token set). Otherwise the server **exits non-zero at startup**
  with an actionable error. `--allow-unauthenticated` is honored on loopback
  binds only; combined with a non-loopback host it refuses to start.
- Loopback + no auth + no flag keeps the existing per-request 503 fail-closed.

### D9 — OpenClaw handshake gate
`participants/openclaw.py` is network-shaped (first instance of issue #127):
it reads an OpenClaw instance over its local HTTP API rather than on-disk
artifacts. Hard preconditions checked in `discover()` and `health_check()`:
- version ≥ **2026.1.29** (first CVE-2026-25253 patch) — else refuse with the
  exact reason and fix ("upgrade OpenClaw to ≥ 2026.1.29").
- instance auth enabled — else refuse ("enable auth in openclaw.json, see docs").
The participant carries `QUARANTINED_CLASS = True`; `bourdon agent add openclaw
--tier trusted` requires `--i-understand-the-risk`.

## 5. Requirements → implementation map

| Req | Delivered by |
|---|---|
| R1 remote transport | D8 startup contract in `run_l6_server` + arg default flips |
| R2 auth | `core/federation_registry.py` (per-agent tokens, sha256-at-rest, `hmac.compare_digest`), middleware rewrite in `core/l6_server.py`, `bourdon agent add/list/rotate` |
| R3 trust tiers | `AgentIdentity` contextvar + enforcement layer wrapping every tool in `create_l6_server` (D4), staging (D5), `bourdon grant/ungrant`, `bourdon staging list/promote/reject` |
| R4 OpenClaw | `participants/openclaw.py` (D9) + CLI subcommands + `docs/integrations/openclaw.md` |
| R5 revocation + audit | `bourdon revoke` (registry flag, middleware re-checks per request — sessions are per-call so effect is immediate), `core/federation_audit.py` (D7) |
| R6 doctor + docs | doctor federation section: bind-vs-auth config, agents missing tier, stale staged writes >7d, revoked-but-token-present; `docs/security-model.md`, `docs/remote-setup.md`; release notes; version bump |

P1 items delivered in-release: `bourdon agent rotate`, `bourdon audit --export`.
P1 deferred: per-agent rate limiting; doctor probe of a registered OpenClaw
instance's external exposure.
P2 unchanged from draft (observer tier, item ACLs, mTLS, multi-operator).

## 6. Acceptance criteria

All of the draft's acceptance boxes hold, restated against the real repo:

- Default config → binds `127.0.0.1` only. `--host 0.0.0.0` with no auth →
  exit non-zero (negative test). stdio behavior unchanged (suite green).
- No/invalid/revoked token → 401 before any handler; valid token → resolves
  to that agent's identity + tier. caplog-grep test: no token material in logs.
- Quarantined read of non-granted namespace → structured denial + audit entry.
  Quarantined write invisible to all reads until promoted. staging
  list/promote/reject e2e. Existing members (legacy env token, stdio) behave
  exactly as v0.8.0 (→ trusted).
- OpenClaw: unpatched or auth-disabled instance → handshake refused with
  actionable error (mocked negative tests); compliant instance → export lands
  in staging and promotes cleanly.
- Revoked agent's audit history remains queryable post-revocation.
- Full suite green; every security control has a negative test.

## 7. Sequencing (each step leaves the suite green)

1. R3 tiers on stdio (registry + enforcement + staging + audit substrate)
2. R1+R2 transport hardening + per-agent token auth
3. R5 revocation + audit CLI
4. R4 OpenClaw adapter
5. R6 doctor + docs + release

Invariant: never a commit where remote access exists without tiers, or the
OpenClaw adapter exists without quarantine.
