# Licensing

Bourdon uses a **two-license split**: a permissive **Apache-2.0** wire/interop
surface over a source-available **BUSL-1.1** engine. This is deliberate — the
parts a third party needs to *interoperate with* or *build a conformant
implementation of* Bourdon are permissively licensed, while the engine that
provides Bourdon's value is protected until its Change Date.

The [`getbourdon/bourdon-js`](https://github.com/getbourdon/bourdon-js) npm
packages use the **identical** split, package-for-package.

## What's under which license

### Apache-2.0 (the wire / interop surface)

Full text: [`LICENSE-APACHE`](LICENSE-APACHE). Files in these paths carry an
`SPDX-License-Identifier: Apache-2.0` header.

| Path | What it is | Mirrors npm package |
|------|------------|---------------------|
| `cli/` | the `bourdon` CLI + client surface | `bourdon` |
| `conformance/` | cross-implementation parity fixtures | `@getbourdon/conformance` |
| `core/l5_io.py` | the L5 manifest wire format | `@getbourdon/l5` |
| `spec/` | interop spec (`L5_schema.json`, `PARTICIPANT_CONTRACT.md`, …) | — (the contract) |
| `examples/`, `starter-template/` | sample code, meant to be copied freely | — |

### BUSL-1.1 (the engine)

Full text: [`LICENSE`](LICENSE). Everything **not** listed above — most importantly
the engine (`core/` recognition / redaction / inference / federation /
`l6_server` / `l6_store` / turn-compilers), `participants/`, `adapters/`, and
`tray/`. BUSL-1.1 is source-available: you may read, modify, and use it for
almost anything **except** offering a competing hosted/embedded version of
Bourdon. Each version converts to the Change License (**Apache-2.0**) four years
after its release.

Mirrors the BUSL-1.1 npm packages: `@getbourdon/recognition`, `redaction`,
`inference`, `federation`, `mcp-server`, `participants`.

## Relicense history

- **v0.0.1 – v0.1.0** — published under **MIT**. These releases **remain MIT** in
  their distributed form; MIT is irrevocable for code already shipped under it.
- **v0.2.0+** — the project relicensed to **BUSL-1.1** (whole repo).
- **This change (vNEXT+)** — the wire/interop surface above is carved out to
  **Apache-2.0**; the engine remains **BUSL-1.1**. This is a move to a *more
  permissive* license for those paths, done unilaterally: RADLAB LLC owns 100% of
  the copyright (see [`CONTRIBUTORS.md`](CONTRIBUTORS.md)), so no contributor
  sign-off is required.

## Why this split

A memory-federation protocol is only useful if other agents and tools can speak
it. Permissively licensing the **wire** (the L5 manifest format, the participant
contract, the conformance fixtures, the CLI/client) removes any legal friction
from building a conformant participant, an alternate client, or an
interoperating implementation — while the **engine** (the recognition runtime,
the redaction SSOT, the federation server) stays protected under BUSL-1.1 until
it converts to Apache-2.0 on its Change Date.

Questions about production use, the Additional Use Grant, or commercial licensing:
see [`LICENSE_FAQ.md`](LICENSE_FAQ.md).
