# Bourdon recognition-latency methodology

This file is the rulebook for entries in [`latency_matrix.md`](./latency_matrix.md).
It exists so future Bourdon versions can be measured against an apples-to-apples
prior. Change the rulebook → bump the prompt version → start a new matrix file.

## What we are measuring

We measure **first-turn recognition latency**: how long it takes an agent, when
asked a recognition-probing prompt for the first time in a new conversation, to
emit a response that demonstrates Bourdon-supplied recognition.

This is *not* the same as the Bourdon-internal latency already returned by
[`prepare_recognition_context`](../core/l6_server.py) as
`recognition_latency_us` — that's the substrate (≈ 1 ms on a modern laptop).
The matrix measures the **agent's end-to-end response**, which is dominated
by the model's reasoning + token generation, not by Bourdon.

## The probe prompt

Stored at [`prompts/recognition_v1.txt`](./prompts/recognition_v1.txt). It is
the verbatim prompt Ry used in the 2026-05-15 cross-account proof:

```
Do you remember what Bourdon is?
```

Versioning rule: a prompt change always rolls the file
(`prompts/recognition_v2.txt`) and a new matrix file
(`latency_matrix_v2.md`). Rows across prompt versions are not comparable.

## What gets recorded per row

| Column | Meaning |
|---|---|
| `timestamp` | UTC ISO 8601 |
| `agent` | `claude-code`, `codex`, `claude-app`, `cursor`, etc. — the surface tested |
| `provider` | `openai` / `anthropic` (API mode) or `manual` |
| `model` | Provider-specific model id (e.g. `gpt-5-codex`, `claude-opus-4-7`) |
| `reasoning` | The reasoning-effort / thinking-budget setting (`low`, `medium`, `high`, `extra-high`, `none`, or integer budget tokens) |
| `mode` | `api` / `manual` |
| `account_state` | `fresh` (first conversation on a new account), `established` (existing account, new conversation), or `nth-turn-N` (within an existing conversation) |
| `machine` | `mac-m1max` / `pc-deskop` / etc. |
| `bourdon_version` | The version of Bourdon active on the machine running L6 |
| `ttft_ms` | Time-to-first-token: prompt-submit → first non-whitespace byte of the response |
| `tt_recognition_ms` | Time-to-recognition: prompt-submit → first token in the response containing any recognition keyword (see below) |
| `total_ms` | Full response duration |
| `recognition_score` | Count of recognition keywords matched (0–N) |
| `runs` | Number of repeat runs the row aggregates (default: 3). `_min` / `_median` / `_max` suffixes for the latency columns when `runs > 1`. |
| `notes` | Free-text — error retries, oddities, etc. |

## Recognition keywords

Case-insensitive substring match on the full response text:

- `Bourdon`
- `Continuo` (prior name)
- `federation` (the federation-protocol framing)
- `recognition-first` or `recognition first`
- `L5` (the manifest schema)
- `RADLAB` (Ry's org)

A response with `recognition_score == 0` is a **recognition miss** regardless of
its latency, and is recorded as such — we want the misses on the public matrix.

## Per-cell procedure

A cell is one (agent, model, reasoning) combination on one (machine, account_state).

1. Confirm `~/agent-library/` is populated by the host's actual participants
   (`bourdon doctor`). No synthetic L5 manifests.
2. Confirm `bourdon --version` matches the value being recorded.
3. Run the cell **3 times**, fresh conversation each time. The matrix row
   reports min / median / max for each latency column.
4. Any run that hits a rate-limit, network error, or safety filter is dropped
   and retried. The retry count is logged in `notes`.
5. Append the resulting row to `latency_matrix.md` — the file is append-only
   so the historical record stays clean.

## Mode semantics

- **API mode** (`--mode api`) — the harness drives a provider API directly
  (OpenAI Responses API for Codex, Anthropic Messages API for Claude),
  streams the response, and times TTFT + TT-recognition from
  `time.monotonic()` deltas. Programmatic, repeatable, no human in the loop.
- **Manual mode** (`--mode manual`) — the harness emits the prompt and
  captures a wall-clock start, waits for the user to paste back the response
  and elapsed time. Used to sanity-check API-mode numbers against the actual
  Codex App or Claude Code CLI surfaces real users experience (the source
  of the 5-min anecdote from 2026-05-15).

## What this matrix does NOT measure

- **Bourdon-internal recognition latency.** Already covered by
  `recognition_latency_us` returned from `prepare_recognition_context`.
- **MCP transport overhead** (stdio vs http). Phase 1.6's federation work
  will add a separate transport latency benchmark.
- **Cross-host federated recognition latency.** Phase 1.6 ships first; that
  benchmark file lands with it as `latency_matrix_v1_federated.md`.

## Reproducing a row

```bash
# Inside ~/bourdon, with the bench extras installed:
pip install -e '.[bench]'

# Anthropic Claude Opus 4.7, default thinking budget, 3 runs
python scripts/latency_harness.py \
  --mode api \
  --provider anthropic \
  --model claude-opus-4-7 \
  --reasoning none \
  --runs 3 \
  --machine mac-m1max \
  --account-state fresh

# OpenAI Codex 5.5, reasoning_effort=high, 3 runs
python scripts/latency_harness.py \
  --mode api \
  --provider openai \
  --model gpt-5-codex \
  --reasoning high \
  --runs 3 \
  --machine mac-m1max \
  --account-state fresh
```

Output appends one row per invocation to `latency_matrix.md`. Pass
`--dry-run` to print the row without writing the file.
