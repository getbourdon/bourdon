# Screencast script — 2-minute Bourdon explainer

Target: 120s, ±10s. Single take with light editing. The goal is to take someone from "what is this?" to "I'm running `bourdon setup` after I close this tab" without losing the thesis.

## Setup

- **Resolution:** record 1920×1080 for crisp text. Final encode 1280×720 is fine.
- **Terminal:** dark background (matches bourdon.ai), 16pt monospace, single window. macOS Terminal.app or iTerm2 with no fancy theming.
- **Voice:** mic at speaking distance, not headset-tinny. Single take preferred — perfection slows the cadence.
- **Cursor:** show the cursor; viewers parse hand-typed commands more easily than wall-of-text.
- **Captions:** burn-in optional but a transcript file should ship with the upload.

## Pre-record checklist

- [ ] Clean `~/agent-library/` (or a fresh user) so `bourdon doctor` reports degraded → ok progression
- [ ] `pip install bourdon` already done in a fresh venv; the actual record starts at step 2 (the surprising one)
- [ ] Codex.app already signed in; first chat is FRESH (no prior turns)
- [ ] Pre-pull terminal scrollback so the first frame is clean

---

## Beat sheet

### Beat 1 — Hook (0:00 → 0:12)

**Screen:** dark terminal, cursor blinking, nothing else.

**Voice:**
> *"AI agents have memory. Most of it's the wrong shape. Today I'll show you what the right shape feels like — in two minutes."*

Beat tightly. Don't editorialize. The audience is here for the demo, not the manifesto.

---

### Beat 2 — The before (0:12 → 0:25)

**Screen:** open Codex on a brand-new account, ask:

```
What dev tools or projects am I currently working on?
```

Codex responds *"I don't currently have any saved information..."* — let it land. Don't speed past.

**Voice:**
> *"Fresh account. Fresh machine. Codex has no idea who I am. This is the default cross-vendor experience."*

---

### Beat 3 — One command (0:25 → 0:42)

**Screen:** new terminal tab. Run:

```bash
bourdon demo
```

Let the output scroll. Pause when the `### Render result ###` block appears. Highlight (cursor or zoom) the three lines:
- `source-attribution strings: 4`
- the `DemoProject (via claude-code, codex)` row
- the "Visibility filter dropped" lines

**Voice:**
> *"This is `bourdon demo`. Synthetic data, real pipeline. It shows what Bourdon does — multi-agent dedup, source attribution, visibility filtering — without touching anything on my machine yet."*

This beat is the heart of the video. Let it breathe.

---

### Beat 4 — Now for real (0:42 → 1:05)

**Screen:** in the terminal, run:

```bash
bourdon setup
```

Walk through the wizard's questions visibly. Hit enter on defaults. When it finishes:

```bash
bourdon doctor
```

Show `ok` rows for the agents that were detected.

**Voice (over the wizard):**
> *"One command sets up the federation library, wires my Claude Code session-end hook, runs the first export, and seeds Codex's memory file. Doctor confirms it."*

---

### Beat 5 — The payoff (1:05 → 1:35)

**Screen:** switch back to Codex. New chat. Ask the same question as Beat 2:

```
What dev tools or projects am I currently working on?
```

Codex responds with a real answer naming the user's actual projects.

**Voice:**
> *"Same prompt. Same Codex. Same model. The difference is the federation library Bourdon just wrote into Codex's memory file. The vendor account didn't change. Bourdon doesn't replace the agent — it gives every agent on the machine the same shared context, in the format they already read."*

Let the Codex response sit on screen for ~5s before cutting.

---

### Beat 6 — Cross-machine punch (1:35 → 1:50)

**Screen:** quick terminal flash:

```bash
bourdon sync push user@laptop.tailnet:~/agent-library/
```

(Don't actually wait for the rsync. Cut after the command issues.)

**Voice:**
> *"And one command transports that context to my other machine. Cross-machine recognition demonstrated end-to-end on 2026-05-26 — full report in the repo."*

---

### Beat 7 — Close (1:50 → 2:00)

**Screen:** bourdon.ai homepage, scrolled to the Status section. Highlight the "Quickstart" link.

**Voice:**
> *"Pre-alpha. Open source. BSL 1.1. \`pip install bourdon\` to try it. The quickstart's at bourdon.ai. That's it."*

End on the URL clearly visible.

---

## Tone notes

- **No "thanks for watching" outro.** This is a technical audience.
- **No music.** Voice + terminal sounds only. Music dates the video; the cursor + terminal does the work.
- **No pretense of polish.** The whole thesis is "evidence over marketing." A first-take video with real commands and real output is more credible than a glossy edit.
- **Don't say "Bourdon" more than twice.** The product name appears in the terminal commands; saying it out loud feels like an ad.
- **Frame the closing line as one short factual sentence.** Not a call-to-action.

## Things to NOT do

- Don't compare to Mem0/Zep/Letta out loud. The landing copy does that quietly; the video stays positive.
- Don't try to explain L0–L6. The video is about *the experience*, not the architecture.
- Don't show the 2026-05-26 benchmark report contents on screen. Mention the report exists; trust the viewer to read it after.
- Don't apologize for pre-alpha status. Just say it once at the close.

## Distribution plan (post-record)

- Upload to a stable host with a direct mp4 URL (not YouTube-only).
- Add the video as an `<video controls>` tag near the top of `bourdon.ai`'s "The cross-machine test" section, *replacing* the existing intro paragraph (the prose still appears below for indexability + accessibility).
- Cross-post to HN Show, /r/LocalLLaMA, Hacker News, AI-tooling Discords (per the post-PR-87 prognosis discussion).
- Provide a `.srt` transcript alongside the video — accessibility + SEO.

## Out of scope for v1

- B-roll. The product is the terminal; cutaway shots would be filler.
- Animated transitions. Crossfade or hard cut only.
- A second voice. Single narrator throughout — multiple voices read as marketing.

## Reference checks for the actual record

Before pressing record, confirm each command in the script still produces the expected output:

```bash
pip install bourdon
bourdon demo --no-keep         # confirm DemoProject (via claude-code, codex) line
bourdon setup --dry-run        # confirm wizard prompts look right
bourdon doctor                 # confirm proposed_fix lines render
bourdon sync push --help       # confirm the verb still exists
```

If any output drifts from the script, update the script before recording — don't paper over differences in editing.

---

*Companion artifacts shipped:*
- *[`docs/quickstart.md`](quickstart.md) — the URL the close points at*
- *[Benchmark report (2026-05-26)](https://github.com/getbourdon/bourdon/blob/main/web/index.html) — the cross-machine reference the script names*
- *`bourdon setup`, `bourdon doctor`, `bourdon demo`, `bourdon sync push` — every command in the script is shipped*
