# Quickstart

From `pip install` to working federation recognition in ~3 minutes.

If you'd rather see Bourdon work before installing it, run [`bourdon demo`](#see-it-before-installing-bourdon-demo) below — it uses synthetic data and changes nothing on your machine.

---

## 1. Install

```bash
pip install bourdon
```

Optional extras:

- `pip install 'bourdon[server]'` — pulls in `fastmcp` so you can run the L6 federation server.
- `pip install 'bourdon[federation]'` — peer L6 federation over HTTP for cross-machine query.

Python 3.10+. Mac, Linux, Windows (WSL recommended) all supported by CI.

## 2. Set up

```bash
bourdon setup
```

The wizard detects which agents you have installed, creates `~/agent-library/`, wires a `SessionEnd` hook in Claude Code so manifests stay fresh automatically, and offers to run the first `bourdon export-all` and `bourdon codex sync-native --from-library --memory-md --write` for you.

It's idempotent — running it twice is safe. `--non-interactive` uses defaults; `--dry-run` shows the plan without changing anything.

## 3. Verify

```bash
bourdon doctor
```

Each adapter reports `ok` / `degraded` / `blocked`. Anything not `ok` carries a `proposed_fix` line with the exact command to run next. Example:

```yaml
- agent: codex
  status: degraded
  reason: 'Missing Codex sub-sources: session_index, sessions_dir'
  proposed_fix: If Codex was just signed in but has no chat history, this is
    expected -- run `bourdon codex sync-native --from-library --memory-md --write`
    to seed recognition substrate from your federation library.
```

## 4. See it work

Open your IDE (Claude Code or Codex) and ask:

> *"What dev tools or projects am I currently working on?"*

The first-turn answer should surface project recognition from your federation library. If you're on Codex, the relevant content lives at `~/.codex/memories/MEMORY.md` — feel free to `cat` it.

If recognition didn't fire, see [Troubleshooting](#troubleshooting) below.

---

## Cross-machine (optional)

Use Bourdon on two machines? Sync the federation library between them:

```bash
# on machine A
bourdon sync push user@machine-b.tailnet:~/agent-library/

# on machine B
bourdon sync pull user@machine-a.tailnet:~/agent-library/
```

Push filters by visibility before the network leg (`--access-level public|team|private`, default `public`). Pull rsyncs whatever the remote sent — no filter applied on receive. Both wrap `rsync -az --checksum --delay-updates` so the transport is idempotent and atomic per file.

See [`docs/agent-integration-status.md`](agent-integration-status.md) for the full mechanism + worked example over Tailscale.

---

## See it before installing — `bourdon demo`

```bash
bourdon demo
```

Self-contained walkthrough. Stages synthetic agent-library content in a tempdir, runs the **production** federation pipeline against it, and prints what would land in `MEMORY.md` — including multi-agent `(via <agent>)` source attribution and visibility filter behavior. No real IDE state is touched, no network calls are made.

`bourdon demo --access-level team` shows what team-only entries look like. `bourdon demo --no-keep` deletes the tempdir on exit.

---

## Maintain

Every now and then:

```bash
bourdon export-all    # refresh all adapter manifests
bourdon doctor        # check health, get fix proposals if anything drifts
```

The Claude Code `SessionEnd` hook wired by `bourdon setup` already keeps `claude-code.l5.yaml` fresh, so the explicit `export-all` is mostly relevant when you add a new adapter or want to force a refresh.

---

## Troubleshooting

| Symptom | First thing to try |
|---|---|
| `bourdon: command not found` after install | Check `pip` installed into a venv on `$PATH`. `pipx install bourdon` avoids this. |
| `bourdon doctor` says everything is `ok` but recognition isn't firing | Codex routes name-triggered prompts (`"what is X"`) to its training corpus — try a contextual prompt: `"what am I working on"`. See [GitHub issue #80](https://github.com/getbourdon/bourdon/issues/80). |
| `bourdon doctor` says `degraded` after setup | The `proposed_fix` line tells you the exact command. Usually a single re-run of `bourdon setup` or an adapter `init`. |
| `~/.codex/memories/MEMORY.md` exists but Codex doesn't seem to read it | Confirm you're using the actual OpenAI Codex desktop app, not chatgpt.com. The two have different memory backends. See the [2026-05-26 cross-machine test report](https://github.com/getbourdon/bourdon/blob/main/web/index.html#the-cross-machine-test) for the surface-mismatch story. |
| `bourdon sync push` errors `rsync: command not found` | Install rsync (`brew install rsync` on Mac; comes with most Linux distros; WSL/Cygwin on Windows). |
| Malformed YAML frontmatter warnings during `export-all` | The warning now names the offending file (post-2026-05-26). Open it; the `---` fences must wrap valid YAML. |

For anything else, file an issue at [github.com/getbourdon/bourdon/issues](https://github.com/getbourdon/bourdon/issues) with the output of `bourdon doctor --report-out doctor.yaml` attached.

---

## What's next

- **[POSITIONING.md](../spec/POSITIONING.md)** — the recognition-first thesis Bourdon is staking publicly.
- **[THESIS.md](../spec/THESIS.md)** — the full long-form thesis document.
- **[RELATED_WORK.md](../spec/RELATED_WORK.md)** — how Bourdon's vocabulary maps onto Mem0, Zep, Letta, Cognee, Memora.
- **[FINDINGS_JOURNAL.md](../spec/FINDINGS_JOURNAL.md)** — running log of cross-account / cross-machine validation runs.
- **[AUTHORING_AN_ADAPTER.md](AUTHORING_AN_ADAPTER.md)** — building your own adapter for an agent Bourdon doesn't ship.
