"""Unit tests for scripts/latency_harness.py (Phase 1.5)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS_PATH = REPO_ROOT / "scripts" / "latency_harness.py"


def _import_harness():
    """Import scripts/latency_harness.py as a module (path-import to avoid scripts/ pkg setup)."""
    spec = importlib.util.spec_from_file_location("latency_harness", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["latency_harness"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


harness = _import_harness()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_score_response_counts_distinct_keywords() -> None:
    text = "Bourdon is a federation thing, formerly Continuo. RADLAB ships it."
    # Bourdon, federation, Continuo, RADLAB = 4
    assert harness.score_response(text) == 4


def test_score_response_is_case_insensitive() -> None:
    assert harness.score_response("BOURDON.") == 1
    assert harness.score_response("bourdon.") == 1


def test_score_response_returns_zero_on_miss() -> None:
    assert harness.score_response("I have no idea what you mean.") == 0


def test_score_response_does_not_double_count_same_keyword() -> None:
    # Same keyword twice should still count as 1.
    assert harness.score_response("Bourdon, Bourdon, Bourdon.") == 1


def test_detect_recognition_offset_finds_earliest_keyword() -> None:
    text = "I think Continuo and Bourdon are related."
    # "Continuo" should win (earliest offset).
    offset = harness.detect_recognition_offset(text)
    assert offset == text.lower().find("continuo")


def test_detect_recognition_offset_returns_none_on_miss() -> None:
    assert harness.detect_recognition_offset("nothing relevant here") is None


# ---------------------------------------------------------------------------
# Stream timing
# ---------------------------------------------------------------------------


async def _slow_stream(chunks: list[tuple[str, float]]) -> AsyncIterator[str]:
    """Yield each chunk after a small sleep, so monotonic timing has signal."""
    for chunk, delay in chunks:
        await asyncio.sleep(delay)
        yield chunk


def test_stream_and_time_records_ttft_and_recognition() -> None:
    chunks = [
        ("", 0.01),                  # empty whitespace, should be skipped
        ("Hello, ", 0.02),           # first real token sets TTFT
        ("nothing yet... ", 0.02),   # no keyword in accumulated text
        ("Bourdon is the project.", 0.02),  # keyword appears → TT-recognition set
    ]
    result = asyncio.run(harness._stream_and_time(_slow_stream(chunks)))
    assert result.ttft_ms is not None
    assert result.tt_recognition_ms is not None
    assert result.tt_recognition_ms > result.ttft_ms
    assert result.score >= 1  # at least "Bourdon"
    assert "Bourdon" in result.response_text


def test_stream_and_time_handles_recognition_miss() -> None:
    chunks = [("Sorry, no idea what that is.", 0.01)]
    result = asyncio.run(harness._stream_and_time(_slow_stream(chunks)))
    assert result.ttft_ms is not None
    assert result.tt_recognition_ms is None
    assert result.score == 0


# ---------------------------------------------------------------------------
# Cell aggregation
# ---------------------------------------------------------------------------


def _make_run(ttft: float | None, ttrec: float | None, total: float, score: int) -> "harness.RunResult":
    return harness.RunResult(
        ttft_ms=ttft,
        tt_recognition_ms=ttrec,
        total_ms=total,
        score=score,
        response_text="…",
    )


def test_cell_aggregate_ms_returns_min_median_max() -> None:
    cell = harness.CellResult(
        runs=[_make_run(100, 200, 500, 3), _make_run(150, 250, 600, 4), _make_run(120, 220, 550, 3)],
        timestamp="2026-05-16T00:00:00Z",
        agent="anthropic",
        provider="anthropic",
        model="claude-opus-4-7",
        reasoning="none",
        mode="api",
        account_state="fresh",
        machine="mac-m1max",
        bourdon_version="0.6.0",
    )
    assert cell.aggregate_ms("ttft_ms") == (100, 120, 150)
    assert cell.median_score() == 3


def test_cell_aggregate_ms_skips_errored_runs() -> None:
    good = _make_run(100, 200, 500, 3)
    bad = harness.RunResult(None, None, 0.0, 0, "", error="boom")
    cell = harness.CellResult(
        runs=[good, bad],
        timestamp="2026-05-16T00:00:00Z",
        agent="anthropic",
        provider="anthropic",
        model="claude-opus-4-7",
        reasoning="none",
        mode="api",
        account_state="fresh",
        machine="mac-m1max",
        bourdon_version="0.6.0",
    )
    assert cell.aggregate_ms("ttft_ms") == (100, 100, 100)


# ---------------------------------------------------------------------------
# Row formatting + append
# ---------------------------------------------------------------------------


def test_format_row_produces_pipe_separated_markdown_row() -> None:
    cell = harness.CellResult(
        runs=[_make_run(100, 200, 500, 3)],
        timestamp="2026-05-16T00:00:00Z",
        agent="anthropic",
        provider="anthropic",
        model="claude-opus-4-7",
        reasoning="none",
        mode="api",
        account_state="fresh",
        machine="mac-m1max",
        bourdon_version="0.6.0",
        notes="smoke",
    )
    row = harness.format_row(cell)
    assert row.startswith("| 2026-05-16T00:00:00Z ")
    assert "claude-opus-4-7" in row
    assert "smoke" in row
    # The single-run case formats as "x / x / x".
    assert "100 / 100 / 100" in row


def test_append_row_inserts_before_marker(tmp_path: Path) -> None:
    matrix = tmp_path / "latency_matrix.md"
    matrix.write_text(
        "# Matrix\n\n"
        "| h | h |\n|---|---|\n| old | row |\n\n"
        f"{harness.APPEND_MARKER}\n"
    )
    harness.append_row(matrix, "| new | row |")
    text = matrix.read_text()
    assert "| old | row |" in text
    assert "| new | row |" in text
    # New row appears before the marker.
    assert text.index("| new | row |") < text.index(harness.APPEND_MARKER)


def test_append_row_raises_when_marker_missing(tmp_path: Path) -> None:
    matrix = tmp_path / "latency_matrix.md"
    matrix.write_text("# Matrix\n\n| h | h |\n|---|---|\n| old | row |\n")
    with pytest.raises(RuntimeError, match="append marker"):
        harness.append_row(matrix, "| new | row |")


# ---------------------------------------------------------------------------
# Reasoning → Anthropic budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("none", None),
        ("low", 1024),
        ("medium", 4096),
        ("high", 16384),
        ("extra-high", 32768),
        ("8000", 8000),
        ("bogus", None),
    ],
)
def test_anthropic_thinking_budget_table(label: str, expected: int | None) -> None:
    assert harness._anthropic_thinking_budget(label) == expected


# ---------------------------------------------------------------------------
# detect_bourdon_version
# ---------------------------------------------------------------------------


def test_detect_bourdon_version_matches_pyproject() -> None:
    # Reads from repo pyproject.toml; just verify it returns something semver-ish.
    v = harness.detect_bourdon_version()
    assert v != "unknown"
    assert "." in v
