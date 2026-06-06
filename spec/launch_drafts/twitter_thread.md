# Twitter / X thread — Bourdon launch

**Status:** Draft, written 2026-04-27. Not yet posted.

**When to post:** After the Cloudflare custom domain is bound and
`https://bourdon.ai` is reachable. The thread links to the landing page,
not the workers.dev subdomain.

**Account:** Ry's main / RADMAN handle.

**Length target:** 7 tweets, all under 280 chars. Tag accounts where it adds
context, not for engagement-bait.

---

## Tweet 1 / Hook

> Every AI agent memory framework I've tried optimizes for the same thing:
> better retrieval. Mem0, Zep, Letta, the whole field.
>
> But retrieval was never the actual problem. The problem is *runtime
> timing*. Recognition fires before details. Most frameworks skip that step.
>
> 🧵

---

## Tweet 2 / The diagnosis

> What the field calls "natural language" is really call-and-repeat:
>
>   you speak → silence → AI searches → AI replies
>
> A serial pipeline is why memory-enabled agents feel like databases.
> Real conversation is concurrent: recognition first, hydration parallel.

---

## Tweet 3 / The OMNIvour test (the lived bug)

> Last week: Codex on a populated memory federation.
> Asked: "What do you know about OMNIvour?"
>
> Data layer worked. Behavior was wrong. Codex searched first,
> summarized second. A mind would've said "Oh, OMNIvour, the file
> converter" in 200ms.
>
> That gap is the product.

---

## Tweet 4 / The architecture

> Bourdon's timing model:
>
>  L0  hot cache       → recognition (always loaded)
>  L1  entity synopses → hydration (parallel-loaded)
>  L2-L4  episodic + index + archive → descent
>  L5/L6  manifest + federation → cross-agent
>
> Numbering = time budget each layer fits in.

---

## Tweet 5 / Not competing with the field

> Bourdon isn't competing with @mem0ai @zep_ai @letta_inc — those
> frameworks solve representation + retrieval really well.
>
> Bourdon is the *runtime timing layer* that can sit on top of any of
> them. Mem0's store + Bourdon's timing = memory that feels like a
> mind, not a database.

---

## Tweet 6 / Status + what's shipped

> Pre-alpha v0.0.7, MIT, public:
>
>   • 250/250 tests passing
>   • Participants: Claude Code, Codex
>   • Native publisher for a local AI swarm
>   • Recognition runtime as Python module (24µs avg)
>   • SessionEnd hook = auto-federation
>
> Free, open, on GitHub.

---

## Tweet 7 / CTA

> Read the thesis: https://bourdon.ai
>
> Code: https://github.com/getbourdon/bourdon
>
> Most-wanted: participants for Cursor + Cline, integration of the
> recognition runtime into a real agent's response loop, and honest
> falsification attempts.
>
> Recognition first.

---

## Posting notes

**Tags considered:**
- `@anthropicai` — primary
- `@OpenAI` — Codex side
- `@mem0ai @letta_inc @zep_ai` — peer projects, Tweet 5 specifically
- `@huggingface` — academic adjacency (Memora, Memory in the Age of AI Agents)

**Don't:**
- Tag @ycombinator — let HN organic discovery happen
- Use marketing-speak like "revolutionary", "game-changing", "the future of"
- Promise features that aren't shipped (recognition runtime is a module
  today, not a wired-in production loop yet — Tweet 6 says so honestly)

**Engagement plan post-post:**
- First hour: monitor mentions, reply to substantive questions
- First 24h: pin the thread; reply to anyone who actually engages with the
  thesis. Skip drive-by quote-tweets.
- If a competitor framework's account replies: be respectful, link to the
  RELATED_WORK doc. The point is positioning, not picking fights.
