# Bourdon Architecture

**Status:** current as of v0.10.x · **Supersedes:** [`ARCHITECTURE_v0.1.md`](ARCHITECTURE_v0.1.md) (the historical Clyde/NeuroLayer design)

This document describes Bourdon *as it is implemented in `core/` and
`participants/` today* — the tiered personal-memory stack (L0–L4), the
cross-agent federation layer (L5–L6), the recognition-first runtime, and the
participant contract that lets Bourdon federate across agents it does not
control. For the founding argument (recognition > retrieval, concurrent
language > call-and-repeat) read [`THESIS.md`](THESIS.md); for the public
positioning read [`POSITIONING.md`](POSITIONING.md).

---

## 1. The core insight

Every retrieval-augmented system retrieves *after* the moment: the human asks,
the system stops, digs, returns. That pause breaks the illusion of a mind.
Humans don't retrieve — they **recognize**. Hearing a word activates a web of
associations *before* conscious recall begins, and detail surfaces *as* the
conversation continues. Bourdon is the engineering translation of that
concurrent structure: recognition first, hydration second, archive descent only
when needed.

---

## 2. The memory stack

```
Per-agent personal memory:
  L0 — Hot Cache          always in system prompt, ~2-3K tokens, never retrieved
  L1 — Entity Synopses    triggered on an L0 keyword hit, parallel-loaded
  L2 — Episodic Memory    async retrieval during human response time
  L3 — Indexed History    on-demand searchable session logs
  L4 — Raw Archive        verbatim conversation history (ground truth)

Cross-agent federation:
  L5 — Agent Memory Manifest    per-agent public glossary (a projection of L0-L4)
  L6 — Federation Library       aggregates every L5, exposed as an MCP server
```

The personal tiers (L0–L4) are about *one agent feeling continuous to its user*.
The federation tiers (L5–L6) are about *many agents sharing one memory of the
work* — continuity around the work, not around a vendor account.

### L0 — Hot Cache
A compact, always-loaded payload (keywords, entity names, project slugs, active
flags, key dates) injected permanently into the system prompt. Static; never
retrieved. The thing the agent simply *knows*.

### L1 — Entity Synopses
A tight (~300–500 token) synopsis per known entity, loaded the instant an L0
keyword is detected in the incoming message — *while* the model begins
formulating its opening response. This is the recognition→recall transition.

### L2 — Episodic Memory
Topic/project/person-indexed session summaries, fired during the human's
response to the L1-informed message. Retrieval-backend-agnostic (the original
UltraRAG coupling is gone; L2 is now a `Protocol`-shaped client — see
`core/l2.py` — that never blocks and never raises, disabled by default).

### L3 / L4 — Indexed history & raw archive
On-demand searchable logs (L3) and verbatim history (L4), descended into only
when the conversation demands specifics.

---

## 3. The recognition-first runtime (`core/recognition_runtime.py`)

This is the concrete implementation of the timing thesis. The public entry point
is `recognition_first(user_msg, manifest, *, l1_dir=..., access_level="team")`,
which returns a `RecognitionResult`:

| Field | Meaning |
|-------|---------|
| `recognition` | The immediate, **no-retrieval** acknowledgment string. Ready to emit as the first sentence. `""` when nothing matched. |
| `matched_entities` | The entity dicts that triggered recognition. |
| `confidence` | The shared contract bucket (`none`/`low`/`medium`/`high`) for the **top** anchor — agrees with the codex/cursor surfaces for the same `(prompt, top anchor)` (parity contract). |
| `entity_confidences` | Per-entity buckets for **every** match, so a caller surfacing 2+ entities can hedge a weak secondary. The top entity's bucket equals `confidence`. |
| `hydration` | An awaitable resolving to L1-hydrated detail, run **in parallel** with the caller's own streaming. `None` when there were no matches. |

### The timing contract (enforced, not just documented)

```
recognition_first() returns  ──► emit result.recognition NOW   (synchronous, no I/O)
        │
        ├─ start model.stream(...) for the main response
        └─ asyncio.create_task(result.hydration)  ──► L1 detail lands in parallel,
                                                       injected next turn / appended
```

Three invariants the code and tests guarantee:

1. **Recognition never blocks on retrieval.** `recognition_first` is fully
   synchronous — no event loop, no I/O, no model call. The recognition string is
   a deterministic template populated from matched-entity metadata
   (`build_recognition_string`). This keeps L0 honest: recognition is
   *recognition*, not abbreviated retrieval.
2. **Hydration never blocks the first response.** It is handed back as an
   un-awaited awaitable. `hydrate_l1` offloads each blocking file read via
   `asyncio.to_thread`, so multiple entities hydrate concurrently.
3. **Hydration degrades cleanly.** A timeout past `DEFAULT_HYDRATION_TIMEOUT`
   (3.0s) yields `""`, so the worst case is a clean "L0-only response," never a
   crash.

`tests/test_recognition_runtime.py` asserts all three directly — including an
explicit ordering test that recognition is emitted *strictly before* a slow
hydration resolves.

### Entity detection

`detect_entities` matches manifest entities against the prompt using the shared
`core/recognition_contract.py` match ladder (EXACT > NAME_SUBSTRING >
TOKEN_SUBSEQUENCE > TOKEN_OVERLAP > NONE), gated at `>= TOKEN_SUBSEQUENCE`. The
ladder carries a short-name false-positive guard ("ILTTed" never matches
"ILTT"). A token-set **prefilter** tokenizes the prompt once and skips the
(re-tokenizing) match-tier call for any candidate whose tokens aren't a subset of
the prompt's — a sound necessary condition that preserves match semantics while
keeping the latency-critical path cheap on large manifests.

### Interrupt-first (speaker-still-talking)

`interrupt_first` is the symmetric primitive for when a new user message arrives
mid-generation: it cancels the in-flight slot (best-effort, idempotent), then
computes recognition for the new message, and signals which slot to continue on
for KV-cache reuse. `build_splice_prompt` optionally threads the interrupted
context into the new turn.

---

## 4. The federation layer (L5–L6)

### L5 — Agent Memory Manifest
A per-agent, visibility-filtered glossary: a *projection* of that agent's L0–L4
into a portable manifest. Normative schema: [`L5_schema.json`](L5_schema.json).
Shape: an `agent` block (id, type, optional `role_narrative` and temporal
fields), `recent_sessions`, and `known_entities`. Entities carry optional
`valid_from`/`valid_to` temporal-validity windows (Zep-Graphiti-inspired) so
federation queries can answer "what was active in Q1 2026?" not just "what's in
memory?".

### L6 — Federation Library (`core/l6_store.py`, `core/l6_server.py`)
Loads every `~/agent-library/agents/*.l5.yaml`, builds a cross-agent entity
index, and exposes query primitives — `list_agents`, `find_entity`,
`list_recent_work`, `get_cross_agent_summary`, plus the recognition-orchestration
surfaces (`prepare_recognition_context`, `get_deeper_context`) — as a `fastmcp`
MCP server. Visibility is **re-applied at query time** with an
`access_level=public|team|private` gate. Peer L6 servers can be federated
(depth-1) via `--peer`, with peer-sourced agents tagged `peer:<name>:<agent>`.

---

## 5. The participant contract (`participants/`)

A **participant** bridges an agent's native memory store and the L5 manifest.
Two kinds, one interface (`participants/base.py`, full contract in
[`PARTICIPANT_CONTRACT.md`](PARTICIPANT_CONTRACT.md)):

- **Native publisher** — the agent writes its own L5 (we control it).
- **External participant** — code that reads an agent's native store (files,
  SQLite, JSONL) and normalizes it into L5 (we don't control the agent).

Every participant implements `discover()` / `export_l5()` / `export_sessions()` /
`health_check()`, registers via the `bourdon.participants` entry point, and is
auto-discovered by `discover_participants()` — which is what wires it into
`bourdon doctor` and `bourdon export-all`. Shipping participants include Claude
Code, Codex, Cursor, Copilot (CLI/VS Code), Cascade, and Hermes Agent.

Four non-negotiable semantics:

1. **Visibility is enforced in the participant, before emission.** L6 trusts the
   manifest it receives; there is no second filter. A `private`-tagged entity
   that escapes the participant leaks.
2. **`export_l5()` is deterministic** for a given native-store state, so L6 can
   detect change via a manifest hash (idempotency).
3. **Errors are bounded.** `discover()` raises only `ParticipantDiscoveryError`;
   everything else is caught and converted to a `HealthStatus.degraded`.
   Participants never propagate unknown exceptions to L6.
4. **Reads are non-destructive.** Participants reading a live SQLite store open
   it read-only so a running agent's write-lock is never disturbed.

---

## 6. System-prompt injection order

```
[SYSTEM PROMPT]
  1. Base persona / instructions
  2. L0 hot cache (always)
  3. L1 synopses for detected entities (parallel-loaded, recognition-first)
  4. L2 episodic context (if it landed in time; never blocks)
  5. L6 federation context (cross-agent, when an MCP surface is wired)
[USER MESSAGE]
```

---

## 7. What changed from the historical doc

| Historical (`ARCHITECTURE_v0.1.md`, "Clyde/NeuroLayer") | Current (Bourdon) |
|---|---|
| Ollama-only, single local agent | Model/agent-agnostic; federates Claude Code, Codex, Cursor, Copilot, Cascade, Hermes |
| UltraRAG hard-coded as the L2 backend | L2 is a non-blocking `Protocol` client, backend-agnostic, off by default |
| L0–L4 personal stack only | + L5 manifest, L6 MCP federation, peer federation |
| "RADLAB Internal" packaging as NeuroLayer | Open-source spec + reference implementation under BUSL-1.1 |
| Timing model as pseudocode | Implemented in `core/recognition_runtime.py` with enforced timing tests |

The **timing thesis itself is unchanged** — it's the load-bearing idea, now
shipped as code.
