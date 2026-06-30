# `conformance/` — cross-implementation parity fixtures

**Python is the oracle.** These language-neutral fixtures are the contract that the
TypeScript mirror (`getbourdon/bourdon-js`, published as `@bourdon/*`) must reproduce
exactly. The TS mirror is *conformant* iff it produces the same output as Python on
these fixtures — byte-for-byte where the contract is bytes (redaction, recognition
strings, MCP wire), value-for-value where it's structured (F1, schema validity, tool I/O).

This directory is **generated** by `tools/gen_conformance.py` (the single writer) — it
imports the live oracle modules and emits their actual output, so expectations can never
be hand-typed out of sync with the code. Two mechanical suites assert against the *same
bytes*: `tests/test_redaction.py` (Python, characterization) and the TS `vitest`
conformance suite (parity). A CI drift gate runs the generator and fails on an
uncommitted diff (`git diff --exit-code -- conformance/`).

## Regenerate

```bash
python tools/gen_conformance.py     # rewrites conformance/ + manifest.json
```

After changing a pattern in `core.redaction` (or any oracle), regenerate, review the
diff, and bump `conformance_version` in `tools/gen_conformance.py` (patch = added cases,
minor = new family, major = a changed expected-output = a reviewed behavior change).

## Contents

| File | Family | Source (extracted from) | Phase |
|------|--------|-------------------------|-------|
| `redaction_battery.json` | redaction | `tests/test_redaction.py` SECRETS/BENIGN | ✅ wired |
| `manifest.json` | index | (generated) | ✅ |
| `recognition_vectors.json` | recognition | `test_recognition_{contract,parity}.py` | TODO P2 |
| `l5_schema.json` + `l5_manifests/` | schema | `spec/L5_schema.json` | TODO P1 |
| `tier_matrix.json` | trust | `test_federation_*.py` allowlist | TODO P5 |
| `mcp_snapshots/` | wire | Python L6 server over a seed library | TODO P5/P6 |

Token-shaped secrets are stored as **fragment arrays** joined at load time, so no
contiguous secret literal ever lands in git (GitHub push-protection scans literals).
Both loaders `"".join(fragments)`.

See the master plan: `claude-brain/PROJECTS/NEUROLAYER/PLAN_TS_MIRROR_2026-06-29.md` and
the `bourdon-parity-fixture-harness` / `bourdon-py-to-ts-port` skills.
