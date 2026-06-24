# v0.10.0 — shadcn/improve backlog federation

Your repo's plan backlog, visible to every agent in the fleet.

## Added

- **`bourdon improve sync`** (#137): reads any repo's
  [shadcn/improve](https://github.com/shadcn/improve)-format `plans/`
  backlog (the `plans/README.md` status index plus per-plan `## Status`
  blocks) and commits it to the federation — one entity per plan, one
  rollup entity per repo that names the next executable plan (lowest-
  numbered TODO whose dependencies are DONE), and one session per sync
  run. Every federated agent can answer "what's executable right now in
  repo X — and what's blocking it" without opening repo X.

  ```
  bourdon improve sync .            # federate this repo's backlog
  bourdon improve sync . --dry-run  # print the would-be payload, zero writes
  ```

  Entity names (`plan:<repo>/<file-stem>`, `improve-backlog:<repo>`) are
  the stable federation merge keys — re-syncing updates in place, never
  duplicates. Parsing is tolerant by design: columns map by header name
  (not position), `BLOCKED (reason)` parentheticals are captured, and
  index rows whose plan file is missing degrade gracefully.

  Wire it to a session-end hook and the answer is never stale:

  ```bash
  # Claude Code SessionEnd hook (settings.json)
  d=$(jq -r '.cwd // empty'); [ -f "$d/plans/README.md" ] && bourdon improve sync "$d"
  ```

  The adapter consumes the improve plan format as-is. It complements
  [shadcn/improve](https://github.com/shadcn/improve) (MIT) — it does not
  fork or replicate it. Thanks to shadcn for a plan format that turned
  out to be a great federation substrate, not just a great handoff
  standard.

## Fixed

- **Scalar-typed list fields in `commit_l5`** (#134, fixed by #135 + #136):
  reader-exported manifests carrying list fields as bare strings (e.g.
  `project_focus: "Bourdon"`) could crash or silently corrupt the merge
  union on both the merge and add paths. Both paths now coerce; the
  durability contract (MCP commits are durable only on commit-only slugs)
  is documented on the store.

## Docs

- Claude Desktop chat write path via `commit_to_federation` (#133).
