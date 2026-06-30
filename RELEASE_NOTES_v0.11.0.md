# v0.11.0 — recognition, measured and defended

The recognition thesis stops being a vibe and becomes a number: a CI-gated
precision/recall harness, a leak auditor that proves no secret federates, a
shared recognition contract that holds all three engines to the same
behavior, network-shaped adapters for cloud agents, and the Hermes Agent
joining the federation. Plus the security hardening from the 3-Star Michelin
audit.

This is the first feature release since v0.10.1. 1356 tests pass, 8 skipped.

## Added

- **Recognition quality eval harness + CI gate** (#157). `core/recognition_eval.py`
  scores recognition against a labeled golden dataset
  (`BENCHMARKS/recognition_golden_v1.yaml`, 12 self-contained cases) for
  micro/macro **precision / recall / F1**, latency p50/p95, and confidence
  accuracy. `bourdon recognition eval` exposes `--min-micro-f1` /
  `--min-macro-f1` / `--max-p95-us` CI-gate flags that drive a meaningful exit
  code; the test workflow gates at micro/macro F1 = 1.0. The thesis "did the
  *right* thing match" is now a number CI defends, not a subjective rating.
  Methodology + versioning rule in `BENCHMARKS/recognition_eval_methodology.md`.

- **Federation leak auditor** (#157). `core/leak_audit.py` statically walks
  every published L5 manifest (`~/agent-library/agents/*.l5.yaml`) and flags two
  leak classes: **CREDENTIAL** (a field value matching the canonical
  `core.redaction` patterns — reuses the single source of truth so it can't
  drift from what participants scrub at emit time) and **VISIBILITY** (an
  entity/session that resolves to private but is sitting in a federated
  manifest). Read-only, never raises. `bourdon audit-leaks --strict` is the
  CI / pre-commit gate (exit 1 on any finding); the test workflow runs it.
  Visibility was enforced per-participant before emission — this makes it a
  library-wide *guarantee*.

- **Network-shaped adapter contract v0.3** (#127, #158). Every adapter through
  v0.2 was a file-reader (`discover()` walks an on-disk path), which locked out
  agents whose state lives behind a SaaS API. `participants/_network_base.py`
  adds the `NetworkParticipant` category keeping the **same `export_l5()`
  contract** and differing only in discovery, with three built-in guarantees:
  a TTL'd local cache (`~/.cache/bourdon/<slug>/`, fresh hit = zero network,
  token never cached), graceful degradation (network down → serve most-recent
  cached payload, `degraded` health with cache age — stale loudly, never
  silently), and a hard auth boundary (token from an injected `AuthProvider`,
  never in manifest/cache/logs; an auth error is never masked by stale cache).
  `participants/github_copilot.py` is the first network adapter — reads
  `@copilot` PR activity via GitHub REST (stdlib `urllib`, no new dependency).
  Spec in `spec/PARTICIPANT_CONTRACT.md` (v0.3 section).

- **Hermes Agent participant** (#155). `participants/hermes.py` federates
  [Hermes Agent](https://github.com/NousResearch) (Nous Research) — normalizes
  its `~/.hermes` SQLite `state.db` + Markdown memory stores into an L5
  manifest. Read-only SQLite (`mode=ro`) so a live Hermes write-lock is never
  disturbed; deterministic export; visibility + redaction enforced before
  emission. Auto-discovered, so it wires into `bourdon doctor` / export-all for
  free. `bourdon hermes export` / `bourdon hermes doctor` subcommands (hook-safe:
  a missing/empty `~/.hermes` returns 0, never a traceback). Verified
  end-to-end against a live `~/.hermes` (7 real sessions). Setup +
  SessionEnd-hook + MCP docs in `docs/integrations/hermes.md`.

- **Codex CLI recognition hook** (#147). A `UserPromptSubmit` hook for Codex
  CLI that emits live recognition cues, with a general informative-clause
  condenser (overfit content-specific anchor branches removed).

## Changed

- **Shared recognition contract — tier-driven confidence parity** (#149, #150,
  #151, #152). The Claude Code, Codex, and Cursor recognition engines had
  drifted into three subtly different matchers. A shared recognition-contract +
  parity ledger (stage 1, zero behavior change) now unifies stopwords (stage 2),
  routes all three engines through one shared `match_tier` (stage 3), and
  derives per-entity confidence from the match tier (stage 4) — closing the
  contract so the same prompt yields the same recognition on every engine.

- **Recognition runtime fixes + perf + per-entity confidence + shared SQLite
  base** (#156). `hydrate_l1` uses `asyncio.to_thread` (the deprecated
  `get_event_loop()`/`run_in_executor()` dance raised on 3.12+ with no bound
  loop); `detect_entities` gains a token-set prefilter that skips the
  re-tokenizing match for any candidate whose tokens aren't a subset of the
  prompt's (sound necessary condition, semantics unchanged) — cuts the hot path
  on large manifests. Plus per-entity confidence and a shared read-only SQLite
  base reused across the SQLite-backed participants.

## Fixed

- **Federation privacy / trust hardening — 3-Star Michelin P0s + P1s** (#148).
  The audit found the federation/export surfaces leaked across machines:
  - **Credential redaction unified into `core/redaction.py` as the single
    source of truth** (P0-2, P0-4) — ~5 drifting pattern copies collapsed into
    a strict superset that also catches keyword-less token shapes (AWS `AKIA`,
    GitHub `ghp_`/`github_pat_`, GitLab, Slack, OpenAI/Anthropic `sk-`, Google
    `AIza`/`ya29`, npm, JWTs incl. Supabase `service_role`, PEM private keys)
    that previously federated verbatim. A cross-surface parity test means drift
    can no longer ship silently.
  - **PRIVATE/TEAM visibility clamped out of the federated export path** (P0-1)
    — `summarize_agent_manifest` stamped the visibility label but never filtered
    on it, so a trusted peer could pull another machine's PRIVATE session
    content over the wire. The export path now applies an access-level egress
    gate clamped to the caller's trust tier (local operator view unchanged).

## Authorship

Built in the open with the agent-as-author + agent-as-reviewer pattern —
authored by Claude Opus 4.8, with `cursor[bot]` / `copilot` review findings
hardened back in before merge.

## Get Started

```bash
pip install -e .              # Core — L0 + L1 + participants
pip install -e '.[server]'    # + L6 MCP federation server
brew install getbourdon/bourdon/bourdon
```

Full v0.10.x feature notes: [RELEASE_NOTES_v0.10.0.md](RELEASE_NOTES_v0.10.0.md).
