"""Recognition evaluation harness -- turn the recognition thesis into a number.

The runtime ships latency + hit-rate telemetry (see cli `_recognition_eval`), but
"did *something* match" is not "did the *right* thing match." This module adds the
missing dimension: a labeled golden dataset of recognition cases, scored for
precision / recall / F1 against ground truth, plus latency percentiles -- so a
commit that quietly regresses recognition quality fails CI instead of shipping.

A case is `(prompt, manifest, expected_entities[, expected_confidence])`. The
harness runs `recognition_first` against each, compares the matched entity names
to the expected set, and aggregates:

  * micro precision/recall/F1  -- pooled TP/FP/FN across all cases (rewards
    getting the common cases right; dominated by high-entity prompts)
  * macro F1                   -- mean of per-case F1 (every case counts equally,
    so a rare edge case can't be drowned out)
  * recognition latency p50/p95 (microseconds) -- the timing thesis, measured
  * confidence accuracy        -- when a case labels an expected bucket, the
    fraction of matched top-anchors whose bucket agreed

The harness is pure + synchronous: it scores *recognition* (which is synchronous
by design), and simply closes the optional un-awaited hydration coroutine rather
than spinning an event loop -- so it is safe to call from sync code OR from
inside a running loop. It never raises on a single bad case -- a case that errors
is recorded with `error` set and scored as a miss, so one malformed row can't
sink the whole run. That keeps it safe as a CI gate.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.recognition_runtime import recognition_first

logger = logging.getLogger(__name__)

EVAL_SCHEMA_VERSION = "recognition-eval/v1"


# -- Case + result types -------------------------------------------------------


@dataclass
class EvalCase:
    """One labeled recognition case."""

    id: str
    prompt: str
    manifest: dict[str, Any]
    expected_entities: list[str] = field(default_factory=list)
    expected_confidence: Optional[str] = None
    access_level: str = "team"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvalCase":
        if not isinstance(raw, dict):
            raise ValueError(f"case must be a mapping, got {type(raw).__name__}")
        cid = str(raw.get("id") or "").strip()
        if not cid:
            raise ValueError("case missing required 'id'")
        if "prompt" not in raw:
            raise ValueError(f"case {cid!r} missing required 'prompt'")
        manifest = raw.get("manifest") or {}
        if not isinstance(manifest, dict):
            raise ValueError(f"case {cid!r}: 'manifest' must be a mapping")
        expected = raw.get("expected_entities") or []
        if not isinstance(expected, list):
            raise ValueError(f"case {cid!r}: 'expected_entities' must be a list")
        return cls(
            id=cid,
            prompt=str(raw["prompt"]),
            manifest=manifest,
            expected_entities=[str(e) for e in expected],
            expected_confidence=(
                str(raw["expected_confidence"])
                if raw.get("expected_confidence") is not None
                else None
            ),
            access_level=str(raw.get("access_level") or "team"),
        )


@dataclass
class CaseResult:
    """Scored outcome for a single case."""

    id: str
    prompt: str
    expected: list[str]
    matched: list[str]
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    recognition_latency_us: float
    confidence: str = "none"
    expected_confidence: Optional[str] = None
    confidence_ok: Optional[bool] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "prompt": self.prompt,
            "expected": self.expected,
            "matched": self.matched,
            "tp": self.true_positives,
            "fp": self.false_positives,
            "fn": self.false_negatives,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "recognition_latency_us": round(self.recognition_latency_us, 1),
            "confidence": self.confidence,
        }
        if self.expected_confidence is not None:
            out["expected_confidence"] = self.expected_confidence
            out["confidence_ok"] = self.confidence_ok
        if self.error is not None:
            out["error"] = self.error
        return out


@dataclass
class EvalReport:
    """Aggregate report across all cases."""

    schema_version: str
    n_cases: int
    micro_precision: float
    micro_recall: float
    micro_f1: float
    macro_f1: float
    latency_p50_us: float
    latency_p95_us: float
    confidence_accuracy: Optional[float]
    n_errors: int
    cases: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "n_cases": self.n_cases,
            "n_errors": self.n_errors,
            "micro_precision": round(self.micro_precision, 3),
            "micro_recall": round(self.micro_recall, 3),
            "micro_f1": round(self.micro_f1, 3),
            "macro_f1": round(self.macro_f1, 3),
            "latency_p50_us": round(self.latency_p50_us, 1),
            "latency_p95_us": round(self.latency_p95_us, 1),
        }
        if self.confidence_accuracy is not None:
            out["confidence_accuracy"] = round(self.confidence_accuracy, 3)
        out["cases"] = [c.to_dict() for c in self.cases]
        return out

    def meets(
        self,
        *,
        min_micro_f1: float = 0.0,
        min_macro_f1: float = 0.0,
        max_p95_us: Optional[float] = None,
    ) -> bool:
        """True if the report clears the given CI thresholds."""
        if self.micro_f1 < min_micro_f1:
            return False
        if self.macro_f1 < min_macro_f1:
            return False
        if max_p95_us is not None and self.latency_p95_us > max_p95_us:
            return False
        return True


# -- Scoring -------------------------------------------------------------------


def _score_sets(expected: set[str], matched: set[str]) -> tuple[int, int, int, float, float, float]:
    """Set-based TP/FP/FN + precision/recall/F1 for one case.

    Comparison is case-insensitive on entity names. The empty/empty case (a
    negative control that correctly matched nothing) scores a perfect 1.0 on all
    three metrics -- "correctly recognized nothing" is a success, not undefined.
    """
    exp = {e.lower() for e in expected}
    mat = {m.lower() for m in matched}
    tp = len(exp & mat)
    fp = len(mat - exp)
    fn = len(exp - mat)
    if not exp and not mat:
        return 0, 0, 0, 1.0, 1.0, 1.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return tp, fp, fn, precision, recall, f1


def run_case(case: EvalCase) -> CaseResult:
    """Run one case through recognition_first and score it. Never raises."""
    try:
        t0 = time.perf_counter()
        result = recognition_first(
            case.prompt, case.manifest, access_level=case.access_level
        )
        latency_us = (time.perf_counter() - t0) * 1_000_000

        # We score *recognition*, not hydration, so we don't run the hydration
        # coroutine -- we just close it to avoid a "coroutine was never awaited"
        # warning. close() touches no event loop, so this is safe whether the
        # caller is synchronous or already inside a running loop (asyncio.run()
        # would raise "cannot be called from a running event loop" there).
        hydration = result.hydration
        close = getattr(hydration, "close", None)
        if callable(close):
            close()

        matched = [str(e.get("name") or "") for e in result.matched_entities]
        matched = [m for m in matched if m]
        tp, fp, fn, precision, recall, f1 = _score_sets(
            set(case.expected_entities), set(matched)
        )

        confidence_ok: Optional[bool] = None
        if case.expected_confidence is not None:
            confidence_ok = result.confidence == case.expected_confidence

        return CaseResult(
            id=case.id,
            prompt=case.prompt,
            expected=case.expected_entities,
            matched=matched,
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            precision=precision,
            recall=recall,
            f1=f1,
            recognition_latency_us=latency_us,
            confidence=result.confidence,
            expected_confidence=case.expected_confidence,
            confidence_ok=confidence_ok,
        )
    except Exception as exc:  # noqa: BLE001 -- one bad case must not sink the run
        logger.warning("recognition-eval: case %r raised: %s", case.id, exc)
        # An errored case is scored as a total miss of whatever it expected.
        n_exp = len(case.expected_entities)
        return CaseResult(
            id=case.id,
            prompt=case.prompt,
            expected=case.expected_entities,
            matched=[],
            true_positives=0,
            false_positives=0,
            false_negatives=n_exp,
            precision=0.0,
            recall=0.0 if n_exp else 1.0,
            f1=0.0,
            recognition_latency_us=0.0,
            error=str(exc),
        )


def run_eval(cases: list[EvalCase]) -> EvalReport:
    """Run all cases and aggregate into a report."""
    results = [run_case(c) for c in cases]
    n = len(results)

    # Micro: pool TP/FP/FN across every case.
    total_tp = sum(r.true_positives for r in results)
    total_fp = sum(r.false_positives for r in results)
    total_fn = sum(r.false_negatives for r in results)
    micro_p = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    micro_r = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0
    micro_f1 = (
        2 * micro_p * micro_r / (micro_p + micro_r) if (micro_p + micro_r) else 0.0
    )

    # Macro: every case's F1 counts equally.
    macro_f1 = statistics.fmean(r.f1 for r in results) if results else 0.0

    latencies = sorted(
        r.recognition_latency_us for r in results if r.error is None
    )
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)

    conf_checked = [r for r in results if r.confidence_ok is not None]
    conf_acc = (
        sum(1 for r in conf_checked if r.confidence_ok) / len(conf_checked)
        if conf_checked
        else None
    )

    return EvalReport(
        schema_version=EVAL_SCHEMA_VERSION,
        n_cases=n,
        micro_precision=micro_p,
        micro_recall=micro_r,
        micro_f1=micro_f1,
        macro_f1=macro_f1,
        latency_p50_us=p50,
        latency_p95_us=p95,
        confidence_accuracy=conf_acc,
        n_errors=sum(1 for r in results if r.error is not None),
        cases=results,
    )


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already-sorted list. 0.0 when empty."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100) * (len(sorted_values) - 1)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


# -- Dataset loading -----------------------------------------------------------


def load_cases(path: Path) -> list[EvalCase]:
    """Load a golden dataset (YAML) into EvalCase objects.

    Expected shape::

        version: recognition-eval/v1
        cases:
          - id: ...
            prompt: ...
            manifest: {known_entities: [...]}
            expected_entities: [...]
            expected_confidence: medium   # optional

    Raises ValueError on a malformed dataset (fail loud at load time -- a broken
    golden file is a developer error, distinct from a single case mis-scoring at
    run time).
    """
    import yaml  # local import: yaml is an optional-ish dep, keep module import-light

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ValueError(f"{path}: 'cases' must be a non-empty list")
    cases = [EvalCase.from_dict(c) for c in cases_raw]
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise ValueError(f"{path}: duplicate case id {c.id!r}")
        seen.add(c.id)
    return cases
