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

# Make the repo root importable so this runs standalone (`python tools/gen_conformance.py`)
# from any cwd and in a CI lane that has NOT `pip install -e .`'d the package -- the drift
# gate only needs the checked-out tree (core.redaction is stdlib-only).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The oracle. Import the real implementation -- never reimplement here.
import jsonschema  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

from core.redaction import REDACTED, contains_secret, redact_text  # noqa: E402
from participants.base import (  # noqa: E402
    AgentInfo,
    Entity,
    L5Manifest,
    Session,
    Visibility,
    VisibilityPolicy,
)

CONFORMANCE = REPO_ROOT / "conformance"
SPEC_SCHEMA = REPO_ROOT / "spec" / "L5_schema.json"

CONFORMANCE_VERSION = "1.1.0"  # bump on any fixture change (see manifest.json doc)
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


# ---------------------------------------------------------------------------
# l5_schema_and_manifests  (oracle: spec/L5_schema.json + participants.base)
# ---------------------------------------------------------------------------
# Emits a multi-file tree under conformance/:
#   l5_schema.json                         -- byte-identical copy of the spec schema
#   l5_manifests/valid/*.json              -- manifests that MUST validate
#   l5_manifests/invalid/*.json            -- manifests that MUST fail
#   l5_manifests/invalid/reasons.json      -- {file: {keyword, instancePath}} (oracle-run)
#   l5_todict.json                         -- L5Manifest.to_dict() parity cases
#
# Expectations are produced by RUNNING the oracle (jsonschema validation +
# the live dataclass serializer), never hand-typed. The TS port asserts ajv
# accept/reject + (keyword, instancePath) parity and toDict byte/shape parity.

# Manifests that MUST validate (on-disk schema form).
_VALID_MANIFESTS: dict[str, dict] = {
    "minimal.json": {
        "spec_version": "0.1",
        "agent": {"id": "clyde", "type": "note-capture"},
        "last_updated": "2026-06-29T12:00:00+00:00",
    },
    "full.json": {
        "spec_version": "0.1",
        "agent": {
            "id": "claude-code",
            "type": "code-assistant",
            "instance": "pc-threadripper",
            "spec_version_compat": ">=0.1",
            "role_narrative": "Lead code-assistant. Organizes project code.",
        },
        "last_updated": "2026-06-29T12:00:00+00:00",
        "capabilities": ["code-read", "code-write", "web-search"],
        "recent_sessions": [
            {
                "date": "2026-06-28",
                "cwd": "/c/Users/cumul/repos/bourdon",
                "project_focus": ["bourdon"],
                "key_actions": ["wired l5 conformance fixtures"],
                "files_touched": ["tools/gen_conformance.py"],
                "visibility": "public",
            }
        ],
        "known_entities": [
            {
                "name": "Bourdon",
                "type": "project",
                "aliases": ["NeuroLayer", "Continuo"],
                "summary": "Cross-agent memory federation.",
                "last_touched": "2026-06-29",
                "valid_from": "2026-01-01",
                "tags": ["infra"],
                "visibility": "public",
            }
        ],
        "visibility_policy": {
            "default": "public",
            "private_tags": ["personal", "financial", "credential"],
            "team_tags": ["team"],
        },
    },
    "team-and-private.json": {
        "spec_version": "0.1",
        "agent": {"id": "clair", "type": "note-capture"},
        "last_updated": "2026-06-29T12:00:00+00:00",
        "known_entities": [
            {
                "name": "Quarterly Revenue",
                "type": "concept",
                "tags": ["financial"],
                "summary": "Q2 numbers.",
                "visibility": "private",
            },
            {"name": "Roadmap", "type": "concept", "tags": ["team"], "visibility": "team"},
            {"name": "Public Blog", "type": "site", "visibility": "public"},
        ],
        "recent_sessions": [
            {"date": "2026-06-27", "visibility": "private"},
            {"date": "2026-06-28", "visibility": "team"},
        ],
        "visibility_policy": {
            "default": "team",
            "private_tags": ["financial"],
            "team_tags": ["team"],
        },
    },
}

# Manifests that MUST fail -- each crafted to trip exactly ONE schema violation.
_INVALID_MANIFESTS: dict[str, dict] = {
    # required keyword at the document root (last_updated omitted)
    "missing-required-last_updated.json": {
        "spec_version": "0.1",
        "agent": {"id": "clyde", "type": "note-capture"},
    },
    # spec_version pattern ^\d+\.\d+$ (three-part semver rejected)
    "bad-spec-version-pattern.json": {
        "spec_version": "0.1.0",
        "agent": {"id": "clyde", "type": "note-capture"},
        "last_updated": "2026-06-29T12:00:00+00:00",
    },
    # agent.type enum
    "bad-agent-type-enum.json": {
        "spec_version": "0.1",
        "agent": {"id": "clyde", "type": "wizard"},
        "last_updated": "2026-06-29T12:00:00+00:00",
    },
    # nested $ref Visibility enum on an entity
    "entity-bad-visibility-enum.json": {
        "spec_version": "0.1",
        "agent": {"id": "clyde", "type": "note-capture"},
        "last_updated": "2026-06-29T12:00:00+00:00",
        "known_entities": [{"name": "X", "visibility": "secret"}],
    },
    # required keyword nested in an array item (entity.name omitted)
    "entity-missing-name.json": {
        "spec_version": "0.1",
        "agent": {"id": "clyde", "type": "note-capture"},
        "last_updated": "2026-06-29T12:00:00+00:00",
        "known_entities": [{"type": "project"}],
    },
}


def _json_pointer(parts) -> str:
    """ajv-style instancePath (RFC 6901 JSON Pointer) from a jsonschema path deque."""
    elems = list(parts)
    if not elems:
        return ""
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in elems)


def _vis(value):
    return Visibility(value) if value is not None else None


def _build_entity(d: dict) -> Entity:
    d = dict(d)
    if "visibility" in d:
        d["visibility"] = _vis(d["visibility"])
    return Entity(**d)


def _build_session(d: dict) -> Session:
    d = dict(d)
    if "visibility" in d:
        d["visibility"] = _vis(d["visibility"])
    return Session(**d)


def _build_policy(d):
    if d is None:
        return None
    d = dict(d)
    if "default" in d:
        d["default"] = _vis(d["default"])
    return VisibilityPolicy(**d)


def _build_manifest(spec: dict) -> L5Manifest:
    """Build an L5Manifest from a declarative spec (the same shape the TS port builds from)."""
    return L5Manifest(
        spec_version=spec["spec_version"],
        agent=AgentInfo(**spec["agent"]),
        last_updated=spec["last_updated"],
        capabilities=spec.get("capabilities", []),
        recent_sessions=[_build_session(s) for s in spec.get("recent_sessions", [])],
        known_entities=[_build_entity(e) for e in spec.get("known_entities", [])],
        visibility_policy=_build_policy(spec.get("visibility_policy")),
    )


# to_dict() parity cases. `input` is a declarative dataclass-spec the TS port
# rebuilds objects from; `expected` is produced by the live Python serializer.
# These pin the three change-detection rules: empty-list drop, None drop, and
# Visibility enum -> lowercase value.
_TODICT_SPECS: list[dict] = [
    {
        "name": "drops_empty_lists_and_none",
        "spec": {
            "spec_version": "0.1",
            "agent": {"id": "clyde", "type": "note-capture", "instance": None},
            "last_updated": "2026-06-29T12:00:00+00:00",
            "capabilities": [],
            "recent_sessions": [],
            "known_entities": [],
            "visibility_policy": None,
        },
    },
    {
        "name": "visibility_lowercased",
        "spec": {
            "spec_version": "0.1",
            "agent": {"id": "clair", "type": "note-capture"},
            "last_updated": "2026-06-29T12:00:00+00:00",
            "recent_sessions": [{"date": "2026-06-28", "visibility": "team"}],
            "known_entities": [{"name": "Secret", "visibility": "private"}],
            "visibility_policy": {"default": "public"},
        },
    },
    {
        "name": "entity_empty_inner_lists_dropped",
        "spec": {
            "spec_version": "0.1",
            "agent": {"id": "clyde", "type": "note-capture"},
            "last_updated": "2026-06-29T12:00:00+00:00",
            "known_entities": [
                {
                    "name": "Bourdon",
                    "type": "project",
                    "summary": "Federation layer.",
                    "aliases": [],
                    "tags": [],
                }
            ],
        },
    },
    {
        "name": "policy_default_injected",
        "spec": {
            "spec_version": "0.1",
            "agent": {"id": "clyde", "type": "note-capture"},
            "last_updated": "2026-06-29T12:00:00+00:00",
            "visibility_policy": {},
        },
    },
    {
        "name": "full_key_order",
        "spec": {
            "spec_version": "0.1",
            "agent": {
                "id": "claude-code",
                "type": "code-assistant",
                "instance": "pc-threadripper",
                "spec_version_compat": ">=0.1",
                "role_narrative": "Lead code-assistant.",
            },
            "last_updated": "2026-06-29T12:00:00+00:00",
            "capabilities": ["code-read", "code-write"],
            "recent_sessions": [
                {
                    "date": "2026-06-28",
                    "cwd": "/repo",
                    "project_focus": ["bourdon"],
                    "key_actions": ["ported l5"],
                    "files_touched": ["a.py"],
                    "visibility": "public",
                }
            ],
            "known_entities": [
                {
                    "name": "Bourdon",
                    "type": "project",
                    "aliases": ["NeuroLayer"],
                    "summary": "Federation.",
                    "last_touched": "2026-06-29",
                    "tags": ["infra"],
                    "visibility": "team",
                    "valid_from": "2026-01-01",
                    "valid_to": "2026-12-31",
                }
            ],
            "visibility_policy": {
                "default": "public",
                "private_tags": ["financial"],
                "team_tags": ["team"],
            },
        },
    },
]


def l5_schema_and_manifests() -> dict:
    """Emit the L5 schema + manifest validity corpus + to_dict() parity cases.

    Multi-file producer: writes its own tree and returns a marker listing the
    relative paths it wrote so ``main`` can stamp each in manifest.json.
    """
    schema = json.loads(SPEC_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )

    written: list[str] = []

    # 1. Byte-identical schema copy (raw bytes -- never re-serialized).
    schema_out = CONFORMANCE / "l5_schema.json"
    schema_out.write_bytes(SPEC_SCHEMA.read_bytes())
    written.append("l5_schema.json")

    # 2. Valid manifests -- assert each truly validates against the oracle schema.
    valid_dir = CONFORMANCE / "l5_manifests" / "valid"
    valid_dir.mkdir(parents=True, exist_ok=True)
    for fname, instance in _VALID_MANIFESTS.items():
        if not validator.is_valid(instance):
            errs = sorted(e.message for e in validator.iter_errors(instance))
            raise SystemExit(f"REFUSING TO EMIT: valid/{fname} does not validate: {errs}")
        _write_json(valid_dir / fname, instance)
        written.append(f"l5_manifests/valid/{fname}")

    # 3. Invalid manifests -- assert each fails; capture (keyword, instancePath).
    invalid_dir = CONFORMANCE / "l5_manifests" / "invalid"
    invalid_dir.mkdir(parents=True, exist_ok=True)
    reasons: dict[str, dict] = {}
    for fname, instance in _INVALID_MANIFESTS.items():
        if validator.is_valid(instance):
            raise SystemExit(f"REFUSING TO EMIT: invalid/{fname} unexpectedly validated")
        best = jsonschema.exceptions.best_match(validator.iter_errors(instance))
        all_errs = sorted(
            (
                {
                    "keyword": e.validator,
                    "instancePath": _json_pointer(e.absolute_path),
                    "message": e.message,
                }
                for e in validator.iter_errors(instance)
            ),
            key=lambda r: (r["instancePath"], str(r["keyword"])),
        )
        _write_json(invalid_dir / fname, instance)
        written.append(f"l5_manifests/invalid/{fname}")
        reasons[fname] = {
            "valid": False,
            "expected": {
                "keyword": best.validator,
                "instancePath": _json_pointer(best.absolute_path),
            },
            "errors": all_errs,
        }
    _write_json(invalid_dir / "reasons.json", {
        "_doc": "Each invalid manifest's expected primary failure. `expected` "
                "(keyword + ajv-style instancePath) is the cross-impl assertion; "
                "`errors` lists every violation for debugging. Oracle: "
                "jsonschema Draft202012Validator over spec/L5_schema.json.",
        "reasons": reasons,
    })
    written.append("l5_manifests/invalid/reasons.json")

    # 4. to_dict() parity cases -- expectations from the live serializer.
    cases = []
    for case in _TODICT_SPECS:
        manifest = _build_manifest(case["spec"])
        cases.append(
            {
                "name": case["name"],
                "input": case["spec"],
                "expected": manifest.to_dict(),
            }
        )
    _write_json(CONFORMANCE / "l5_todict.json", {
        "_doc": "L5Manifest.to_dict() parity. The TS port rebuilds objects from "
                "`input` (declarative dataclass-spec: visibility as a lowercase "
                "string, null=None, []=empty list) and asserts toDict()==`expected`. "
                "Rules pinned: drop None fields, drop empty-list fields, Visibility "
                "enum -> lowercase value, key order = dataclass field order, and "
                "VisibilityPolicy.default always emitted (defaults to 'public').",
        "cases": cases,
    })
    written.append("l5_todict.json")

    return {"__multifile__": True, "files": written}


def tier_matrix() -> dict:
    # TODO(P5): extract the D4 quarantined allowlist from tests/test_federation_*.py
    # (tool x tier x granted -> allow|deny + structured-denial shape).
    raise NotImplementedError("tier_matrix: wire in Phase 5")


def mcp_snapshots() -> dict:
    # TODO(P5/P6): drive the Python L6 server in-process over a seed library;
    # snapshot req/res for all 10 tools through the NORMALIZER (key-sort, float
    # round, null->absent, frozen timestamps) -- codify the normalizer first.
    raise NotImplementedError("mcp_snapshots: wire in Phase 5/6")


# The active families (stubs excluded until wired). Single-file producers
# return a payload dict; multi-file producers (l5) write their own tree and
# return {"__multifile__": True, "files": [...]}.
FAMILIES = {
    "redaction_battery.json": ("redaction_battery", redaction_battery),
    "l5_schema_and_manifests": ("l5_schema_and_manifests", l5_schema_and_manifests),
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
        result = fn()
        if isinstance(result, dict) and result.get("__multifile__"):
            # Producer already wrote its own tree; stamp each emitted file.
            for rel in result["files"]:
                out = CONFORMANCE / rel
                fixtures_meta.append(
                    {
                        "path": rel,
                        "sha256": _sha256(out),
                        "producer": f"tools/gen_conformance.py::{producer_name}",
                    }
                )
                print(f"  wrote {rel}  ({fixtures_meta[-1]['sha256'][:12]})")
            continue
        out = CONFORMANCE / filename
        _write_json(out, result)
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
