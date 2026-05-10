# Contributors

Bourdon is built by humans working with AI co-implementors. The contributor list documents who is authorized to push and what their lane is, so the project stays legible as more agents come online.

## Maintainers

- **Ryan Davis** -- RADLAB LLC. Project lead, thesis author, merges PRs, sets direction.

## AI co-implementors

Bourdon is co-built with AI agents working alongside their humans. The convention is **agent-as-author**: the commit lands under the human's GitHub identity (so attribution and code-review responsibility stay with a real person), and the PR description names the agent and links to the originating session for traceability.

If you submit a PR that an AI agent helped write, please:

1. Include the agent name and a link to the session (e.g. `cursor.com/agents/...`, `claude.ai/code/session_...`) in the PR description.
2. Push from a branch in your agent's lane (see below) so reviewers can tell at a glance what's happening.
3. State which parts you reviewed yourself -- the human is responsible for what the PR claims.

### Cursor Cloud Agent -- `cursor-cloud-agent`

- **Status:** contributor (granted 2026-05-02)
- **Branch lane:** `cursor/<feature-slug>`
- **Scope:** Cursor adapter (SQLite reverse engineering -- see CONTRIBUTING.md "Specifically wanted"), Cursor-side recognition wiring, Cursor integration docs.
- **Staging area:** `ryandavispro1-cmyk/cursor-spot` hosts v0 of the Cursor-Bourdon CLI and SQLite adapter while it's stabilized; mature pieces are upstreamed here as PRs.
- **Notes:** Cursor Cloud commits land under the human's GitHub identity. PRs from Cursor link the originating Cursor session.

### Claude Code -- `claude` / `claude-code-bot`

- **Status:** contributor (granted 2026-05-02)
- **Branch lane:** `claude/<feature-slug>`
- **Scope:** core orchestrator, L0/L1 logic, federation (L5/L6), spec, tests, contributor docs, repo maintenance.

### Codex -- `codex`

- **Status:** contributor (granted 2026-05-02)
- **Branch lane:** `codex/<feature-slug>`
- **Scope:** co-implementor on the reference orchestrator and CLI. Documented as co-author in earlier work; formal lane added so future Codex sessions have a documented home.
- **Recent delivery (OpenAI Codex 5.3):** hybrid memory cycle tooling, MCP smoke assertions, CI/report automation, and starter template packaging.

### GitHub Copilot -- `copilot`

- **Status:** contributor (granted 2026-05-10)
- **Branch lane:** `copilot/<feature-slug>`
- **Scope:** Copilot adapter (`adapters/copilot.py`), `bourdon copilot` CLI subcommands (`export`, `doctor`, `init`), test suite, adapter integration status documentation. Convention-based memory layer using `~/.copilot-bourdon/memory.md` -- Copilot's contribution to the recognition runtime designed from the inside out, same as every other agent in the fleet.
- **Notes:** GitHub Copilot has no accessible on-disk session index (cloud reasoning, no JSONL). The convention-file approach is Copilot's native layer: Copilot Chat can be instructed to maintain `memory.md` at session end, giving it persistent cross-session entity awareness via the L6 federation library.

## Stacked-PR caveat (cursor[bot] auto-close)

When an agent (Cursor especially) opens a PR whose **base branch is another open PR's head branch** (a stacked PR), automation may auto-close the child PR when the parent merges -- even if the child contains commits the parent did not absorb. We hit this on 2026-05-03: PR #14 merged at `T+0`, and `cursor[bot]` closed PR #12 at `T+3 seconds` and deleted its head ref at `T+5 seconds`. Four hardening commits unique to the child became unreachable from any branch.

Recovery is possible if you fetched recently (`git cat-file -e <sha>` confirms the SHA is still in your local object store; cherry-pick onto a fresh branch off `main`). But the recovery window is finite -- once garbage collection runs, the orphaned commits are gone.

**To avoid this entirely:**

- **Prefer non-stacked PRs.** Open the child against `main` directly with the parent's branch as a *base reference in the PR description*, rather than as the literal git base.
- If you must stack, **squash the parent into the child branch before merging the parent**, so the child's diff against `main` is empty when the auto-close fires.
- After merging a parent PR, **immediately verify any open child PRs** still target a valid branch and contain the expected diff.

This entry exists because future agents working in stacked configurations need to know about the foot-gun before they hit it.

## Adding a new agent

1. Add a section above with: name, branch lane, scope, identity-on-commits.
2. If the agent's runtime needs specific git config (sandbox `user.name`, token scopes, push permissions), capture the working configuration so the next agent doesn't have to rediscover it.
3. Open a PR. Maintainers approve agent additions because the lane is also a write-access decision.

## License

All contributions -- including agent-assisted ones -- are licensed under the Business Source License 1.1 (BSL) with copyright assigned to RADLAB LLC. By submitting a PR you agree to license your contribution accordingly. The human submitter is responsible for ensuring the agent's output is licensable under BSL 1.1 and that copyright can be assigned to RADLAB LLC. This allows the project to maintain unified copyright, which is required for offering commercial licenses to organizations whose use exceeds the BSL Additional Use Grant.
