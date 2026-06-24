# Show HN draft — Bourdon

**Status:** Draft, written 2026-04-27. Not yet posted.

**When to post:** ~30 minutes after the Twitter thread fires. HN is more
sober prose-driven than Twitter, so the draft reads accordingly.

**Posting account:** Ry's HN account.

**Submit URL field:** `https://bourdon.ai`

---

## Title

> Show HN: Bourdon – recognition-first memory for AI agents

(Original draft was longer; HN's 80-char title limit pushed us to the
tighter version, which is also more direct:)
> Show HN: Bourdon – memory framework for AI agents that fixes the
> call-and-repeat problem

---

## Body (URL submission has a separate text field for the comment)

I've been running multiple AI coding agents — Claude Code, Codex, Cursor,
Cline — and the same thing kept breaking my flow: every agent treats memory
as retrieval. You ask it about a project, it goes silent for 1-3 seconds
while it searches, then comes back with a paragraph from notes.

A human with that same context would have said "Oh, OMNIvour — the file
converter project" in 200ms and filled in the rest while talking. The
*shape* of the response is what makes a memory-enabled agent feel like a
mind versus a database that talks.

That gap is what Bourdon is trying to close. It's not a better RAG. It's a
runtime timing layer.

The model is L0–L6:

  - L0 hot cache: always in the system prompt. Zero retrieval. This is
    the recognition substrate.
  - L1 entity synopses: triggered by L0 keyword hit, loaded in parallel
    with response generation. The hydration substrate.
  - L2–L4: episodic, indexed, archive — only descended when a question
    actually demands them.
  - L5/L6: per-agent manifests federated through an MCP server, so
    cross-agent queries answer "what did I work on yesterday in Codex?"
    from a Claude Code session.

The numbering encodes the *time budget* each layer fits inside. Recognition
fires in microseconds. Hydration in tens-to-hundreds of milliseconds.
Descent only when needed.

Status: pre-alpha v0.0.7, MIT, 250 tests passing. Participants shipped for
Claude Code and Codex. Native publisher for a local AI swarm. Recognition-
first runtime exists as a Python module that fires in 24 microseconds
average (measured by `bourdon codex eval --recognition`); integrating
it into a live agent's response loop is the open empirical test.

I'm explicitly NOT competing with mem0, Zep (Graphiti), or Letta — those
solve representation and retrieval well. Bourdon is the timing layer that
could sit on top of any of them. Spec/RELATED_WORK.md in the repo maps the
overlap.

Most-wanted feedback:

  1. Is the "recognition-first runtime timing" framing actually new, or
     have I missed prior art? (I've surveyed Mem0, Zep, Letta, Cognee,
     Memora, the Memory-in-the-Age-of-AI-Agents survey, the SCS proposal
     in arXiv 2601.11595, Intrinsic Memory Agents, G-Memory, H-MEM, plus
     the MCP 2026 roadmap — found no one framing the runtime-timing
     problem this way. Wrong?)
  2. The recognition string is template-based for v0.1.0, not LLM-
     generated. Is the templates-feel-formulaic concern legitimate enough
     to swap to LLM-based generation now, or is "wait until users
     complain" the right call?
  3. For folks who've shipped agent memory at scale (LinkedIn CMA, Mem0
     production deployments, etc.) — what's the honest failure mode you
     ran into that this framing might be missing?

Repo: https://github.com/getbourdon/bourdon
Thesis (3 falsifiability conditions): https://github.com/getbourdon/bourdon/blob/main/spec/POSITIONING.md
Live findings journal: https://github.com/getbourdon/bourdon/blob/main/spec/FINDINGS_JOURNAL.md

Built in public. Honest about being pre-alpha. Genuinely interested in being
falsified.

---

## Posting notes

**HN-specific tone:**
- No marketing-speak. No "revolutionary." No exclamation marks except in
  questions.
- Concrete first (the OMNIvour story), abstract second (the L0-L6 model).
- Acknowledge what we're not doing as much as what we are.
- End with explicit invitations to falsify, not validate.

**Don't post if:**
- The Twitter thread is still in its first 30 min — let one fire alone first
- bourdon.ai is not actually serving content (verify with curl before
  hitting submit)
- The repo's main README still mentions "early-alpha v0.0.7" with broken
  links

**First-hour engagement:**
- Reply to the first 3-5 substantive comments within the first hour.
  HN's ranking algorithm rewards early engagement.
- Don't reply to "this looks like X but worse" comments; reply to "have
  you considered Y?" comments.
- Specifically watch for: someone in the field flagging prior art (the
  recognition-first framing). Engage seriously if surfaced — that's the
  highest-value signal.
