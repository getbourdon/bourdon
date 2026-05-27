# v0.7.0 — The adoptability floor

**v0.6.0 made Bourdon work cross-machine. v0.7.0 makes it installable, demoable, and maintainable by someone other than the author.**

Today (2026-05-26) we validated the cross-machine recognition story on a brand-new OpenAI account on a fresh-substrate Mac user — the 2026-05-17 informative-negative is reversed. Then we shipped the install / see-it-work / maintain UX loop around it. The number is v0.7 (not v1) deliberately: this is the floor that makes a first non-author user possible, not the product itself.

## What's new

### Federation transport: `bourdon sync push/pull <remote>`

Closes #74. The federation library at `~/agent-library/` is now distributable cross-machine via a single command:

```bash
bourdon sync push user@laptop.tailnet:~/agent-library/   # default: --access-level public
bourdon sync pull user@desktop.tailnet:~/agent-library/
```

Push stages a visibility-filtered copy of the library in a tempdir and rsyncs that — entries above the requested access level are dropped **before** the network leg. Default is `public` so pushing team/private content is opt-in. Wraps `rsync -az --checksum --delay-updates` so transport is idempotent and atomic per file. Pull does no filtering on receive; trust boundary lives on push. Requires `rsync` on PATH.

### Federation → Codex ingestion: `bourdon codex sync-native --from-library`

Closes #75. Renders the federation library directly into Codex's native memory surfaces (`~/.codex/memories/MEMORY.md` and `bourdon_fallback.md`), preserving per-entity source attribution `(via <agent>)` across multi-agent dedup. With `--memory-md` the federation block is wrapped between markers in MEMORY.md so re-runs are idempotent and don't blow away user content outside the block.

This was the missing piece between "federation library exists on the target machine" and "Codex actually reads it" — measured today as **588 bytes → 34,482 bytes** on a fresh-substrate machine. Full benchmark in [claude-brain's NEUROLAYER/BENCHMARKS/2026-05-26 report](https://github.com/getbourdon/bourdon/blob/main/web/index.html) (excerpt embedded in the bourdon.ai cross-machine test section).

### `bourdon setup` — interactive post-install wizard

One command replaces the previous 5-step manual install path. Detects which agents are installed, creates `~/agent-library/`, wires a `SessionEnd` hook in Claude Code, runs first `export-all`, and offers to seed Codex's memory file. Idempotent; `--non-interactive` for CI; `--dry-run` for inspection.

### `bourdon doctor` — every degraded/blocked carries a `proposed_fix`

`HealthStatus` gains a `proposed_fix: Optional[str]` field. Every adapter's `health_check` populates it when status is not `ok` — an action-oriented line a user can copy-paste. Setup wires the initial state; doctor closes the loop on drift.

### `bourdon demo` — synthetic-data walkthrough

`bourdon demo` stages a synthetic agent-library in a tempdir, runs the **production** federation pipeline against it, and prints what would land in `MEMORY.md` (including `(via claude-code, codex)` multi-agent attribution and visibility-filter behavior). No real IDE state required, no network calls. The shareable answer to *"what does Bourdon actually do?"* before someone commits to installing.

### Mac install via Homebrew

```bash
brew tap getbourdon/bourdon https://github.com/getbourdon/bourdon
brew install getbourdon/bourdon/bourdon
```

Formula pulls in `python@3.12` + `rsync` automatically. See `homebrew/README.md` for maintainer SHA-refresh recipes.

### Adapter quality improvements

- **claude_code**: deferred Protocol-conformance check + permission-error tolerance — CLI no longer fails to import when `$HOME` points at an unreadable directory (sudo-without-`-H`, sandboxed containers, etc.). Closes #77.
- **codex**: junk-entity filter at L5 export. Drops placeholder names (`Project 2`, `New Project`, `untitled`) and `Observed across N session(s)` boilerplate so the federation surface stays high-signal. Closes #78.
- **All convention adapters** (claude_code, copilot, cascade): malformed YAML frontmatter now logs adapter id + source path + truncated exception. Cascade previously swallowed the error silently. Closes #79.

### Landing copy + visual refinement

[bourdon.ai](https://bourdon.ai) gets the 2026-05-26 cross-machine test section (with verbatim Codex.app evidence), drops the previous "operationally true but not yet a single-command claim" caveat now that `bourdon sync` ships, and gets a restraint-led typography pass — dated eyebrows on field-test sections, blockquote citation treatment for Codex quotes, figure-block callouts for empirical anchors (`0 ms vs ~406 ms`, `588 B → 34,482 B`), elevated philosophy line in the footer.

### Quickstart docs

New [`docs/quickstart.md`](https://github.com/getbourdon/bourdon/blob/main/docs/quickstart.md) — install to first recognition in ~3 minutes, including cross-machine sync, troubleshooting table, and the "see it before installing" path via `bourdon demo`. Linked from bourdon.ai's Status section.

### Screencast script (recordable today)

[`docs/screencast-script.md`](https://github.com/getbourdon/bourdon/blob/main/docs/screencast-script.md) — tight 2-minute shot list mapping each beat to a real shipped command. Recording is owner-discretion; the script lowers that barrier from half-a-day to ~15 minutes.

## Open questions still on the table

- **#80** — Codex's tool router maps name-triggered prompts (`"what is X?"`) to its training corpus rather than the local memory tool. Contextual prompts (`"what am I working on?"`) trigger the tool correctly. Filed as observation; workaround documented in the quickstart's troubleshooting table.
- **A separate `getbourdon/homebrew-bourdon` tap repo** — would collapse the awkward two-arg `brew tap` to a clean one-liner. Deferred.

## Counts

| Surface | v0.6.0 | v0.7.0 |
|---|---|---|
| CLI subcommands | 19 | 23 (+`setup`, `demo`, `sync push`, `sync pull`) |
| Tests passing | ~613 | 710 |
| Shipping IDE adapters | 5 | 5 (no new adapters; the existing five got hardened) |
| Open issues | 7 | 1 (the #80 observation) |

## Migration

Pure additive release. No CLI surface was renamed or removed. Existing `bourdon claude-code export` / `bourdon codex export` / `bourdon export-all` / `bourdon doctor` all keep their previous behavior plus the new `proposed_fix` field where applicable.

Existing `~/agent-library/` libraries remain readable; no schema changes. Manifests written by v0.6.0 federate without re-export.

If you have a SessionEnd hook wired by hand (per the v0.0.7 release notes' example), `bourdon setup` won't duplicate it — the hook detector uses token matching (`bourdon` + `claude-code` + `export` all present, case-insensitive), so existing hooks are recognized regardless of how the binary is spelled (bare `bourdon`, absolute path, `.exe` suffix on Windows).

## Why v0.7 and not v1

Today's wave is the **floor** that makes a first non-author user possible — not the product itself. v1.0.0 would mean either (a) a second unaffiliated user is actually using Bourdon productively, or (b) the commercial wedge (Phase 1.7 team federation with ACLs, or hosted federation service) exists as shipped code. Today shipped neither. Reserving v1.0.0 for one of those gives the number weight.

That said, v0.7.0 commits us to keeping the new CLI verbs (`setup`, `demo`, `sync push/pull`) stable — they're documented in the quickstart and shipping in Homebrew. Any future breaking change to those is a major bump.

## Acknowledgements

Built with Claude (Anthropic) + Codex (OpenAI) + Cursor across PC and Mac. The 2026-05-26 cross-machine test ran on a brand-new OpenAI account on a fresh-substrate macOS user; full transcripts in the [benchmark report](https://github.com/getbourdon/bourdon/blob/main/web/index.html#the-cross-machine-test). Codex.app gets a particular nod for opening Bourdon-written MEMORY.md as a file attachment and quoting it verbatim including the `(via claude-code, codex)` attribution — the cleanest possible primary-source confirmation of the federation→recognition pipeline.
