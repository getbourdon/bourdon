# Bourdon — Positioning

**Status:** v0.1, 2026-04-27. Stake-in-the-ground document.

This file makes Bourdon's positioning explicit so contributors, evaluators,
and future-us can hold each other to it. It is intentionally short and
opinionated. If a future commit adds a feature whose justification doesn't
trace back to one of the claims below, that commit needs a different
justification or the claim needs revising.

---

## The Claim

**Bourdon is the first memory layer for AI agents that treats *runtime
interaction timing* as a first-class problem.** Every other agent-memory
framework treats memory as a *representation + retrieval* problem and
optimizes for accuracy, recall, latency-of-the-call, or knowledge-graph
richness. Those are real problems and the field is good at them. But they
are not the same problem as: *does the first sentence of the AI's response
feel like recognition or like lookup?*

That second problem is what Bourdon exists to solve.

---

## The Gap We're Filling

The 2026 agent-memory landscape is mature. Mem0, Zep (Graphiti), Letta,
Cognee, Supermemory, LinkedIn's Cognitive Memory Agent, and a growing
academic literature (Memora, Memory in the Age of AI Agents survey, H-MEM,
G-Memory, Intrinsic Memory Agents, the Shared Context Store proposal in
arXiv 2601.11595) collectively solve, very well:

- **Persisting** facts across sessions
- **Retrieving** the right ones at query time
- **Structuring** memory as graphs, embeddings, hybrid stores
- **Federating** memory across multiple servers (the SCS / MCP
  collaboration direction)
- **Scoping** memory to user / session / agent
- **Tracking** temporal validity (Zep's biggest wedge)

What none of them solve, or even frame as a problem worth solving:

> When an agent has memory, the **moment-by-moment shape** of the
> response should match how a human with that memory would actually
> respond — recognition first, hydrated detail second, archive descent
> third — instead of: silence, retrieval call, paragraph from notes.

That asymmetry is what makes today's "memory-enabled" agents still feel
like databases that talk. The data layer is solved. The behavior layer
isn't.

This is documented as a live finding from a 2026-04-19 test session: see
[`FINDINGS_JOURNAL.md` — Codex Recognition-First Gap](FINDINGS_JOURNAL.md).
We invoked Codex with a fully-populated Bourdon context and asked
"What do you know about OMNIvour?" The data layer worked perfectly.
The agent still answered like a retrieval system: searched first,
summarized notes second. **That gap is the product.**

---

## How Bourdon Differs from the Field

| Framework | What it optimizes | What it does not address |
|---|---|---|
| **Mem0** | 3-tier scope (user/session/agent), hybrid store, compliance | Runtime interaction shape |
| **Zep** (Graphiti) | Temporal validity windows, knowledge graph correctness | Runtime interaction shape |
| **Letta** | Editable memory blocks, stateful runtime | Runtime interaction shape (focuses on memory *editing* during runtime, not on the *first response moment*) |
| **Cognee** | Doc → KG construction | Runtime interaction shape |
| **Memora** ([paper](https://hf.co/papers/2602.03315)) | Abstraction + cue anchors representation balance | Runtime interaction shape (framed as a representation problem, not a timing problem) |
| **MCP SCS** ([paper](https://arxiv.org/abs/2601.11595)) | Cross-server context sharing for multi-agent workflows | Runtime interaction shape |
| **Bourdon** | **Runtime interaction shape: recognition → hydration → archive descent, timed to conversation rhythm** | Less mature on representation, less compliance posture, no commercial cloud — by choice |

The pattern is clear: every box in the right column says "runtime
interaction shape." That column is open. We're staking it.

---

## Why "Timing" Is the Right Frame

The thesis is grounded in how human cognition actually works:

1. **Recognition fires first.** When you hear a familiar name, your brain
   confirms identity in milliseconds — *before* details surface.
2. **Hydration runs in parallel.** While you're already speaking the
   recognition response ("Oh yeah, OMNIvour..."), your brain is fetching
   project context in the background.
3. **Archive descent is deliberate.** You only consciously dig for old
   details when the current conversation demands them.

A retrieve-then-respond architecture forces all three steps into a single
serial pipeline. Recognition can't happen until retrieval finishes.
Hydration can't happen in parallel because there's nothing to be parallel
*to*. The result is the lookup-feel that breaks the illusion of a mind.

Bourdon's L0–L6 stack is the engineering translation of this cognition
sequence:

- **L0** (always-loaded keywords) → recognition substrate
- **L1** (entity synopses) → hydration substrate
- **L2/L3/L4** (episodic / indexed / archive) → descent substrate
- **L5/L6** (federation) → cross-agent recognition substrate

The numbering is not arbitrary; it encodes the **timing budget** each
layer is allowed to consume relative to a conversation turn.

---

## Where the Wedge Comes From — and What It Costs

We chose timing as the wedge because:

1. **It's empty.** Nobody else is framing the problem this way as of
   April 2026. The field has rallied around representation + retrieval
   + temporal correctness. Timing is white space.
2. **It's testable from felt experience.** The 8-out-of-10 recognition
   feel test in `THESIS.md` is subjective but it's *honestly* subjective
   — humans can tell within seconds whether an agent feels like a mind
   or like a database. We don't need a benchmark we don't have.
3. **It compounds with everything else.** A timing-aware layer on top
   of Mem0's scope model, Zep's temporal graph, or Letta's editable
   blocks doesn't have to compete with them. It can make any of them
   *feel* more like a mind.

The cost of choosing timing as the wedge:

- **Less compliance posture.** SOC 2 / HIPAA are not on the v1
  roadmap. If you're building for healthcare or finance today,
  Mem0 is the right choice; Bourdon isn't.
- **Less benchmark presence.** Bourdon doesn't beat anyone on
  LongMemEval. We don't try to. The benchmark we care about doesn't
  exist yet (response-shape eval).
- **Smaller initial population of users.** "Memory that feels like
  recognition" is a felt pitch, not a metric pitch. Adoption depends
  on people *trying it* and noticing the difference, which is slower
  than adoption driven by leaderboards.

We're paying these costs deliberately.

---

## How We'd Know We're Wrong

The thesis is falsifiable:

- **If a Bourdon-equipped agent still fails the 2026-04-19 test** —
  user asks "What is X?", agent searches before answering — after we
  ship the recognition-first runtime path (the open work), then the
  architecture is wrong, not just the implementation. Either the
  L0/L1 split doesn't carry enough recognition signal, or the
  parallelization model doesn't actually buy meaningful hydration
  time, or the cognition analogy doesn't translate to LLM token
  generation rhythm. In any of those cases, the thesis revises or
  retires.

- **If a competing project ships recognition-first behavior using a
  different architecture** (e.g., not L0/L1, not timing-orchestrated,
  but achieving the same felt result), we adopt their approach and
  document the loss. Bourdon's wedge is the *behavior*, not the
  specific architecture.

- **If the felt difference doesn't matter to users** — if rigorous
  blind testing shows people genuinely cannot distinguish a
  recognition-first agent from a retrieve-then-respond agent at
  comparable retrieval quality — then the entire wedge collapses
  and Bourdon should be folded back into one of the existing
  frameworks as a runtime helper rather than a standalone product.

We are not in any of those states yet. The recognition-first runtime
work is the next code item that will let us actually run the
falsification.

---

## What's Next (Tracking Toward the Test)

Work needed to make the thesis testable in daily use:

1. **`core/recognition_runtime.py`** — the runtime path that emits an
   immediate recognition sentence using L0 only, while L1 hydrates in
   parallel. Tracked in
   [FINDINGS_JOURNAL.md](FINDINGS_JOURNAL.md#actions).

2. **L0 entity alias map** — names like "OMNIVour", "Prun",
   "Coolculator" should resolve immediately to canonical project
   identities with no lookup pause.

3. **Recognition-first eval harness** — `bourdon codex eval
   --recognition` should score response *shape* and *latency*, not
   just entity correctness. Without this, we can't measure progress
   against the thesis.

4. **Auto-fire on session close** — the Claude Code participant exists
   and reads memory correctly; the missing piece is wiring it to
   emit L5 to `~/agent-library/` automatically at session end so
   the federation populates without manual `bourdon codex export`
   calls.

These four items, together, complete the loop that makes the thesis
runnable in normal daily work. Until they ship, Bourdon has the right
shape but not yet the proof.

---

## Provenance

- Thesis origin: 2026-04-14 design conversation, archived in
  [THESIS.md](THESIS.md).
- First explicit gap-finding: 2026-04-19 Codex live-test session,
  [FINDINGS_JOURNAL.md](FINDINGS_JOURNAL.md).
- Field landscape sweep that confirmed white space:
  [FINDINGS_JOURNAL.md, 2026-04-22 entry](FINDINGS_JOURNAL.md).
- This positioning doc: 2026-04-27.

If a future commit ships a feature that conflicts with one of the
claims in this document, the commit should either revise the claim
explicitly (with the reason) or justify the feature on a different
basis.
