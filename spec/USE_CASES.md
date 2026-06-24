# NeuroLayer Use Cases

**Purpose:** Ground the universal-infrastructure claim in concrete scenarios across domains. Each shows the same memory architecture (L0-L6) applied to a different knowledge-work context, with different entity types and visibility policies — but the same underlying cognition model.

If the thesis (`THESIS.md`) is right — that NeuroLayer is not a dev tool but a substrate for human-AI collaboration — these use cases should all work on the same primitives with minimal domain-specific wiring.

---

## 1. Developer Workflow (Reference Case)

**Scenario:** Ryan opens Claude Code in the morning after a Codex session the night before.

**Without NeuroLayer:** Claude Code has no idea what Codex was doing. Ryan re-explains: "I was debugging the ILTT auth middleware, found the issue in server/auth.ts, haven't fixed it yet."

**With NeuroLayer:** L6 query on session start: *"what was touched in the last 24 hours?"* Returns Codex's session summary — file paths, branch state, last thread_name, open TODO comments. Claude Code opens with: *"Picking up from last night's Codex session — you narrowed the auth middleware bug to server/auth.ts line 87, not fixed yet. Want to continue there?"*

**Entities in L5:** `project`, `file`, `function`, `bug`, `commit`, `tool`

---

## 2. Customer Support Operations

**Scenario:** An ILTT personal trainer emails support at 2am: "my client can't see their workout plan."

**Without NeuroLayer:** Support agent (human or AI) opens 5 tabs — Stripe, RevenueCat, Attio CRM, ILTT backend admin, Slack history — reconstructs context over 10 minutes.

**With NeuroLayer:** Support AI queries L6: *"give me everything about trainer_id=12345."* Federated response: RevenueCat sub history, Attio account notes, Linear tickets filed by or about this trainer, Slack threads where the trainer was mentioned, past support interactions. One query, complete picture, sub-second.

**Entities in L5:** `customer`, `subscription`, `ticket`, `interaction`, `feature_request`, `bug`
**Critical:** PII visibility policy enforced strictly — `private` tag on all customer identifiers; L6 never replicates off-prem.

---

## 3. Scientific Research — Chemistry Lab

**Scenario:** Researcher returns to an AI-assisted synthesis project after a two-week gap. Three other team members also contributed sessions.

**Without NeuroLayer:** Every session starts with re-loading the research context — what reactions were tried, what worked, what failed and why, what conventions the team has agreed on for naming intermediates.

**With NeuroLayer:** L0 hot cache holds reaction nomenclature + active hypothesis + safety constraints. L1 synopses per reactant/intermediate. L6 federates all team members' sessions — when Researcher A comes back, they see what Researchers B and C tried while A was out, with the decision rationale preserved.

**Entities in L5:** `compound`, `reaction`, `hypothesis`, `result`, `safety_constraint`, `team_member`

---

## 4. Creative Writing — Poetry

**Scenario:** Poet iterates on a 14-line sonnet over 6 sessions across 3 weeks. Earlier drafts, rejected images, aesthetic choices, and the emotional register the poem is reaching toward — all need to persist.

**Without NeuroLayer:** Every session the poet re-explains the aesthetic: "no Latinate diction, no abstractions in the final couplet, the central image is a pier at dusk." The AI forgets each time.

**With NeuroLayer:** L0 hot cache holds the poem's working title + central image + prohibited register. L1 synopsis captures current draft + rejected lines + the *why* behind each rejection. L2 surfaces past iterations on the specific stanza being worked. The AI remembers the *taste* of the project, not just its content.

**Entities in L5:** `draft`, `line`, `image`, `rejected_choice`, `aesthetic_constraint`, `influence`

---

## 5. Architecture — Site Design

**Scenario:** Architect has 8 active client projects, each with unique site constraints (zoning, topology, budget, style preferences), and is discussing one of them with a client for the third time.

**Without NeuroLayer:** The AI assistant knows nothing about the specific client, has to be re-briefed on constraints, cannot catch the architect if they propose something that contradicts a previous client request.

**With NeuroLayer:** L1 synopsis per client + project. L6 federates across past design reviews, client email threads (via participant), Notion project notes (via participant). The AI catches: *"The client said in the March 12 meeting they didn't want south-facing glazing because of the neighbor's pool. This plan adds a south-facing wall of windows."*

**Entities in L5:** `client`, `project`, `site`, `constraint`, `decision`, `meeting`

---

## 6. Physics Research — Theory Development

**Scenario:** Physicist developing a theoretical model, working with AI across dozens of sessions, notation conventions specific to their approach.

**Without NeuroLayer:** Every new session requires reloading: which symbols mean what, which derivations have been completed, which branches of the theory were explored and rejected.

**With NeuroLayer:** L0 holds notation dictionary + active branch of the theory. L1 synopses per concept (fields, boundary conditions, proposed mechanisms). L2 retrieves past derivations on demand. The AI never asks "what does σ mean in this context" after session 1.

**Entities in L5:** `concept`, `notation`, `derivation`, `branch`, `open_question`, `counterexample`

---

## 7. Project Management — Cross-Team Decision Tracking

**Scenario:** PM at a company with 40 engineers, 5 products, 12 active initiatives. Decisions happen in Slack, Linear, Notion, and meetings. Six months later, someone asks "why did we go with Postgres for the analytics pipeline?"

**Without NeuroLayer:** PM searches Slack, maybe finds a thread, maybe not. Linear ticket exists but doesn't capture the reasoning. Notion doc is 3 versions old.

**With NeuroLayer:** L6 federates Slack + Linear + Notion + meeting transcripts. Query: *"why did we choose Postgres for analytics?"* Returns the decision thread, the considered alternatives, the stakeholders who weighed in, the date it was locked.

**Entities in L5:** `decision`, `stakeholder`, `alternative`, `initiative`, `artifact`

---

## 8. Education — Continuous Tutoring

**Scenario:** Student working with an AI tutor across a semester. Past misconceptions, struggled topics, learning style preferences, and pace need to persist.

**Without NeuroLayer:** Tutor forgets every session that the student confuses derivative with integral and that they learn better through physical analogies than algebraic manipulation.

**With NeuroLayer:** L1 synopsis tracks the student's learning model — known strengths, active gaps, effective explanation styles. L2 surfaces past worked examples on the exact concept being taught. The tutor adapts to the individual, not a generic profile.

**Entities in L5:** `concept`, `misconception`, `worked_example`, `learning_style`, `pace_indicator`

---

## Pattern Across All Cases

Look at the entity types column by column. They differ by domain — `compound` vs. `line` vs. `decision` vs. `misconception` — but they play the same structural roles:

- **Some entities are persistent** (projects, clients, concepts) — live in L0/L1
- **Some are event-like** (decisions, reactions, drafts) — live in L2/L3
- **Some are references** (files, lines, citations) — live in L3/L4
- **Some need privacy** (customer PII, personal aesthetic choices) — visibility `private`
- **Some benefit from federation** (decisions across stakeholders, findings across researchers) — visibility `team` or `public`

The architecture doesn't need to know what a *compound* is or what a *poem line* is. It only needs to know: this entity persists, this one is event-like, this one is private, this one is shared. The domain-specific meaning lives in the user's head and in L5's free-text summaries. The memory layer stays universal.

**This is why NeuroLayer works as infrastructure**: it doesn't model the content; it models the cognition. Content is always domain-specific. Cognition is universal.

---

## Not Yet Addressed

Use cases that feel like they should work but haven't been tested:

- **Law (case law + client matters)** — privilege + PII concerns likely dominate design
- **Medicine (patient care across providers)** — HIPAA implications, probably needs separate compliance-first deployment
- **Journalism (source protection + story development)** — visibility policy must handle source anonymity guarantees

These are deliberate v2+ considerations. The core architecture should accommodate them, but the policy + compliance work is domain-specific and significant.

---

*Updated 2026-04-14 alongside THESIS.md.*
