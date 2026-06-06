# Findings Journal

This journal records live findings from Bourdon implementation and testing.
It is intended to capture the gap between what the system can technically
retrieve and how naturally it behaves in the first moments of interaction.

---

## 2026-04-19 - Codex Recognition-First Gap

### Context
- Objective: test whether the Codex-specific Bourdon integration produces
  recognition-first behavior, not just better retrieval.
- Systems: Codex participant, `bourdon codex eval --live`, live `~/.codex`
  ingestion, explicit `claude-brain` overlay ingest.
- Constraint: the first response needs to feel like human recognition while
  deeper context hydrates in parallel.

### Timeline
- Generic Codex Bourdon integration was implemented, verified, committed, and
  pushed.
- Live eval against real Codex memory was tightened until project extraction
  became sane and obvious garbage entities were removed.
- Explicit ingest against `C:\Users\cumul\claude-brain` succeeded and pulled a
  large amount of real project context.
- User tested the behavior with a recognition-style question: `What do you know
  about OMNIvour?`
- Codex responded too much like a retrieval system: it searched first, then
  answered from gathered notes.
- User clarified the intended NeuroLayer/Bourdon behavior:
  first-layer keyword recognition should support an immediate natural response,
  while deeper layers hydrate project summaries, dates, milestones, and archived
  history in the background.

### Decisions
- Decision: treat this as a real product finding, not user preference noise.
  Rationale: this behavior is central to the thesis and to the product promise.
  Impact: runtime behavior now becomes a first-class workstream, not just data
  ingestion quality.
- Decision: distinguish `data-layer success` from `behavior-layer success`.
  Rationale: Bourdon can ingest and structure memory correctly while still
  feeling like lookup.
  Impact: future eval must score both context quality and response timing style.
- Decision: record recognition failures as journal artifacts.
  Rationale: these misses are high-value evidence for the thesis and the next
  implementation phase.
  Impact: this journal becomes the running record of cognition-timing findings.

### Actions
- Owner: Codex
  Action: create a recognition-first runtime path for Codex Bourdon.
  Status: next
- Owner: Codex
  Action: add an L0 entity alias map so names like `OMNIVour`, `Prun`, and
  `Coolculator` resolve immediately to known project identities.
  Status: next
- Owner: Codex
  Action: add automatic L1 hydration so a recognized entity can immediately
  surface repo path, status, and key milestones without a visible lookup pause.
  Status: next
- Owner: Codex
  Action: add prompt-eval cases for recognition-first questions such as
  `What is OMNIVour?`, `What is Prun?`, and `Remind me what Coolculator is`.
  Status: next
- Owner: User + Codex
  Action: continue live conversational testing to pressure-test whether the
  first response moment feels natural.
  Status: active

### Open Questions
- What is the minimum L0 payload required for natural recognition without
  overstuffing the hot context?
- Should the first sentence be generated from a fixed recognition scaffold or a
  more flexible project-card format?
- How should runtime behavior decide when to stay at L1 versus descend into L2/L3
  archive retrieval?
- Should recognition-first eval be measured only by entity correctness, or also
  by response shape and latency?

### Working Answers
- L0 payload budget:
  target roughly 500 tokens, with an upper bound around 2000 tokens.
  This reflects the original design intuition that L0 must stay small enough to
  feel immediate while still carrying enough identity signal for recognition.
- First-response shape:
  prefer a flexible project-card format over a fixed recognition scaffold.
  The first sentence should still feel natural, but the underlying shape should
  support a compact identity + status + path + key-context payload rather than a
  rigid template.
- L1 versus L2/L3 descent:
  use a conditional runtime rule.
  If the system already has enough context to answer, answer with what it has.
  During that same window, continue looking up deeper context in parallel.
  Descend into archive/history only when the answer needs more support or the
  user asks for more depth.
- Recognition-first evaluation:
  measure both correctness and interaction quality.
  Entity correctness alone is not enough; evaluation should also score response
  shape and latency, because the thesis depends on the interaction feeling
  natural in the first moments.

### Risks
- The system may continue to optimize for retrieval quality while missing the
  human-interface timing problem the thesis is actually about.
  Note: this is the primary failure mode to watch, because it would let
  Bourdon become a better database without becoming a more natural interface.
- Overfitting project extraction without a recognition runtime could create a
  stronger database that still feels clunky in use.
  Note: this is a good near-term warning sign for implementation work. Better
  entity extraction alone is not success if the first response moment still
  feels like lookup.
- Too much L0 context may improve recognition but degrade responsiveness or make
  the hot cache noisy.
  Note: this was a known pressure from the start, not a surprise regression. The
  open problem is to find the minimum hot context that still supports natural
  recognition.
- If recognition and hydration are not clearly separated, future debugging will
  blur data issues and runtime-behavior issues together.
  Note: this is likely a longer-term architecture concern. The system will need
  a stable boundary between `entity recognized`, `project summary hydrated`, and
  `archive descended` so future debugging can isolate data quality from runtime
  timing behavior.

### Refinements
- User refinement: the goal is not simply to reduce clunkiness in a general
  sense, but to make the first moments of human-AI interaction feel casual and
  natural enough that the interface stops feeling like staged call-and-response.
- User refinement: the recognition moment should buy time for deeper layers to
  arrive, in the same way human cognition often starts with recognition before
  details are consciously retrieved.
- User refinement: solving the `recognition vs hydration` boundary is part of
  the product architecture, not just implementation polish.

### Next Session Bootstrap
- Start with the recognition-first runtime path, not more participant ingestion work.
- Use OMNIVour as the canonical test case for the first-response behavior.
- Use the current working defaults:
  L0 target around 500 tokens, cap around 2000;
  flexible project-card first response;
  conditional L1-to-L2/L3 descent;
  eval on correctness + response shape + latency.
- Preserve the distinction:
  `I know what this is` should happen first,
  `here are the deeper details` should arrive immediately after,
  `archive descent` should only happen when needed.
- Treat the target behavior as:
  recognition first,
  hydration second,
  archive third.

## 2026-04-22 - Role Narrative + Landscape Sweep

### Context
- Resumed Bourdon work after a brief gap. Goal: review the project, sweep the
  agent-memory field for new findings, and formulate a small adoption plan.
- Picked the smallest plan item first: add `agent.role_narrative` to L5.

### Landscape findings (2026-04-22 sweep)
- Mature frameworks: Mem0 (3-tier scope, hybrid store, SOC 2), Zep (Graphiti,
  temporal validity windows), Letta (memory blocks, stateful runtime), Cognee
  (doc -> KG), Supermemory / SuperLocalMemory (local-first variants).
- Notable academic work:
  - Memora -- "primary abstractions + cue anchors" (Microsoft, Feb 2026,
    hf.co/papers/2602.03315). Structurally close to our L0/L1 split, framed
    as a representation problem rather than a runtime-timing problem.
  - Memory in the Age of AI Agents -- canonical 2025-2026 survey
    (hf.co/papers/2512.13564, 157 upvotes). Six memory types: token-level,
    parametric, latent, factual, experiential, working.
  - Enhancing MCP with Context-Aware Server Collaboration (arXiv 2601.11595)
    -- formalizes "Shared Context Store (SCS)" -- functionally our L6.
  - Intrinsic Memory Agents (hf.co/papers/2508.08997, Jan 2026) -- shows that
    role-aligned memory blocks reduce role drift in heterogeneous multi-agent
    systems. Directly inspired today's role_narrative feature.
- MCP 2026 roadmap -- transport scalability, agent communication, governance,
  enterprise readiness. SEP-1932 (DPoP) and SEP-1933 (Workload Identity
  Federation) sponsored. Our L6 stays roadmap-aligned without active work.
- No external product or paper found that frames the recognition-vs-retrieval
  *runtime timing* problem the way our 2026-04-19 entry does. White space
  remains.

### Decisions
- Decision: adopt role_narrative now, defer Memora-style abstraction/cue
  formalism to a later cycle.
  Rationale: role_narrative is one optional schema field with high
  differentiation payoff. Memora alignment is bigger surface and would
  require deeper read of the paper before commit.
  Impact: shipping role_narrative today, leaving abstraction/cue work as
  an open candidate for v0.1.x.
- Decision: do not rebuild as a knowledge graph (Cognee / Zep direction).
  Rationale: Bourdon's wedge is timing, not representation richness.
  Adding graph engine = scope blowup with no thesis payoff.
- Decision: stay aligned with MCP 2026 roadmap on terminology + governance,
  but do not preemptively implement DPoP / Workload Identity Federation.
  Rationale: those are enterprise concerns; we are still pre-alpha.

### Actions
- Owner: Claude (today)
  Action: ship `agent.role_narrative` schema + dataclass + populations in
  Claude Code and Codex participants + tests.
  Status: complete (commit 798836f)
- Owner: Claude (today)
  Action: populate role_narrative in Clyde native publisher.
  Status: next
- Owner: Future cycle
  Action: write spec/POSITIONING.md staking the recognition-first
  framing publicly before anyone else does.
  Status: open
- Owner: Future cycle
  Action: add Zep-style temporal validity windows (`valid_from` /
  `valid_to`) to L5 Entity.
  Status: open
- Owner: Future cycle
  Action: build the recognition-first runtime path
  (`core/recognition_runtime.py`) that emits an immediate recognition
  sentence while L1 hydrates in parallel.
  Status: open -- highest leverage item, deferred so we can warm up with
  smaller wins first.
- Owner: Future cycle
  Action: write spec/RELATED_WORK.md mapping our terms to Memora,
  Memory in the Age of AI Agents, SCS, G-Memory, H-MEM.
  Status: open

### Working Answers
- Role narratives differentiate within type slug: Claude Code = manager,
  Codex = lead author, Cursor = debugger, Cline = throwaway, Clyde =
  general-purpose. L6 query routing benefits immediately.
- Schema field is optional and capped at 500 chars; backwards-compatible
  with all existing manifests.

### Risks
- Recognition-first runtime work is still the headline gap. role_narrative
  is useful but does not by itself fix the lookup-feel problem. Watch for
  the trap of shipping many small wins while the central gap remains.
- Memora's framing is close enough to ours that aligning vocabulary later
  is cheap, but if a competing project picks up "abstraction + cue"
  vocabulary first, we look downstream of them.

### Next Session Bootstrap
- Either ship POSITIONING.md to stake recognition-first framing, or jump
  to recognition_runtime.py as the headline behavior fix.
- Temporal validity windows is the next-smallest concrete edit if a warm-up
  is preferred.

## 2026-04-27 - Plan Items 2/5/a/4 Shipped

### Context
- After yesterday's role_narrative work + landscape sweep, the plan
  surfaced four concrete edits: temporal validity windows (#2),
  RELATED_WORK.md (#5), auto-fire on session close (#a), and the
  recognition-first runtime (#4). Today: ship them.

### What landed

#### #2 -- Temporal validity windows (Zep Graphiti-inspired)
- Commit `4a26533`. Entity gains optional `valid_from` / `valid_to`
  ISO 8601 date fields. Claude Code participant populates `valid_to` for
  archived/canceled entities, preferring an ISO date in the
  `## Status` section over the file's mtime fallback.
- Federation queries can now answer "what was active in Q1 2026?"
  rather than just "what's in memory?". Cyndy's manifest now reads
  `valid_to: 2026-04-14` -- truthful temporal context.
- Tests +8 = 212 total.

#### #5 -- RELATED_WORK.md
- Commit `77d23ae`. Reference document mapping our L0-L6 vocabulary
  to the academic / industry landscape. Per-framework adoption notes
  (Mem0, Zep, Letta, Cognee, Supermemory, LinkedIn CMA). Per-paper
  cross-references (Memora, the 2025-2026 survey, SCS, Intrinsic
  Memory Agents, G-Memory, H-MEM). Companion to POSITIONING.md
  (commit 17e2d9f) which stakes the claim; this document maps it.

#### #a -- bourdon claude-code export (SessionEnd hook target)
- Commit `65b3ba5`. New CLI subcommand `bourdon claude-code export`.
  Designed for Claude Code SessionEnd hook use: silent on success,
  never raises, exits 0 in all observable failure modes. Default
  output path is `~/agent-library/agents/claude-code.l5.yaml`. Wire-
  up in `~/.claude/settings.json`:

  ```json
  {
    "hooks": {
      "SessionEnd": [
        { "command": "bourdon claude-code export" }
      ]
    }
  }
  ```

  Closes the operational loop: every Claude Code session close
  updates the federation library automatically.
- Tests +7 = 219 total.

#### #4 -- Recognition-first runtime
- Commit (this one). New module `core/recognition_runtime.py`
  shipping the first concrete implementation of the recognition-first
  behavior the FINDINGS_JOURNAL flagged on 2026-04-19.
- Public API: `recognition_first(user_msg, manifest, l1_dir=...,
  access_level=..., hydration_timeout=...)` -> `RecognitionResult`
  with `.recognition` (sync, immediate template-based string),
  `.matched_entities` (the entities that triggered recognition),
  and `.hydration` (an awaitable L1-hydration coroutine that runs
  in parallel with the caller's other work, with the documented 3s
  timeout budget).
- Recognition strings are deliberately template-based, NOT
  LLM-generated. Reasons: zero latency by definition, no model
  dependency, deterministic / testable, and honest to the
  architecture (L0 IS recognition; if recognition needed an LLM
  call, the layer numbering would be wrong). LLM-based generation
  can be swapped in at `build_recognition_string` later without
  changing the public API.
- Hydration runs `asyncio.gather` over per-entity L1 file reads in
  a thread pool, with `asyncio.wait_for` timeout. NEVER raises:
  timeouts and read errors return empty string so the worst case
  degrades to L0-only response.
- Visibility filter applied at dispatch via
  `filter_manifest_for_access` -- private entities cannot surface
  in recognition strings even if their name appears in the user
  message.
- Tests +25 = 244 total. Coverage: detect_entities (case-insensitive,
  alias, multi-match, no-match, defensive non-dict), recognition
  string templates (0/1/2/3+ matches, with/without type, archived
  with valid_to / via tag), hydrate_l1 (loads, missing l1_dir,
  case-insensitive filename, name-less entity), recognition_first
  full dispatch (sync recognition, no-match path, parallel
  hydration, timeout returns "", visibility filter), constants.

### Decisions
- Decision: template-based recognition string for v0.1.0. Rationale
  above. Impact: shipping the runtime today instead of debating
  which model to call. LLM-based generation is a future swap, not
  a launch blocker.
- Decision: hydration timeout default = 3.0 seconds. Rationale:
  matches the documented thesis budget (recognition fires in
  100-200ms; hydration runs concurrent with model generation
  which typically takes 2-3s; by the time the model finishes its
  first response, hydration is ready for the second turn).
- Decision: hydration NEVER raises out of `recognition_first`.
  Rationale: thesis depends on the recognition path being uncrashable.
  Caller should not need try/except around the awaitable.

### Risks
- The recognition string templates are simple. They will sound
  formulaic to users who interact with the system frequently. The
  remedy when this surfaces is the v0.2.x LLM-based generator
  swap. Hold the deterministic version until we have user feedback
  proving the formulaic feel is a problem worth fixing.
- We have not yet integrated the recognition runtime into a real
  agent's response loop. Codex's runtime path uses
  `core/codex_context.py` which builds L0 + L1 artifacts statically.
  Wiring `recognition_first` into Codex's response generation is a
  next-cycle item.

### Actions
- Owner: Future cycle
  Action: integrate `recognition_first` into Codex's runtime so
  the 2026-04-19 OMNIvour test case actually exercises the new path.
  Expected effect: response begins with "Oh -- OMNIvour, the project."
  no retrieval pause; followed by hydrated detail when the L1
  documents arrive.
  Status: open -- the runtime exists, the wiring doesn't.
- Owner: Future cycle
  Action: build `bourdon codex eval --recognition` mode that scores
  response *shape* + *latency*, not just entity correctness.
  Status: open.
- Owner: Future cycle
  Action: swap `build_recognition_string` for an LLM-based variant
  ONLY after observing whether the deterministic version feels
  formulaic in real use.
  Status: deferred -- evidence-driven, not speculative.

### Today's totals
- Bourdon: 5 commits (POSITIONING, temporal validity, RELATED_WORK,
  auto-fire, recognition runtime)
- Tests: 219 -> 244 passing (+25 in recognition runtime)
- Plan items closed: 2, 5, a, 4 (the four that came out of the
  2026-04-22 landscape sweep)
- Architecture: complete and wired in tests; integration into a
  live agent's response loop is the next experimental step.

---

## 2026-04-28 - Launch day: Twitter live, HN gated

### Context
- Objective: post the Bourdon launch publicly to convert technical
  priority (the public POSITIONING.md commit on 2026-04-22) into
  discoverable priority before any peer paper publishes the same
  framing.
- Channels planned: Twitter thread (7 tweets) + Show HN, with the
  Twitter thread firing first and HN ~30 minutes later.

### Timeline
- bourdon.ai verified live (HTTP 200, 9.2KB, ~250ms TTFB).
- Repo About sidebar populated (description + homepage + 8 topics).
- README first line hoisted to feature bourdon.ai above the fold
  (commit 801a129).
- scripts/launch.py written + shipped (commit 4b9da91): interactive
  poster with weighted-char Twitter pre-flight (URLs = 23, codepoints
  > U+10FF count as 2) and HN's 80-char title limit. Pre-flight caught
  5 of 7 tweets over the 280-weighted limit and the HN title at 89/80.
- Drafts tightened (commit 68ee421): tweets 2/3/4/6/7 trimmed,
  HN title swapped to the alternate ("Show HN: Bourdon - recognition-
  first memory for AI agents", 58 chars).
- 9am Pacific: Twitter thread posted via the X compose-stack flow
  (all 7 tweets atomic). User reports "We are live!"
- ~9:30am Pacific: HN submission attempted. Hit anti-spam gate:
  "We're temporarily restricting Show HNs because of a massive influx,
  mostly by users who aren't yet familiar with the site or its
  culture." Account-age + karma based, automatic, not personal.

### Decisions
- Decision: do NOT try to game the HN gate (different account, dropping
  Show HN prefix, etc.).
  Rationale: HN's anti-abuse posture is well-documented; gaming it
  burns the account permanently and signals exactly the kind of
  marketing-first behavior the gate exists to filter.
  Impact: HN becomes a deferred channel, not the launch's load-bearing
  signal.
- Decision: email dang/hn@ycombinator.com to request whitelist for this
  specific submission, with the actual post content inline.
  Rationale: dang is known to whitelist legitimate launches that hit
  the gate. The post is bona fide (open-source, MIT, building in
  public, asks for falsification not validation).
  Impact: HN may go live in 24-48h if approved; if not, build comment
  karma over weeks and retry.
- Decision: lean into Twitter as today's primary signal.
  Rationale: thread is live, pinned, reachable. The launch was always
  a publish-the-thesis move first, traffic-spike second (per
  LAUNCH_CHECKLIST). The thesis IS published.
- Decision: keep the Wednesday reception check scheduled but adjust
  its prompt to expect HN may not be live.
  Rationale: Twitter + GitHub stars + Cloudflare traffic are still
  meaningful signals on their own. The agent should not waste cycles
  hunting for an HN post that may not exist.

### Findings (the actual journal point)
- The HN Show gate is a real launch-day risk that none of the launch
  drafts anticipated. LAUNCH_CHECKLIST should add a pre-launch item:
  "verify HN account is past the anti-abuse threshold (commented
  thoughtfully in the last few weeks, has karma)."
- Twitter compose-stack ("+" button) is meaningfully better than
  reply-chain for thread launches: atomic post, single algorithm
  signal. scripts/launch.py defaults to reply-chain instructions; the
  helper text should call out compose-stack as the preferred path.
- The pre-flight caught real launch-blocking content bugs (5 tweets
  over weighted limit, HN title over the 80-char cap) that manual
  review missed. Twitter's weighted-char counting (URLs = 23,
  codepoints > U+10FF count as 2) is non-obvious and worth its own
  defensive layer.

### Actions
- Owner: Ry
  Action: send dang whitelist request email today.
  Expected effect: HN post live within 24-48h, OR dang declines and
  the deferred-karma path activates.
- Owner: Future cycle
  Action: update LAUNCH_CHECKLIST with the HN-account-readiness
  pre-launch item.
- Owner: Future cycle
  Action: update scripts/launch.py instructions to recommend
  compose-stack over reply-chain.
