# Recognition eval methodology

Companion to [`methodology.md`](./methodology.md). That file measures *agent
end-to-end latency* (a human-in-the-loop, manually-recorded matrix). This file
measures *recognition quality and substrate latency* — fully automated, run in
CI on every push.

## What it measures

Given a labeled golden dataset of `(prompt, manifest, expected_entities)` cases,
the harness runs `core.recognition_runtime.recognition_first` against each and
scores the matched entity names against ground truth:

| Metric | Meaning |
|---|---|
| `micro_precision/recall/f1` | Pooled TP/FP/FN across all cases. Dominated by high-entity prompts; rewards getting the common path right. |
| `macro_f1` | Mean of per-case F1. Every case counts equally, so a rare edge case (e.g. the short-name guard) can't be drowned out. |
| `latency_p50_us` / `latency_p95_us` | Recognition substrate latency in microseconds. This is the timing thesis, measured — the synchronous, no-I/O recognition path. |
| `confidence_accuracy` | For cases that label an `expected_confidence`, the fraction whose top-anchor bucket agreed. Guards the cross-surface parity contract. |

## The golden dataset

[`recognition_golden_v1.yaml`](./recognition_golden_v1.yaml). Each case is
self-contained (carries its own manifest) so the harness has zero external
dependencies and runs deterministically on any machine. The set deliberately
covers the behaviors the runtime must never regress:

- single + multi-entity recognition, alias matching, multi-word entities
- **negative controls** (a prompt that must match *nothing* — guards against
  recognition becoming trigger-happy)
- the **short-name false-positive guard** (`ILTTed` must not match `ILTT`)
- substring-vs-token discipline (`NAS` matches as a whole token, not inside
  `bananas`)
- **visibility filtering** (a `private` entity never surfaces at `team` access)
- confidence buckets (top-anchor parity)

### Versioning rule

Same discipline as the latency matrix: changing the dataset's semantics rolls
the file (`recognition_golden_v2.yaml`) and starts a fresh baseline. Adding a
case to the existing file is fine; changing what an existing case asserts is a
version bump, because cross-version scores aren't comparable.

## Running it

```bash
# Full report (per-case detail + aggregates)
bourdon recognition eval

# Aggregates only
bourdon recognition eval --summary

# As a CI gate — exit 1 if quality regresses below the bar
bourdon recognition eval --min-micro-f1 1.0 --min-macro-f1 1.0 --max-p95-us 5000

# Against your own dataset
bourdon recognition eval --dataset path/to/cases.yaml
```

The CI workflow (`.github/workflows/test.yml`) runs the gate at
`--min-micro-f1 1.0 --min-macro-f1 1.0` — the bundled golden set is expected to
score a perfect F1 against the runtime. A drop means either the code regressed
or a golden label genuinely needs updating; both should be a conscious,
reviewed change, which is exactly what the gate forces.

## Why F1 and not just hit-rate

The runtime already reports a hit-rate (`cli._recognition_eval`): "did
*something* match." That can't distinguish "matched the right entity" from
"matched a wrong one too." F1 against ground truth can — it penalizes both
false positives (recognizing an entity the prompt didn't mention) and false
negatives (missing one it did). For a recognition-first system, a confident
*wrong* recognition is worse than a miss, so precision matters as much as
recall, and F1 is the honest single number.
