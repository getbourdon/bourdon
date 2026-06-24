# Bourdon — Related Work

**Status:** v0.1, 2026-04-27. Reference document.

This file maps Bourdon's vocabulary to the wider agent-memory field so
contributors and evaluators can locate Bourdon on the map. We try to
adopt established terminology where it fits, and explicitly diverge
where our timing-first frame demands it. See [POSITIONING.md](POSITIONING.md)
for what we're staking; this document is purely descriptive.

---

## Vocabulary Cross-Reference

How Bourdon's terms map to academic + industry vocabulary:

| Bourdon term | Closest academic / industry analogue | Source |
|---|---|---|
| **L0 Hot Cache** | "Primary abstractions" (Memora); "always-loaded context" (LinkedIn CMA); "system-prompt context" (general) | [Memora](https://hf.co/papers/2602.03315) |
| **L1 Entity Synopses** | "Cue anchors" (Memora); "memory blocks" (Letta); "factual memory" (Memory in the Age of AI Agents survey) | [Memora](https://hf.co/papers/2602.03315), [Letta](https://www.letta.com/) |
| **L2 Episodic Memory** | "Episodic memory" (universal cognitive-science term used by every survey); "experiential memory" (2025 survey) | [Memory in the Age of AI Agents](https://hf.co/papers/2512.13564) |
| **L3 Indexed History** | "Long-term memory" / "RAG corpus" (most frameworks) | (general) |
| **L4 Raw Archive** | "Latent memory" or "parametric memory" depending on framing | [Memory in the Age of AI Agents](https://hf.co/papers/2512.13564) |
| **L5 Agent Memory Manifest** | "Agent profile" (Intrinsic Memory Agents); "memory passport" (informal) | [Intrinsic Memory Agents](https://hf.co/papers/2508.08997) |
| **L6 Federation Library** | "Shared Context Store (SCS)" (formal MCP proposal); "memory mesh" (informal) | [SCS proposal — arXiv 2601.11595](https://arxiv.org/abs/2601.11595) |
| **role_narrative** | "Role-aligned memory block" (Intrinsic Memory Agents) | [Intrinsic Memory Agents](https://hf.co/papers/2508.08997) |
| **valid_from / valid_to** | "Temporal validity window" (Zep Graphiti); "fact validity" (TKG literature) | [Zep / Graphiti](https://www.getzep.com/) |
| **Recognition-first runtime** *(open work)* | None established — this is the white-space we're staking | (Bourdon's own framing) |

---

## Production Frameworks

### Mem0
**Wedge:** 3-tier scope (user / session / agent), hybrid vector + graph + KV
store, mature compliance posture (SOC 2 Type II, HIPAA), large community.

**What we adopt:** Nothing yet. The 3-tier scope is well-designed but
orthogonal to Bourdon's L0-L6 split — they answer different questions
(scope is "who is this for?"; layer is "what timing budget does this
fit in?"). We could add Mem0-style scope tags as an optional Entity
field in a future spec revision.

**Where we diverge:** Mem0 is a managed cloud / SDK product; Bourdon is
a local-first OSS spec. Mem0 is paid; Bourdon is free. Mem0 ranks well
on retrieval benchmarks; Bourdon doesn't try to.

### Zep (Graphiti)
**Wedge:** Temporal knowledge graph. Every fact has a validity window
(start + end date). Best-in-class on temporal-reasoning benchmarks
(LongMemEval +15 over competitors).

**What we adopted (2026-04-27):** Temporal validity windows on Entities
(`valid_from` / `valid_to`). See `feat: add temporal validity windows`
commit `4a26533`. Our adoption is lighter than Graphiti's — we don't
build a knowledge graph, we just attach validity dates to entity rows.
This was a deliberate scope choice: graphs solve representation
richness, which is not our wedge.

**Where we diverge:** No graph engine. Our entity dedupe uses simple
case-insensitive name matching with aliases, not graph traversal.

### Letta (formerly MemGPT)
**Wedge:** First-class memory blocks, stateful runtime, developer-
controllable memory editing during conversation.

**What we adopt:** The "memory block" concept maps closely to our L1
synopses — both are explicit, named, editable per-entity context units.
Letta's edit-during-runtime model is something we should consider for
v0.1.x: today our L1 synopses are read-only at runtime; allowing the
agent to update them via a tool call would close a real gap.

**Where we diverge:** Letta focuses on memory *editability* during
runtime; we focus on memory *timing* during runtime. Different problem,
non-conflicting solutions.

### Cognee
**Wedge:** Document-to-knowledge-graph construction. Turns unstructured
data into structured, retrievable knowledge.

**What we adopt:** Nothing. Cognee solves a different problem (raw doc
ingestion); we expect participants to handle ingestion in their domain-
specific way before emitting L5.

### Supermemory / SuperLocalMemory
**Wedge:** Local-first / edge variants of agent memory. Validates that
the local-first stance has market support.

**What we adopt:** The architectural validation. Our local-first default
is the right call.

### LinkedIn Cognitive Memory Agent (CMA, Apr 2026)
**Wedge:** Production agent memory infrastructure with episodic +
semantic + procedural layers. Big-tech production scale.

**What we adopt:** The "always-loaded context" framing for L0 maps to
their always-on episodic surface. Their three-layer cognitive split
(episodic / semantic / procedural) is more rigorous than our L0-L4
numbering and we cross-reference it in the table above.

**Where we diverge:** CMA is enterprise-scale managed infrastructure;
Bourdon is a spec + reference impl. Different audience, similar
architectural intuitions.

---

## Academic Papers

### "Memory in the Age of AI Agents" (Dec 2025, 157 HF upvotes)
[hf.co/papers/2512.13564](https://hf.co/papers/2512.13564)

**Why it matters:** The canonical 2026 survey. Establishes a 6-type
taxonomy of agent memory (token-level, parametric, latent, factual,
experiential, working) that's richer than our L0-L4 numbering.

**How it maps to us:** Their taxonomy is **complementary** to our
timing-budget numbering. Bourdon's L0 holds *factual* memory, L1
holds *experiential* memory ("what we did with X"), L2-L4 hold a mix
of *factual* and *latent* memory. We don't address parametric memory
(model weights) at all. The cross-reference is in the vocabulary table
above.

### "Memora: A Harmonic Memory Representation" (Microsoft, Feb 2026)
[hf.co/papers/2602.03315](https://hf.co/papers/2602.03315)

**Why it matters:** Closest academic relative of our L0/L1 split.
Their primitives — *primary abstractions* (high-level identity) and
*cue anchors* (specific retrieval triggers) — map functionally onto
our L0 (recognition substrate) and L1 (hydration substrate). They
claim outperformance over RAG and KG-based systems on long-term
memory benchmarks.

**Where we diverge:** Memora frames the problem as **representation
balance** ("how much abstraction vs. how much specificity?"). We
frame it as **timing balance** ("which layer fits in which slice of
the response window?"). Both framings can coexist — Memora's
representation insight could inform how we choose what goes in L0
vs. L1 in a future spec revision. This is the most likely "adopt
their formalism in v0.2.x" candidate.

### "Enhancing MCP with Context-Aware Server Collaboration" / Shared Context Store (SCS)
[arXiv 2601.11595](https://arxiv.org/abs/2601.11595)

**Why it matters:** Formal academic proposal for what is functionally
our L6. Multi-agent MCP workflows where specialized servers read /
write a shared context memory.

**How it maps to us:** L6 ≈ SCS. Same data shape, same coordination
goal. We use the term "L6 Federation Library" because it fits our
numbered timing model; if SCS terminology becomes the established
norm we will document the equivalence here and adopt it where the
overlap is exact. Our `role_narrative` field is an addition that
SCS doesn't formalize.

### "Intrinsic Memory Agents" (Yuen et al., Jan 2026)
[hf.co/papers/2508.08997](https://hf.co/papers/2508.08997)

**Why it matters:** Direct inspiration for our `role_narrative`
field. Shows that role-aligned memory blocks reduce role drift in
heterogeneous multi-agent systems.

**What we adopted (2026-04-27):** `agent.role_narrative` (commits
`798836f` + `301d002`). Lighter-weight than their full role-template
system — one optional free-text field rather than structured role
constraints — but enough to inform L6 cross-agent query routing
("which agent should I ask about X?").

### "G-Memory: Tracing Hierarchical Memory for Multi-Agent Systems" (Jun 2025)
[hf.co/papers/2506.07398](https://hf.co/papers/2506.07398)

**Why it matters:** Hierarchical memory across multiple agents using
insight graphs + query graphs + interaction graphs. Closest existing
work to our L5 + L6 federation.

**How it maps to us:** G-Memory's interaction graph ≈ L6 cross-agent
session timeline; their insight graph ≈ L5 known_entities; their
query graph is something we don't yet have — it's a structured
representation of pending queries that propagate across agents.
Worth considering as a future v0.2.x feature.

### "H-MEM: Hierarchical Memory for High-Efficiency Long-Term Reasoning" (Jul 2025)
[hf.co/papers/2507.22925](https://hf.co/papers/2507.22925)

**Why it matters:** Vector + positional index encoding for hierarchical
memory. Architecturally similar to our L0 → L1 → L2 routing pattern.

**What we adopt:** Nothing direct. Our L0/L1 routing uses keyword
detection rather than vector similarity, which is a deliberate choice
(L0 must be O(1) latency for the recognition-first model to work).
H-MEM's vector indexing applies more naturally at L2 / L3.

---

## MCP Roadmap Alignment

The [Model Context Protocol 2026 roadmap](https://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/)
focuses on **transport scalability, agent communication, governance
maturation, and enterprise readiness.** Specific sponsored work:

- **SEP-1932 (DPoP)** — proof-of-possession tokens for transport
  authentication
- **SEP-1933 (Workload Identity Federation)** — cross-org agent identity

**Our alignment:** Bourdon's L6 server is built on `fastmcp` and
exposes resources / tools per MCP convention. We do not yet implement
DPoP or Workload Identity Federation; those are enterprise concerns
out of scope for our pre-alpha phase. Our spec choices try not to
contradict the roadmap (e.g., our access_level model uses
`public / team / private` which is compatible with SEP-1933's
identity scopes).

---

## Where Bourdon Diverges Intentionally

The recurring theme: Bourdon's wedge is **runtime interaction timing**,
not memory representation or retrieval quality. So we deliberately do
*not* compete with the field on:

- **Retrieval benchmarks (LongMemEval, MMLU-style memory recall).** We
  don't optimize for these. If we ranked, that would be a side effect
  of integration with backends like UltraRAG (L2), not a primary goal.
- **Knowledge graph richness.** We use simple entity rows. KG
  integrations are something Bourdon could consume from (via participants
  emitting graph-shaped Entities) but we don't build a graph engine.
- **Compliance certifications.** Our local-first, free-OSS posture
  defers SOC 2 / HIPAA to whoever wants to take Bourdon to enterprise.
- **Managed cloud.** No hosted Bourdon. Anyone can self-host the L6
  server; that's the deployment model.

The corresponding *positive* commitment is that Bourdon focuses on the
runtime-shape problem ([POSITIONING.md](POSITIONING.md)) and treats every
adoption from this list as a measurable improvement on top of an existing
field standard, not a replacement for it.

---

## Provenance

This document is the result of a 2026-04-22 / 2026-04-27 landscape sweep
documented in the second entry of [FINDINGS_JOURNAL.md](FINDINGS_JOURNAL.md).
It will be updated as the field evolves and as Bourdon's own
implementation reveals new alignments / divergences.
