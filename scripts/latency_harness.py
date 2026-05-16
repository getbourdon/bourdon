#!/usr/bin/env python3
"""Bourdon Phase 1.5 — first-turn recognition latency harness.

Drives a provider API (OpenAI / Anthropic) or a manual paste-back loop,
times the response, scores it for recognition keywords, and appends a
row to BENCHMARKS/latency_matrix.md.

Methodology: BENCHMARKS/methodology.md.

Usage examples:

    python scripts/latency_harness.py \\
        --mode api --provider anthropic --model claude-opus-4-7 \\
        --reasoning none --runs 3 --machine mac-m1max --account-state fresh

    python scripts/latency_harness.py \\
        --mode api --provider openai --model gpt-5-codex \\
        --reasoning high --runs 3 --machine mac-m1max --account-state fresh

    python scripts/latency_harness.py --mode manual \\
        --agent claude-code --model claude-opus-4-7 --reasoning none
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / "BENCHMARKS" / "latency_matrix.md"
PROMPT_V1_PATH = REPO_ROOT / "BENCHMARKS" / "prompts" / "recognition_v1.txt"
APPEND_MARKER = "<!-- BOURDON_LATENCY_MATRIX_APPEND_MARKER -->"

RECOGNITION_KEYWORDS: tuple[str, ...] = (
    "bourdon",
    "continuo",
    "federation",
    "recognition-first",
    "recognition first",
    "l5",
    "radlab",
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """One single response."""

    ttft_ms: float | None
    tt_recognition_ms: float | None
    total_ms: float
    score: int
    response_text: str
    error: str | None = None


@dataclass
class CellResult:
    """Aggregate across N runs of one (agent, model, reasoning) cell."""

    runs: list[RunResult]
    timestamp: str
    agent: str
    provider: str
    model: str
    reasoning: str
    mode: str
    account_state: str
    machine: str
    bourdon_version: str
    notes: str = ""

    def kept_runs(self) -> list[RunResult]:
        return [r for r in self.runs if r.error is None]

    def aggregate_ms(self, field_name: str) -> tuple[float | None, float | None, float | None]:
        values = [getattr(r, field_name) for r in self.kept_runs() if getattr(r, field_name) is not None]
        if not values:
            return None, None, None
        return min(values), statistics.median(values), max(values)

    def median_score(self) -> int:
        scores = [r.score for r in self.kept_runs()]
        if not scores:
            return 0
        return int(statistics.median(scores))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_response(text: str, keywords: Iterable[str] = RECOGNITION_KEYWORDS) -> int:
    """Count of distinct recognition keywords matched in the response."""
    lower = text.lower()
    hits = sum(1 for kw in keywords if kw in lower)
    return hits


def detect_recognition_offset(text: str, keywords: Iterable[str] = RECOGNITION_KEYWORDS) -> int | None:
    """Character offset of the first recognition keyword (case-insensitive).

    Returns None if no keyword present.
    """
    lower = text.lower()
    earliest: int | None = None
    for kw in keywords:
        i = lower.find(kw)
        if i >= 0 and (earliest is None or i < earliest):
            earliest = i
    return earliest


# ---------------------------------------------------------------------------
# Provider drivers
# ---------------------------------------------------------------------------


async def _stream_and_time(
    stream: AsyncIterator[str],
    keywords: Iterable[str] = RECOGNITION_KEYWORDS,
) -> RunResult:
    """Consume a token-by-token async stream of text chunks, time TTFT + TT-recognition.

    Each chunk yielded from `stream` should be a string delta (possibly empty,
    will be skipped). The first non-whitespace chunk sets TTFT; the first chunk
    whose accumulated text contains a recognition keyword sets TT-recognition.
    """
    start = time.monotonic()
    ttft_ms: float | None = None
    tt_recognition_ms: float | None = None
    acc = ""
    last_check_len = 0
    async for chunk in stream:
        if chunk is None:
            continue
        if not chunk.strip():
            acc += chunk
            continue
        now_ms = (time.monotonic() - start) * 1000.0
        if ttft_ms is None:
            ttft_ms = now_ms
        acc += chunk
        if tt_recognition_ms is None and detect_recognition_offset(acc) is not None:
            tt_recognition_ms = now_ms
        last_check_len = len(acc)
    total_ms = (time.monotonic() - start) * 1000.0
    return RunResult(
        ttft_ms=ttft_ms,
        tt_recognition_ms=tt_recognition_ms,
        total_ms=total_ms,
        score=score_response(acc),
        response_text=acc,
    )


async def drive_anthropic(prompt: str, model: str, reasoning: str) -> RunResult:
    """Drive Anthropic Messages API, return one RunResult."""
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        return RunResult(None, None, 0.0, 0, "", error=f"anthropic SDK not installed: {exc}")

    client = anthropic.AsyncAnthropic()  # uses ANTHROPIC_API_KEY env var
    kwargs: dict = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    # Reasoning -> Anthropic thinking budget.
    budget = _anthropic_thinking_budget(reasoning)
    if budget is not None:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Streaming requires max_tokens > budget_tokens.
        kwargs["max_tokens"] = max(kwargs["max_tokens"], budget + 1024)

    async def _gen() -> AsyncIterator[str]:
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for text_delta in stream.text_stream:
                    yield text_delta
        except Exception as exc:  # noqa: BLE001
            yield f"\n[stream error: {exc}]"

    return await _stream_and_time(_gen())


def _anthropic_thinking_budget(reasoning: str) -> int | None:
    """Map our reasoning labels to Anthropic thinking budgets (tokens)."""
    table = {
        "none": None,
        "low": 1024,
        "medium": 4096,
        "high": 16384,
        "extra-high": 32768,
    }
    if reasoning in table:
        return table[reasoning]
    # Allow raw integer budgets.
    try:
        return int(reasoning)
    except ValueError:
        return None


async def drive_openai(prompt: str, model: str, reasoning: str) -> RunResult:
    """Drive OpenAI Responses API, return one RunResult.

    Falls back to chat.completions if responses API is unavailable for the model.
    """
    try:
        import openai  # type: ignore
    except ImportError as exc:
        return RunResult(None, None, 0.0, 0, "", error=f"openai SDK not installed: {exc}")

    client = openai.AsyncOpenAI()  # uses OPENAI_API_KEY env var

    async def _gen() -> AsyncIterator[str]:
        try:
            kwargs: dict = {
                "model": model,
                "input": prompt,
                "stream": True,
            }
            if reasoning in ("low", "medium", "high", "extra-high"):
                # Codex 5.5 uses reasoning.effort
                kwargs["reasoning"] = {"effort": reasoning}
            response_stream = await client.responses.create(**kwargs)
            async for event in response_stream:
                delta = getattr(event, "delta", None)
                if isinstance(delta, str):
                    yield delta
                elif hasattr(event, "output_text") and isinstance(event.output_text, str):
                    yield event.output_text
        except Exception as exc:  # noqa: BLE001
            # Fall back to chat.completions for older models.
            try:
                chat = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                )
                async for event in chat:
                    delta = event.choices[0].delta.content if event.choices else None
                    if delta:
                        yield delta
            except Exception as exc2:  # noqa: BLE001
                yield f"\n[stream error: {exc} | fallback error: {exc2}]"

    return await _stream_and_time(_gen())


def drive_manual(prompt: str) -> RunResult:
    """Interactive manual mode — user pastes the response."""
    print("=" * 72)
    print("MANUAL MODE — paste this prompt into your agent surface verbatim:")
    print("-" * 72)
    print(prompt)
    print("-" * 72)
    input("Press ENTER the moment you submit the prompt (timer starts now)...")
    start = time.monotonic()
    first_token_input = input("Press ENTER the moment the first token appears: ")
    ttft_ms = (time.monotonic() - start) * 1000.0
    print("Paste the full response below. End with a line containing only `EOF`:")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "EOF":
            break
        lines.append(line)
    total_ms = (time.monotonic() - start) * 1000.0
    response = "\n".join(lines)
    recognition_offset = detect_recognition_offset(response)
    # TT-recognition can't be precisely measured in manual mode without per-token
    # timing — we approximate as the total elapsed *if* recognition is present.
    tt_recognition_ms = total_ms if recognition_offset is not None else None
    if first_token_input.strip():
        # User typed something other than a bare ENTER; ignore.
        pass
    return RunResult(
        ttft_ms=ttft_ms,
        tt_recognition_ms=tt_recognition_ms,
        total_ms=total_ms,
        score=score_response(response),
        response_text=response,
    )


# ---------------------------------------------------------------------------
# Matrix row formatting + append
# ---------------------------------------------------------------------------


def _fmt_ms_tuple(t: tuple[float | None, float | None, float | None]) -> str:
    a, b, c = t
    if a is None or b is None or c is None:
        return "—"
    return f"{a:.0f} / {b:.0f} / {c:.0f}"


def format_row(result: CellResult) -> str:
    ttft = _fmt_ms_tuple(result.aggregate_ms("ttft_ms"))
    ttrec = _fmt_ms_tuple(result.aggregate_ms("tt_recognition_ms"))
    total = _fmt_ms_tuple(result.aggregate_ms("total_ms"))
    runs_recorded = len(result.kept_runs())
    return (
        f"| {result.timestamp} "
        f"| {result.agent} "
        f"| {result.provider} "
        f"| {result.model} "
        f"| {result.reasoning} "
        f"| {result.account_state} "
        f"| {result.machine} "
        f"| {result.bourdon_version} "
        f"| {ttft} "
        f"| {ttrec} "
        f"| {total} "
        f"| {result.median_score()} "
        f"| {runs_recorded} "
        f"| {result.notes or '—'} |"
    )


def append_row(matrix_path: Path, row: str) -> None:
    """Insert row immediately before the append marker."""
    text = matrix_path.read_text()
    if APPEND_MARKER not in text:
        raise RuntimeError(f"append marker {APPEND_MARKER!r} missing from {matrix_path}")
    before, _marker, after = text.partition(APPEND_MARKER)
    new_text = f"{before.rstrip()}\n{row}\n\n{APPEND_MARKER}{after}"
    matrix_path.write_text(new_text)


# ---------------------------------------------------------------------------
# Cell orchestration
# ---------------------------------------------------------------------------


def detect_bourdon_version() -> str:
    """Read the version pinned in pyproject.toml — cheap, no import side effects."""
    pyproject = REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "unknown"
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    return match.group(1) if match else "unknown"


async def run_cell(args: argparse.Namespace) -> CellResult:
    prompt = PROMPT_V1_PATH.read_text().strip()

    driver: Callable[[], "asyncio.Future[RunResult]"]
    if args.mode == "manual":
        async def _wrap() -> RunResult:
            # input() can't be awaited; run in thread.
            return await asyncio.to_thread(drive_manual, prompt)
        driver = _wrap
    elif args.provider == "anthropic":
        driver = lambda: drive_anthropic(prompt, args.model, args.reasoning)
    elif args.provider == "openai":
        driver = lambda: drive_openai(prompt, args.model, args.reasoning)
    else:
        raise SystemExit(f"Unknown provider/mode combination: {args.provider}/{args.mode}")

    runs: list[RunResult] = []
    for i in range(args.runs):
        if args.runs > 1:
            print(f"  run {i + 1}/{args.runs}…", file=sys.stderr)
        result = await driver()
        runs.append(result)

    return CellResult(
        runs=runs,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        agent=args.agent,
        provider=args.provider if args.mode == "api" else "manual",
        model=args.model,
        reasoning=args.reasoning,
        mode=args.mode,
        account_state=args.account_state,
        machine=args.machine,
        bourdon_version=detect_bourdon_version(),
        notes=args.notes,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bourdon recognition-latency harness (Phase 1.5).")
    p.add_argument("--mode", choices=("api", "manual"), default="api")
    p.add_argument("--provider", choices=("openai", "anthropic"), default="anthropic")
    p.add_argument("--model", required=False, default="claude-opus-4-7")
    p.add_argument("--reasoning", default="none", help="low/medium/high/extra-high/none or integer budget tokens")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument(
        "--agent",
        default=None,
        help="Surface label for the matrix row. Defaults to provider when mode=api.",
    )
    p.add_argument("--account-state", default="fresh", choices=("fresh", "established") or None)
    p.add_argument("--machine", default=_default_machine())
    p.add_argument("--notes", default="")
    p.add_argument(
        "--matrix",
        type=Path,
        default=MATRIX_PATH,
        help=f"Path to latency_matrix.md (default: {MATRIX_PATH}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the row instead of appending to the matrix.",
    )
    p.add_argument(
        "--json-report",
        type=Path,
        default=None,
        help="Optional path to write a full JSON dump of all run details.",
    )
    return p


def _default_machine() -> str:
    """Best-effort machine identifier — short, stable, no PII."""
    try:
        uname = subprocess.run(["uname", "-sm"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"
    return uname.lower().replace(" ", "-")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.agent is None:
        args.agent = args.provider if args.mode == "api" else "manual"

    result = asyncio.run(run_cell(args))
    row = format_row(result)
    print(row)

    if args.json_report is not None:
        args.json_report.write_text(
            json.dumps(
                {
                    "cell": {k: v for k, v in asdict(result).items() if k != "runs"},
                    "runs": [asdict(r) for r in result.runs],
                },
                indent=2,
            )
        )

    if args.dry_run:
        print("\n(dry-run: matrix not modified)", file=sys.stderr)
        return 0

    append_row(args.matrix, row)
    print(f"\nappended row to {args.matrix}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
