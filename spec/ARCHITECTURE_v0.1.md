# Clyde Memory Architecture v1
### Tiered Human-Inspired Memory for Local AI Systems
**Author:** Ryan Davis — RADLAB LLC  
**Project:** Clyde (Local AI Swarm — OpenAI Agents SDK + Ollama)  
**Status:** Draft v1.0 — April 14, 2026  
**Classification:** RADLAB Internal / Future Open Source

---

## The Core Insight

Every RAG system ever built shares the same fatal flaw: it retrieves *after* the moment, not *during* it. The human asks something, the system stops, digs, returns. That pause — however brief — breaks the illusion of a mind.

Humans don't retrieve. They *recognize*. Hearing a word activates a web of associations passively before conscious recall begins. Details surface as the conversation continues — not before it.

This architecture is the engineering translation of that biological reality.

---

## 1. Problem Statement

Current AI memory systems — including UltraRAG 3.0, ClawXMemory, and all OpenClaw memory plugins — are fundamentally **retrieve-then-respond** architectures. They are well-engineered archives with fast lookup. They are not minds.

The gap: no existing system parallelizes memory retrieval with conversation rhythm. Every implementation blocks on retrieval before responding. This creates a felt discontinuity — the AI either pauses awkwardly, or responds without context, or surfaces stale information after the moment has passed.

The result: interactions that feel like talking to a search engine, not a collaborator who knows you.

**Who experiences this:** Any developer building local AI agents on Ollama, LangChain, OpenAI Agents SDK, or UltraRAG. Any end user interacting with a memory-equipped AI assistant. Experienced daily, on every session start and context switch.

**Cost of not solving it:** AI assistants remain perpetually amnesiac at the conversational level even when technically equipped with memory. Adoption stalls. Trust fails to build. The human/AI collaboration loop never closes.

---

## 2. Goals

**User Goals:**
- AI feels like it *recognizes* the user and their context — not like it looked them up
- Conversation flows without retrieval pauses or cold-start awkwardness
- Deeper context surfaces naturally as conversation develops, not all at once

**System Goals:**
- L0 + L1 memory layer fits within 15K tokens — compatible with any model running 32K+ context
- L2 retrieval completes during natural human response time (~3–8 seconds)
- Architecture is model-agnostic — any Ollama-compatible model, any inference backend

**Business / Product Goals:**
- Clyde becomes the first local AI system with human-paced memory feel
- Architecture is extractable as a standalone open source package (NeuroLayer)
- Compatible with UltraRAG 3.0 as the retrieval backend for L2–L4

---

## 3. Non-Goals (v1)

- **Not a replacement for UltraRAG** — this is an orchestration layer on top of it, not a competing retrieval system
- **Not cloud-dependent** — v1 is fully local; cloud API compatibility is a v2 abstraction
- **Not a training/fine-tuning system** — memory is runtime context injection, not weight modification
- **Not a universal plugin for all frameworks yet** — Clyde/Ollama first; OpenAI Agents SDK, LangChain, CrewAI are v2 targets
- **Not a UI product** — v1 is infrastructure; developer-facing only

---

## 4. The Memory Layers

### L0 — Hot Cache (Always In Context)
**What it is:** A compact, always-loaded payload injected permanently into the system prompt.  
**What it contains:** Keywords, names, project slugs, active status flags, key dates.  
**Size target:** 2–3K tokens maximum.  
**How it loads:** Static. Always present. Never retrieved.  
**Human analog:** The things you simply *know* without thinking — your name, your job, your current project.

```
Example L0 payload:
ENTITIES: Ryan, RADLAB, ILTT, Clyde, Coolculator, Shipstable, A100
PROJECTS_ACTIVE: ILTT(submitted_app_stores), Clyde(memory_upgrade), Coolculator(naming)
LAST_SESSION: 2026-04-14 | topic: memory_architecture
HARDWARE: A100_PCIe_80GB(evaluating), Gemma3_27B_Q(running)
```

**Why this works:** L0 fires the instant any entity name appears in a user message. The AI recognizes — it doesn't lookup.

---

### L1 — Entity Synopses (Triggered by L0 Hit)
**What it is:** A tight synopsis per recognized entity — what it is, current status, last touched, key decisions.  
**What it contains:** ~300–500 tokens per entity, pre-built and cached.  
**Size target:** 10–15K tokens for ~20–30 active entities.  
**How it loads:** Fires immediately when an L0 keyword is detected in the incoming message. Loads *while* the model begins formulating the opening response.  
**Human analog:** The immediate recall that follows recognition — "Oh yeah, Clyde — that's Ryan's local AI swarm, he was just upgrading the memory layer."

```
Example L1 synopsis for "Clyde":
Clyde | RADLAB project | Local AI swarm
Stack: OpenAI Agents SDK + Ollama + UltraRAG 3.0
Model: Gemma 3 27B quantized (128K context)
Status: Active — upgrading memory architecture (this session)
Last updated: 2026-04-14
Key context: Primary motivation for local compute hardware research (A100 eval)
```

---

### L2 — Episodic Memory (Triggered by Conversation Direction)
**What it is:** Topic/project/person indexed summaries. Richer session history per entity.  
**What it contains:** What was discussed, decisions made, open threads, chronological arc.  
**How it loads:** Fires during the human's response to the L1-informed message. By the time they've finished typing, L2 is ready.  
**Retrieval backend:** UltraRAG 3.0  
**Human analog:** Remembering the specifics — "Last week we decided on the commission structure, and he was looking at the A100 for compute..."

---

### L3 — Indexed Session History
**What it is:** Searchable session logs, timestamped and indexed by entity/topic.  
**How it loads:** On-demand only. Triggered when conversation requires specifics — exact wording, specific decisions, dates.  
**Retrieval backend:** UltraRAG 3.0 with vector + BM25 hybrid search  
**Human analog:** Consciously trying to remember something specific — "What exactly did we decide about the pricing model?"

---

### L4 — Raw Archive
**What it is:** Complete verbatim conversation history. The ground truth.  
**How it loads:** Rarely. Only when exact reproduction is needed.  
**Human analog:** Looking something up in your notes. Slow but authoritative.

---

## 5. The Timing Model (The Innovation)

This is what separates this architecture from every existing RAG system.

```
User sends message
       │
       ▼
L0 fires instantly ──────────────────────────────► Recognition response begins
       │                                            ("Oh yeah, Clyde...")
       ▼
L1 loads in parallel ───────────────────────────► Enriched context ready by
       │                                            sentence 2 of response
       ▼
L2 retrieval fires ─────────────────────────────► Completes during human
       │                                            reading + typing (~3-8s)
       ▼
Human responds ──────────────────────────────────► L2 context fully available
       │                                            for follow-up response
       ▼
L3/L4 only if needed ───────────────────────────► Triggered by explicit
                                                    depth requirement
```

**The key:** Each layer buys exactly the time needed for the next layer to complete. Retrieval happens *during* conversation rhythm, not *blocking* it.

---

## 6. User Stories

**As a developer running Clyde**, I want Clyde to immediately recognize my projects and active context when I start a session so that I don't spend the first 5 minutes re-explaining where we left off.

**As a developer building on Ollama**, I want the memory layer to work with any model I'm running so that I'm not locked into a specific LLM for memory functionality.

**As Ryan using Clyde daily**, I want Clyde to respond to "let's work on ILTT" with immediate contextual awareness — current status, last session, open threads — without me having to ask it to "remember" anything.

**As a developer**, I want L0 to be human-readable and editable so that I can manually tune what's always in context without touching code.

**As a future open source user**, I want to drop this memory layer into my existing UltraRAG setup with minimal configuration so that I get human-feel memory without rebuilding my stack.

---

## 7. Requirements

### P0 — Must Have (v1 ships with these)

- **L0 Hot Cache loader** — reads a structured YAML/JSON file, injects into system prompt on every session start. Max 3K tokens enforced.
- **L0 keyword detector** — scans incoming user message for L0 entity matches before model call begins
- **L1 synopsis store** — flat file store (Markdown per entity) with auto-inject on L0 hit
- **L1 parallel loader** — fires L1 retrieval concurrent with initial model response generation, not before
- **L0/L1 combined token budget enforcer** — hard cap at 15K tokens, drops lowest-priority entities if exceeded
- **Ollama compatibility** — works with any model served via Ollama REST API (`localhost:11434`)
- **UltraRAG 3.0 integration** — L2 queries route to UltraRAG retrieval pipeline
- **Session logger** — auto-captures session summary to L3 index after conversation ends

### P1 — Nice to Have (fast follows)

- **L1 auto-updater** — model-generated synopsis refresh after each session
- **L0 keyword suggester** — analyzes recent sessions to suggest new L0 entities
- **L2 completion signal** — notifies when L2 retrieval is ready so response can reference it
- **Token usage telemetry** — reports per-layer token consumption per session
- **CLI management tool** — `clyde-memory add`, `clyde-memory status`, `clyde-memory prune`

### P2 — Future / Architectural Insurance

- **Provider abstraction layer** — swap Ollama for vLLM, OpenAI API, Anthropic API via config
- **Framework participants** — LangChain, CrewAI, OpenAI Agents SDK, LangGraph
- **L0 auto-generation** — build L0 payload from L1/L2 automatically, no manual maintenance
- **Multi-user support** — separate L0/L1 namespaces per user identity
- **NeuroLayer SDK** — packaged open source release with pip/npm install

---

## 8. Technical Architecture

### File Structure (v1)
```
clyde-memory/
├── l0/
│   └── hot_cache.yaml          # Always-loaded keywords + entity flags
├── l1/
│   ├── ILTT.md                 # Synopsis per entity
│   ├── Clyde.md
│   ├── Ryan.md
│   └── ...
├── l2/
│   └── ultrarag/               # UltraRAG knowledge base (existing)
├── l3/
│   └── sessions/               # Indexed session logs YYYY-MM-DD.md
├── l4/
│   └── archive/                # Raw conversation exports
└── config.yaml                 # Token budgets, model settings, layer config
```

### System Prompt Injection Order
```
[SYSTEM PROMPT]
  1. Base persona / instructions
  2. L0 hot cache (always)
  3. L1 synopses for detected entities (parallel loaded)
  4. L2 episodic context (if loaded in time)
[USER MESSAGE]
```

### Ollama Integration
```python
# Pseudocode — memory orchestrator
async def handle_message(user_message: str) -> str:
    
    # L0 always present — no async needed
    l0_context = load_l0_cache()  
    
    # L1 fires immediately on keyword detection
    entities = detect_entities(user_message, l0_context)
    l1_task = asyncio.create_task(load_l1_synopses(entities))
    
    # L2 fires in parallel — may not complete before first response
    l2_task = asyncio.create_task(query_ultrarag(user_message))
    
    # Build initial context with whatever is ready
    l1_context = await asyncio.wait_for(l1_task, timeout=1.0)
    
    # First response uses L0 + L1
    system_prompt = build_system_prompt(l0_context, l1_context)
    
    # L2 available for follow-up turns
    l2_context = await l2_task  # completes during human response time
    
    return await ollama_chat(system_prompt, user_message)
```

---

## 9. Success Metrics

| Metric | Baseline (current) | Target (v1) | Measurement |
|--------|-------------------|-------------|-------------|
| Session cold-start re-explanation time | ~5 min | < 30 sec | Subjective eval |
| L0+L1 token footprint | N/A | < 15K tokens | Token counter |
| L1 load latency | N/A | < 1 second | Timer log |
| L2 retrieval completes before 2nd human message | 0% | > 90% | Session logs |
| Model compatibility | Clyde/Gemma3 only | Any Ollama model | Integration tests |
| "Feels like recognition" rating | 0 | 8/10 subjective | Dev eval sessions |

---

## 10. Open Questions

| Question | Owner | Blocking? |
|----------|-------|-----------|
| What's the right L0 entity limit before it feels noisy? | Ryan (eval) | No |
| Should L1 synopses be human-written or auto-generated by model? | Ryan + eng | Yes — v1 decision |
| Does UltraRAG 3.0's MCP architecture support async parallel queries? | Eng research | Yes — needed for L2 timing |
| What happens when L2 isn't ready before the human's second message? | Eng | No — graceful degradation |
| Token budget enforcement — hard cap or soft warning? | Ryan | No |
| Should L0 be version-controlled in claude-brain? | Ryan | No |

---

## 11. Phasing

**Phase 1 — Clyde MVP (2–3 weeks)**  
L0 hot cache + L1 entity synopses. Manual file maintenance. Ollama only. Proves the *feel* before building automation.

**Phase 2 — Full Stack (4–6 weeks)**  
L2 UltraRAG integration with async timing. L3 session logging. Auto L1 refresh. CLI tools.

**Phase 3 — NeuroLayer (TBD)**  
Provider abstraction. Framework participants. Open source packaging. Documentation site.

---

## 12. The Bigger Picture

This architecture is not just a Clyde feature. It is the missing layer in every local AI stack.

The pattern: **model-agnostic, timing-aware, tiered memory orchestration**.

UltraRAG handles retrieval mechanics. ClawXMemory handles storage. NeuroLayer handles the *feel* — the parallelized, rhythm-aware, recognition-before-lookup layer that makes AI collaboration feel like working with someone who actually knows you.

No one has built this yet. The field is moving (ClawXMemory, openclaw-memory-hierarchical, memory-lancedb-pro all shipped in the last two weeks). But none of them have the timing model.

That is the RADLAB contribution.

---

*Spec authored in collaboration with Claude (Anthropic) — April 14, 2026*  
*Save to: claude-brain/PROJECTS/Clyde/MEMORY_ARCHITECTURE_v1.md*
