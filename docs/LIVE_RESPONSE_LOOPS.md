# Live Response Loop Integrations

This is the operational guide for wiring Bourdon into an agent's **live response loop** — the real-time, per-turn path where a prompt arrives and the agent answers *while the user waits*. It is the companion to [`AUTHORING_A_PARTICIPANT.md`](AUTHORING_A_PARTICIPANT.md): a participant **captures** memory after the fact; a live response loop integration **injects** recognition into the turn as it happens.

If you only read one thing: **a participant publishes an L5 manifest; a live loop integration consumes recognition inside a running turn and feeds the turn back into the federation.** They are the two halves of the cycle. Most agents eventually want both.

---

## What a live response loop is

The live response loop is an agent's per-turn cycle:

```
prompt arrives → context assembled → model generates → turn closes
```

It is "live" to distinguish it from Bourdon's **batch path** — the `SessionEnd` export hook, the hourly `export-all` job — which runs *outside* any turn the user is waiting on. The spec has called this out as the open frontier since the recognition runtime landed:

> "Architecture: complete and wired in tests; integration into a live agent's response loop is the next experimental step." — `spec/FINDINGS_JOURNAL.md`

A live response loop integration plugs Bourdon into a runtime at **two boundaries**:

| Boundary | When | What Bourdon does | Reference surface |
|---|---|---|---|
| **Pre-generation (inject)** | After the prompt is read, before the model generates | Recognize entities in the prompt; splice cross-agent / cross-machine context into the turn | `prepare_recognition_context`, `compile_codex_turn`, `get_deeper_context` |
| **Post-turn (capture)** | After the turn closes | Harvest what just happened back into the federation so the next loop — possibly a different agent or machine — recognizes it | `commit_to_federation`, the participant `export_l5` path |

The inject boundary is the one that's hard and the one we most want contributions for. Capture is well-trodden (that's what participants do).

### The canonical shape: recognition-first, hydration-second

A correct live loop integration does **not** block generation on a full memory fetch. It follows the splice pattern proven in the Clyde/llama.cpp path:

1. Emit the **recognition string** immediately (cheap, deterministic — "Oh — OMNIvour, the project.").
2. Let the model **start streaming** tokens.
3. Inject **hydrated detail** (the L1 documents) on the next turn boundary, when it arrives.

This is *why* recognition feels like recognition and not like a database lookup: the acknowledgment lands at conversational latency; the depth catches up. An integration that waits for full hydration before the first token has missed the point.

---

## You are reading this inside a live response loop right now

In a Claude Code session, the `SessionStart` and `UserPromptSubmit` hooks shell out to `bourdon` and inject a "Bourdon recognition context — federated recognition" block ahead of the model's answer. That injection-into-a-running-turn **is** a reference live loop integration. The `SessionEnd` hook in the same config is the *batch* counterpart. Same agent, two paths — study both.

---

## Reference integrations (copy these patterns)

| Integration | Boundary | Mechanism | Where it lives |
|---|---|---|---|
| **Claude Code hooks** | inject + capture | `SessionStart` / `UserPromptSubmit` shell out to `bourdon` (inject); `SessionEnd` runs `bourdon claude-code export` (capture) | `~/.claude/settings.json` hooks → [`docs/integrations/claude-code.md`](integrations/claude-code.md) |
| **Codex turn-scoped compiler** | inject | `compile_codex_turn` builds a deterministic ranked brief per turn (prompt + cwd/repo + L5/L6 federation → scored items routed to `explicit_text` / `mcp_payload` / `memory_md_block` / `fallback_block` / `repo_overlay_block`) | `core/codex_turn_compiler.py`, `codex-turn-brief/v1` schema, `bourdon codex compile-turn` → [`docs/codex-turn-compiler.md`](codex-turn-compiler.md) |
| **MCP tool surface** | inject + capture | Any MCP-speaking agent calls `prepare_recognition_context` / `get_deeper_context` / `query_agent_memory` mid-turn, `commit_to_federation` after | `core.l6_server` MCP server |
| **Clyde / llama.cpp WS splice** | inject | Recognition string emits first over WS, then llama tokens stream, then hydrated detail injects on the next turn boundary | Clyde repo (native publisher) |
| **OpenClaw plugin** | inject + capture | Platform-native plugin hooks the message loop in OpenClaw's own idiom | [`docs/integrations/openclaw.md`](integrations/openclaw.md) |

---

## Candidate live loops (most wanted)

- **New IDE / agent turn hooks** beyond the five existing adapters — Cline (blocked pending a memory store), Zed, Continue.dev, JetBrains AI.
- **Chat-framework middleware** — a LangChain / LlamaIndex / Vercel AI SDK callback or middleware that calls `prepare_recognition_context` before the LLM call and `commit_to_federation` after.
- **Voice / realtime pipelines** — Pipecat blocks, LiveKit agents, a Twilio / Vapi turn handler. Recognition has to land inside a sub-second speech turn — the latency contract below is non-negotiable here.
- **Browser / extension turn interception** — inject recognition into a web chat UI's send path.
- **Server-side agent runtimes** — a Hono / Edge middleware that recognizes before routing.

---

## The contract a live loop integration MUST honor

These are normative. An integration that violates them will degrade the live experience for every federated agent, not just yours.

1. **Latency-bounded.** Your inject step runs inside a turn the user is waiting on. It MUST be cancellable and MUST have a hard timeout. If recognition can't be assembled in budget, the turn proceeds *without* it. (Mirror the participant rule: L2/hydration never blocks first-response generation.)
2. **Degrade, never crash.** Recognition failing is normal — the federation may be unreachable, the token may be stale. Surface a `degraded` / `blocked` / `ok` signal and let the turn continue. Never let a recognition failure abort the user's turn. (Same discipline as participant `health_check()`, which MUST NOT raise.)
3. **Recognition-first, hydration-second.** Emit the cheap recognition string before generation; inject hydrated L1 detail on the next boundary. Do not block the first token on a full fetch.
4. **Both boundaries or say so.** A capture-only contribution is a *participant*, not a live loop integration — send it to [`AUTHORING_A_PARTICIPANT.md`](AUTHORING_A_PARTICIPANT.md). A live loop integration must at minimum do the inject boundary; pairing it with capture (or an existing participant) is strongly preferred.
5. **Idempotent capture.** If you also write back, the capture path must be idempotent — same turn state → same federation write — so replays and retries don't duplicate memory.
6. **Visibility is enforced before injection.** Only inject context the current agent/scope is allowed to see. Use the federation's visibility filtering; don't roll your own.

---

## Picking your mechanism

| Your runtime exposes… | Use |
|---|---|
| Turn lifecycle hooks (pre-prompt / post-turn) | Shell out to `bourdon` or call the MCP tools from the hook — the Claude Code pattern |
| An MCP client | The L6 MCP tool surface directly (`prepare_recognition_context` + `commit_to_federation`) |
| A native plugin SDK | A platform-native plugin in the runtime's idiom — the OpenClaw pattern (where adoption lives) |
| A streaming generation pipeline you control | The WS splice — emit recognition, stream, hydrate on next boundary (the Clyde pattern) |
| Only post-hoc disk state, no turn hook | You can't do a live loop — write a **participant** instead |

---

## Filing & testing

- Open an issue describing the runtime and which boundary(ies) you'll wire before building — turn-loop semantics vary wildly and we can save you a rewrite.
- Add a property test that asserts the inject step **never blocks first-token generation** past its timeout budget (this is the failure mode we care most about).
- Conventional commits: `feat(live-loop): wire <runtime> recognition inject`.

> **Normative sources win over this guide.** When this doc and `spec/PARTICIPANT_CONTRACT.md` / `spec/ARCHITECTURE_v0.1.md` disagree, the spec is canonical and a sync PR against this file is welcome.
