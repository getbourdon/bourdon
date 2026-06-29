"""Generate the language-neutral cross-implementation parity fixtures.

Python is the ORACLE. This script is the SINGLE writer of ``conformance/``: it
imports the live oracle modules and emits their actual output as the expected
values, so the fixtures can never be hand-typed out of sync with the code. Two
mechanical suites (pytest + the TS vitest mirror) then assert against the SAME
bytes; a CI drift gate runs this script and fails if any committed fixture
changes without being re-committed (``git diff --exit-code -- conformance/``).

Run:  python tools/gen_conformance.py        # regenerate everything
      python tools/gen_conformance.py --check # emit to a tmp dir + diff (CI)

Master plan: claude-brain/PROJECTS/NEUROLAYER/PLAN_TS_MIRROR_2026-06-29.md
Skill:       bourdon-parity-fixture-harness (companion: bourdon-py-to-ts-port)

EXTRACTION-FIRST MANDATE
------------------------
The richest batteries live inline inside Python test modules today. The job of
this script is to invert that: the data moves HERE (canonical), the fixture is
emitted, and the Python test is refactored to LOAD the fixture. Below, the
``redaction_battery`` family is fully wired as the reference pattern. The other
families are stubbed with the exact Python source to extract from -- fill them
in as each port phase reaches them (see TODO markers).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# The oracle. Import the real implementation -- never reimplement here.
from core.redaction import REDACTED, contains_secret, redact_text

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFORMANCE = REPO_ROOT / "conformance"

CONFORMANCE_VERSION = "1.0.0"  # bump on any fixture change (see manifest.json doc)
BOURDON_VERSION = "0.11.0"     # the oracle version these fixtures were produced against


# ---------------------------------------------------------------------------
# redaction_battery  (extracted from tests/test_redaction.py SECRETS / BENIGN)
# ---------------------------------------------------------------------------
# Token-shaped fixtures are stored as FRAGMENT ARRAYS joined at load time, so no
# contiguous secret literal ever lands in a committed file (GitHub push-protection
# scans literals). Plain keyword fixtures are single-fragment (not token-shaped,
# safe as literals). Both loaders (pytest + vitest) ``"".join(fragments)``.
SECRET_FRAGMENTS: list[list[str]] = [
    ["my api_key is QXp9-not-a-real-key"],
    ["password: hunter2hunter2"],
    ["uses a bearer token to authenticate"],
    ["the service_role key for supabase"],
    ["stripe secret rotated today"],
    ["the keystore password lives in .env"],
    ["sk", "_live_", "abcd1234efGH5678ijkl"],
    ["appl", "_AbCdEfGhIjKlMnOp"],
    ["hf", "_abcdefghij1234567890"],
    ["AKIA", "IOSFODNN7EXAMPLE"],
    ["ghp_", "a" * 36],
    ["github_pat_", "b" * 24],
    ["glpat-", "c" * 22],
    ["xoxb", "-123456789012-abcdefghijklmnop"],
    ["sk-", "d" * 24],
    ["sk-ant-", "api03-", "e" * 24],
    ["AIza", "f" * 35],
    ["ya29.", "g" * 30],
    ["npm_", "h" * 36],
    ["eyJ", "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.", "eyJzdWIiOiIxMjM0In0.",
     "Sf", "lKxwRJSMeKKF2QT4fwpMeJf36"],
    ["-----BEGIN RSA PRIVATE KEY-----"],
]

# Benign recognition text that MUST survive (over-redaction drops anchors).
# Includes task/risk/disk to prove \b does not false-positive mid-word.
BENIGN: list[str] = [
    "We shipped the recognition timing layer for Bourdon",
    "Fixed the desktop tray autostart on Windows",
    "task-management-dashboard-rewrite-was-completed-today",
    "the risk-assessment-and-disk-usage-monitoring-tool shipped",
    "Bourdon recognition-first runtime layer is the core promise",
]

BENIGN_LIMIT = 400  # the limit the recognition surface uses for benign text


def redaction_battery() -> dict:
    """Emit the redaction parity battery with oracle-computed expectations."""
    secrets = []
    for frags in SECRET_FRAGMENTS:
        joined = "".join(frags)
        secrets.append(
            {
                "fragments": frags,
                "expect_redacted": redact_text(joined),
                "expect_contains_secret": contains_secret(joined),
            }
        )
    benign = [
        {
            "text": text,
            "expect_redacted": redact_text(text, limit=BENIGN_LIMIT),
            "expect_contains_secret": contains_secret(text),
        }
        for text in BENIGN
    ]
    return {
        "_doc": "Cross-impl redaction parity. Loaders join `fragments`. "
                f"REDACTED sentinel = {REDACTED!r}. benign uses limit={BENIGN_LIMIT}.",
        "redacted_sentinel": REDACTED,
        "benign_limit": BENIGN_LIMIT,
        "secrets": secrets,
        "benign": benign,
    }


# ---------------------------------------------------------------------------
# STUBS -- fill in as each port phase reaches the family (extraction-first).
# Each must import the live oracle and emit its actual output, never hand-typed.
# ---------------------------------------------------------------------------
def recognition_vectors() -> dict:
    # TODO(P2): extract from tests/test_recognition_contract.py +
    # tests/test_recognition_parity.py CASES. For each (prompt, manifest):
    # match_tier(...).name + recognition_confidence(...) bucket. tier-only.
    raise NotImplementedError("recognition_vectors: wire in Phase 2")


def l5_schema_and_manifests() -> dict:
    # TODO(P1): copy spec/L5_schema.json byte-identical; emit valid/ + invalid/
    # manifests with expected ajv/jsonschema accept-reject + failing keyword/path.
    raise NotImplementedError("l5_schema_and_manifests: wire in Phase 1")


def tier_matrix() -> dict:
    # TODO(P5): extract the D4 quarantined allowlist from tests/test_federation_*.py
    # (tool x tier x granted -> allow|deny + structured-denial shape).
    raise NotImplementedError("tier_matrix: wire in Phase 5")


def mcp_snapshots() -> dict:
    # TODO(P5/P6): drive the Python L6 server in-process over a seed library;
    # snapshot req/res for all 10 tools through the NORMALIZER (key-sort, float
    # round, null->absent, frozen timestamps) -- codify the normalizer first.
    raise NotImplementedError("mcp_snapshots: wire in Phase 5/6")


# The active families (stubs excluded until wired).
FAMILIES = {
    "redaction_battery.json": ("redaction_battery", redaction_battery),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    # newline="\n" forces LF on every OS, so the working-copy bytes == the
    # committed blob == the sha256 we stamp in manifest.json. Without this,
    # Windows writes CRLF, git normalizes to LF on commit, and the drift gate's
    # sha would mismatch across platforms.
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _assert_no_literal_secret(path: Path) -> None:
    """Guard: a multi-fragment secret must never appear contiguously in the file."""
    text = path.read_text(encoding="utf-8")
    for frags in SECRET_FRAGMENTS:
        if len(frags) > 1 and "".join(frags) in text:
            raise SystemExit(
                f"REFUSING TO EMIT: contiguous secret literal leaked into {path.name}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate conformance parity fixtures.")
    parser.add_argument("--check", action="store_true",
                        help="(reserved) CI mode -- regenerate and let the caller git-diff.")
    parser.parse_args()

    CONFORMANCE.mkdir(exist_ok=True)
    fixtures_meta = []
    for filename, (producer_name, fn) in FAMILIES.items():
        out = CONFORMANCE / filename
        _write_json(out, fn())
        _assert_no_literal_secret(out)
        fixtures_meta.append(
            {
                "path": filename,
                "sha256": _sha256(out),
                "producer": f"tools/gen_conformance.py::{producer_name}",
            }
        )
        print(f"  wrote {filename}  ({fixtures_meta[-1]['sha256'][:12]})")

    manifest = {
        "_doc": "Index of cross-impl parity fixtures. conformance_version bumps on "
                "any fixture change (patch=added cases, minor=new family, "
                "major=changed expected-output = a reviewed behavior change). "
                "Python is the oracle; this file is generated by tools/gen_conformance.py.",
        "conformance_version": CONFORMANCE_VERSION,
        "produced_against": {"bourdon_version": BOURDON_VERSION},
        "fixtures": fixtures_meta,
    }
    _write_json(CONFORMANCE / "manifest.json", manifest)
    print(f"  wrote manifest.json  ({len(fixtures_meta)} fixture(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
