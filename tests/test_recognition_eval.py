"""Tests for core.recognition_eval -- the recognition eval harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.recognition_eval import (
    EVAL_SCHEMA_VERSION,
    EvalCase,
    _percentile,
    _score_sets,
    load_cases,
    run_case,
    run_eval,
)

_GOLDEN = Path(__file__).resolve().parent.parent / "BENCHMARKS" / "recognition_golden_v1.yaml"


# ---- scoring primitives -----------------------------------------------------


def test_score_sets_perfect():
    tp, fp, fn, p, r, f1 = _score_sets({"a", "b"}, {"a", "b"})
    assert (tp, fp, fn) == (2, 0, 0)
    assert (p, r, f1) == (1.0, 1.0, 1.0)


def test_score_sets_case_insensitive():
    _, _, _, p, r, f1 = _score_sets({"Bourdon"}, {"bourdon"})
    assert f1 == 1.0


def test_score_sets_partial():
    tp, fp, fn, p, r, f1 = _score_sets({"a", "b"}, {"a", "c"})
    assert (tp, fp, fn) == (1, 1, 1)
    assert p == 0.5 and r == 0.5 and f1 == 0.5


def test_score_sets_empty_empty_is_perfect():
    """Correctly recognizing nothing (negative control) scores 1.0, not 0/undef."""
    tp, fp, fn, p, r, f1 = _score_sets(set(), set())
    assert (tp, fp, fn) == (0, 0, 0)
    assert (p, r, f1) == (1.0, 1.0, 1.0)


def test_score_sets_false_positive_only():
    _, fp, _, p, r, f1 = _score_sets(set(), {"ghost"})
    assert fp == 1 and p == 0.0


def test_percentile():
    assert _percentile([], 50) == 0.0
    assert _percentile([5.0], 95) == 5.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


# ---- case loading -----------------------------------------------------------


def test_evalcase_from_dict_minimal():
    c = EvalCase.from_dict({"id": "x", "prompt": "hi", "manifest": {"known_entities": []}})
    assert c.id == "x" and c.expected_entities == []


def test_evalcase_from_dict_requires_id_and_prompt():
    with pytest.raises(ValueError):
        EvalCase.from_dict({"prompt": "hi"})
    with pytest.raises(ValueError):
        EvalCase.from_dict({"id": "x"})


def test_load_cases_rejects_duplicate_ids(tmp_path: Path):
    p = tmp_path / "dup.yaml"
    p.write_text(
        "version: x\ncases:\n"
        "  - {id: a, prompt: hi, manifest: {known_entities: []}, expected_entities: []}\n"
        "  - {id: a, prompt: yo, manifest: {known_entities: []}, expected_entities: []}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(p)


def test_load_cases_rejects_empty(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("version: x\ncases: []\n")
    with pytest.raises(ValueError):
        load_cases(p)


# ---- run_case / run_eval ----------------------------------------------------


def test_run_case_scores_a_match():
    case = EvalCase(
        id="t",
        prompt="tell me about Bourdon",
        manifest={"known_entities": [{"name": "Bourdon", "type": "project"}]},
        expected_entities=["Bourdon"],
    )
    res = run_case(case)
    assert res.matched == ["Bourdon"]
    assert res.f1 == 1.0
    assert res.recognition_latency_us >= 0.0


def test_run_case_negative_control():
    case = EvalCase(
        id="neg",
        prompt="what's the weather",
        manifest={"known_entities": [{"name": "Bourdon"}]},
        expected_entities=[],
    )
    res = run_case(case)
    assert res.matched == []
    assert res.f1 == 1.0  # correctly matched nothing


def test_run_case_never_raises_on_bad_manifest():
    """A malformed case is scored as a miss, not an exception."""
    case = EvalCase(
        id="bad",
        prompt="hi",
        manifest={"known_entities": "not a list"},  # type: ignore[dict-item]
        expected_entities=["Something"],
    )
    res = run_case(case)
    # detect_entities tolerates this and returns [], so it's a clean miss.
    assert res.matched == []
    assert res.false_negatives == 1


def test_run_case_confidence_check():
    case = EvalCase(
        id="conf",
        prompt="tell me about Bourdon",
        manifest={"known_entities": [{"name": "Bourdon", "type": "project"}]},
        expected_entities=["Bourdon"],
        expected_confidence="medium",
    )
    res = run_case(case)
    assert res.expected_confidence == "medium"
    assert res.confidence_ok is True


def test_run_eval_aggregates():
    cases = [
        EvalCase("a", "about Bourdon", {"known_entities": [{"name": "Bourdon"}]}, ["Bourdon"]),
        EvalCase("b", "weather", {"known_entities": [{"name": "Bourdon"}]}, []),
    ]
    report = run_eval(cases)
    assert report.n_cases == 2
    assert report.n_errors == 0
    assert report.micro_f1 == 1.0
    assert report.macro_f1 == 1.0


def test_report_meets_thresholds():
    cases = [
        EvalCase("a", "about Bourdon", {"known_entities": [{"name": "Bourdon"}]}, ["Bourdon"]),
    ]
    report = run_eval(cases)
    assert report.meets(min_micro_f1=0.9, min_macro_f1=0.9)
    assert not report.meets(min_micro_f1=1.01)  # impossible bar -> fails


# ---- the bundled golden dataset ---------------------------------------------


def test_golden_dataset_loads():
    cases = load_cases(_GOLDEN)
    assert len(cases) >= 10
    ids = {c.id for c in cases}
    assert "short-name-guard" in ids
    assert "visibility-private-hidden" in ids


def test_golden_dataset_is_all_green():
    """The bundled golden set must score a perfect F1 against the current
    runtime -- it IS the regression gate. If this fails, recognition changed
    behavior and either the code regressed or the golden labels need updating."""
    report = run_eval(load_cases(_GOLDEN))
    assert report.schema_version == EVAL_SCHEMA_VERSION
    assert report.n_errors == 0
    assert report.micro_f1 == 1.0, [
        (c.id, c.expected, c.matched) for c in report.cases if c.f1 < 1.0
    ]
    assert report.macro_f1 == 1.0
    assert report.confidence_accuracy == 1.0
