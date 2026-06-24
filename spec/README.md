# Bourdon Spec

Design documents for the Bourdon memory protocol.

## Documents

- **[`THESIS.md`](THESIS.md)** — Founding principles. Start here to understand *why* Bourdon exists and what it argues. Recognition > retrieval, concurrent language > call-and-repeat, cognition-modeled > database-modeled.

- **[`ARCHITECTURE_v0.1.md`](ARCHITECTURE_v0.1.md)** — Full technical architecture for the L0-L4 personal memory stack plus L5-L6 federation layer. Includes the timing model (the product's core innovation), file structure, system prompt injection order, and open technical questions.

- **[`USE_CASES.md`](USE_CASES.md)** — Eight worked domain scenarios demonstrating the universal scope: developer workflows, customer support, chemistry, poetry, architecture, physics, project management, education. Shows how the same L0-L6 architecture applies across domains with different entity types.

## Coming in v0.1.0

- **`SPEC_v0.1.md`** — Formal L5 manifest schema + L6 MCP protocol specification (normative, versioned)
- **`L5_schema.json`** — JSON Schema for L5 manifest validation
- **`PARTICIPANT_CONTRACT.md`** — Participant interface specification (Python protocol + semantics)
- **`PERMISSION_MODEL.md`** — Visibility levels, tag-driven defaults, PII handling guidance

## Spec Versioning

Semver on the spec itself. Each L5 manifest declares `spec_version`. Breaking changes bump major version with a migration path provided via `bourdon migrate` CLI (coming in v0.3.0).

Current spec version: **v0.1** (draft)

## Where Decisions Live

Strategic + architectural decisions are captured in the project's [claude-brain repo](https://github.com/ryandavispro1-cmyk/claude-brain) at `PROJECTS/NEUROLAYER/DECISIONS.md`. Public-facing versions of key decisions will migrate here as they stabilize.
