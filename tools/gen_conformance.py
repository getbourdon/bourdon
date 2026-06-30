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
import os
import sys
from pathlib import Path
from typing import Any

import yaml  # noqa: E402  (third-party, but a hard dep of the oracle)

# Make the repo root importable so this runs standalone (`python tools/gen_conformance.py`)
# from any cwd and in a CI lane that has NOT `pip install -e .`'d the package -- the drift
# gate only needs the checked-out tree (core.redaction is stdlib-only).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# The oracle. Import the real implementation -- never reimplement here.
import jsonschema  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

from core.leak_audit import (  # noqa: E402
    AUDIT_SCHEMA_VERSION,
    PRIVATE_TAG_FAMILIES,
    audit_manifest,
)
from core.recognition_contract import (  # noqa: E402
    MatchTier,
    best_match_tier,
    normalized_confidence,
    recognition_confidence,
)
from core.recognition_runtime import recognition_first  # noqa: E402
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

CONFORMANCE_VERSION = "1.7.0"  # bump on any fixture change (see manifest.json doc)
# 1.7.0: + the turn_compiler_vectors family (P7 turn compilers): the REAL
#        compile_codex_turn / compile_cursor_turn driven over a dedicated 2-agent
#        seed with the wall clock FROZEN (deterministic recency). Pins per case the
#        BriefItem scores (round 1dp), the tier-only recognition_confidence bucket,
#        and the codex TurnBrief.to_dict() (resolved cwd swapped for the logical
#        input -- the only env-bound field; repo.root/remote null for the synthetic
#        non-git cwd). The pinned codex_cwd_hit item folds cwd-hit (25) + recency
#        (15) + a NAME_SUBSTRING tier into one session item. This replaces the
#        deferred compile_codex_turn stub the mcp_snapshots family still carries.
# 1.6.0: + the native_stores family (participant parity): each external-agent
#        reader (hermes=sqlite, claude_code=file, github_copilot=network) gets a
#        seeded hermetic native store + the REAL participant.export_l5() to_dict
#        pinned as expected_l5.json (last_updated + claude_code's agent.instance
#        frozen to fixed valid values; every other field a pure function of the
#        store). Readers redact native strings themselves; seeds use keyword-only
#        credential triggers so no token literal lands in a store fixture.
# 1.5.0: + the mcp_snapshots family (the JSON-in-TextContent wire contract): the
#        live create_l6_server tool closures driven in-process over a temp copy of
#        fed_seed_library, with each of the 10 tools' req/res snapshotted through a
#        codified NORMALIZER (key-sort, float-round, null/empty-list drop, freeze
#        last_updated/generated_at/generated_from/path, drop latency, decode base64
#        cursors). compile_codex_turn is the deferred P7 stub (env-bound brief).
# 1.4.0: + the L6 FEDERATION families (the cross-machine trust boundary):
#        tier_matrix (D4 tool x trust-tier x granted -> allow|deny + structured
#        denial, oracle-driven through the real create_l6_server enforcement),
#        fed_seed_library (a 2-agent seeded agent-library with public/team/private
#        entities + sessions for query-parity), and on_disk/ (a Python-WRITTEN
#        federation.yaml registry + audit.jsonl + auth_vectors the TS side must
#        parse identically -- synthetic bdn_ tokens only, sha256-at-rest).
# 1.3.0: + leak_cases family (core.leak_audit parity) and a case_variants section
#        in redaction_battery pinning per-token-pattern case sensitivity.
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

# Case-sensitivity probes -- one per TOKEN pattern. `correct` is a known-good
# token (stored as fragments, joined at load -- never a contiguous literal);
# `wrong` is the SAME token with its case-bearing prefix flipped. For the
# case-SENSITIVE patterns (every token pattern EXCEPT appl_/hf_, which compile
# with re.IGNORECASE) the wrong case MUST NOT redact -- that is the per-flag
# pin. For appl_/hf_ the wrong case MUST STILL redact (proving the `i` flag).
# Expectations are oracle-computed below and self-checked, so a pattern that
# silently loses/gains its IGNORECASE flag fails generation.
#   (name, case_sensitive, correct_fragments, wrong_fragments)
_CASE_VARIANT_PROBES: list[tuple[str, bool, list[str], list[str]]] = [
    ("stripe_sk", True, ["sk", "_live_", "abcd1234efGH5678ijkl"],
     ["SK_LIVE_ABCD1234EFGH5678IJKL"]),
    ("revenuecat_appl", False, ["appl", "_AbCdEfGhIjKlMnOp"],
     ["APPL", "_ABCDEFGHIJKLMNOP"]),
    ("huggingface_hf", False, ["hf", "_abcdefghij1234567890"],
     ["HF", "_ABCDEFGHIJ1234567890"]),
    ("aws_akia", True, ["AKIA", "IOSFODNN7EXAMPLE"], ["akiaiosfodnn7example"]),
    ("github_ghp", True, ["ghp_", "a" * 36], ["GHP_" + "A" * 36]),
    ("github_pat", True, ["github_pat_", "b" * 24], ["GITHUB_PAT_" + "B" * 24]),
    ("gitlab_glpat", True, ["glpat-", "c" * 22], ["GLPAT-" + "C" * 22]),
    ("slack_xoxb", True, ["xoxb", "-123456789012-abcdefghijklmnop"],
     ["XOXB-123456789012-ABCDEFGHIJKLMNOP"]),
    ("openai_anthropic_sk", True, ["sk-", "d" * 24], ["SK-" + "D" * 24]),
    ("google_aiza", True, ["AIza", "f" * 35], ["AIZA" + "F" * 35]),
    ("google_ya29", True, ["ya29.", "g" * 30], ["YA29." + "G" * 30]),
    ("npm_token", True, ["npm_", "h" * 36], ["NPM_" + "H" * 36]),
    ("jwt", True,
     ["eyJ", "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.", "eyJzdWIiOiIxMjM0In0.",
      "Sf", "lKxwRJSMeKKF2QT4fwpMeJf36"],
     ["EYJHBGCIOIJIUZI1NIISINR5CCI6IKPXVCJ9.EYJZDWIIOIIXMJM0IN0.SFLKXWRJSMEKKF2QT4FWPMEJF36"]),
    ("pem_private_key", True, ["-----BEGIN RSA PRIVATE KEY-----"],
     ["-----begin rsa private key-----"]),
]


def _case_variant_probe(name: str, case_sensitive: bool,
                        correct: list[str], wrong: list[str]) -> dict:
    """One case-sensitivity probe, oracle-computed and self-checked."""
    correct_joined = "".join(correct)
    wrong_joined = "".join(wrong)
    correct_secret = contains_secret(correct_joined)
    wrong_secret = contains_secret(wrong_joined)
    # Self-checks: make the generator authoritative so a mis-cased probe (or a
    # flag regression) can never be committed as a green fixture.
    if not correct_secret:
        raise SystemExit(
            f"REFUSING TO EMIT: case_variant {name!r} correct case did not redact"
        )
    if case_sensitive and wrong_secret:
        raise SystemExit(
            f"REFUSING TO EMIT: case_variant {name!r} is declared case-sensitive "
            "but the wrong case still matched a pattern"
        )
    if not case_sensitive and not wrong_secret:
        raise SystemExit(
            f"REFUSING TO EMIT: case_variant {name!r} is declared IGNORECASE but "
            "the wrong case did not match"
        )
    return {
        "pattern": name,
        "case_sensitive": case_sensitive,
        "correct": {
            "fragments": correct,
            "expect_redacted": redact_text(correct_joined),
            "expect_contains_secret": correct_secret,
        },
        "wrong": {
            "fragments": wrong,
            "expect_redacted": redact_text(wrong_joined),
            "expect_contains_secret": wrong_secret,
        },
    }


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
    case_variants = [
        _case_variant_probe(name, cs, correct, wrong)
        for name, cs, correct, wrong in _CASE_VARIANT_PROBES
    ]
    return {
        "_doc": "Cross-impl redaction parity. Loaders join `fragments`. "
                f"REDACTED sentinel = {REDACTED!r}. benign uses limit={BENIGN_LIMIT}. "
                "case_variants: one probe per TOKEN pattern -- `correct` always "
                "redacts; for case_sensitive patterns `wrong` (case-flipped prefix) "
                "must NOT redact, for the IGNORECASE patterns (appl_/hf_) `wrong` "
                "still redacts. Pins the per-pattern case flag across impls.",
        "redacted_sentinel": REDACTED,
        "benign_limit": BENIGN_LIMIT,
        "secrets": secrets,
        "benign": benign,
        "case_variants": case_variants,
    }


# ---------------------------------------------------------------------------
# leak_cases  (extracted from tests/test_leak_audit.py)
# ---------------------------------------------------------------------------
# Each case feeds a manifest TREE through the live core.leak_audit.audit_manifest
# oracle and pins the resulting findings as [{kind, location}]. kind + location
# are the cross-impl assertion: agent_file is just the passed-in filename and
# `detail` is human prose, so neither is pinned. Findings keep the oracle's
# emission order (visibility entities, then visibility sessions, then the total
# credential walk) -- order is contract.
#
# Credential cases use KEYWORD-shaped triggers (".env", "service_role", "api_key",
# "bearer token") -- never a token-shaped literal -- so no contiguous secret can
# land in a committed fixture (harness rule #3). The per-token-pattern parity
# (incl. case sensitivity) lives in redaction_battery; here we prove the auditor
# wires contains_secret across the WHOLE federated string tree and resolves
# visibility the way the emitters do.
_LEAK_CASES: list[tuple[str, Any]] = [
    # CREDENTIAL: keyword trigger in an entity summary -- the canonical leak.
    ("credential_in_summary", {
        "known_entities": [
            {"name": "API", "summary": "the service_role key for supabase",
             "visibility": "team"},
        ],
    }),
    # CREDENTIAL: a .env path deep in a session's files_touched list -- proves the
    # walk is total (the pre-SSOT allowlist silently skipped files_touched).
    ("credential_in_files_touched", {
        "recent_sessions": [
            {"date": "2026-06-20", "files_touched": ["src/app.ts", "config/.env"]},
        ],
    }),
    # CREDENTIAL: agent.role_narrative -- federated free-text the old allowlist
    # never scanned.
    ("credential_in_role_narrative", {
        "agent": {"id": "x", "type": "code-assistant",
                  "role_narrative": "rotate the api_key before every deploy"},
    }),
    # CREDENTIAL: a top-level scalar list -- the credential walk reaches everything.
    ("credential_in_top_level_capabilities", {
        "capabilities": ["state_db", "the bearer token is read from .env"],
    }),
    # VISIBILITY: an explicitly private entity riding in a federated manifest.
    ("private_entity_explicit", {
        "known_entities": [
            {"name": "SecretProj", "type": "project", "visibility": "private"},
        ],
    }),
    # VISIBILITY: a private-tag-FAMILY entity (a tag forces private regardless of
    # the declared visibility).
    ("private_tag_family_entity", {
        "known_entities": [
            {"name": "Payroll", "type": "concept", "tags": ["financial"]},
        ],
    }),
    # VISIBILITY: a private-tagged session.
    ("private_tagged_session", {
        "recent_sessions": [
            {"date": "2026-06-20", "tags": ["personal"]},
        ],
    }),
    # VISIBILITY: the auditor unions the manifest's OWN declared private_tags, so a
    # custom tag the families don't hardcode is still caught -- and the
    # visibility_policy block itself is never credential-scanned.
    ("manifest_declared_private_tag", {
        "known_entities": [
            {"name": "Vault", "type": "project", "tags": ["client-confidential"]},
        ],
        "visibility_policy": {"default": "team",
                              "private_tags": ["client-confidential"]},
    }),
    # CLEAN: nothing private, no secrets -> zero findings.
    ("clean_manifest", {
        "known_entities": [
            {"name": "Bourdon", "type": "project",
             "summary": "memory federation", "visibility": "team"},
        ],
        "recent_sessions": [
            {"date": "2026-06-20", "key_actions": ["refactored the L6 store"],
             "visibility": "team"},
        ],
    }),
    # CLEAN: visibility_policy enumerates tag-family NAMES (declarations, not
    # content), so naming "credential"/"secret"/"private_key" must NOT self-flag.
    ("policy_names_not_self_flagged", {
        "known_entities": [{"name": "Bourdon", "type": "project",
                            "visibility": "team"}],
        "visibility_policy": {
            "default": "team",
            "private_tags": ["credential", "secret", "password", "private_key"],
        },
    }),
    # GARBAGE: a non-dict manifest never raises and yields nothing.
    ("garbage_non_dict_manifest", "not a dict"),
    # GARBAGE: malformed collections (wrong element/collection types) are skipped,
    # not crashed on.
    ("garbage_malformed_collections", {
        "known_entities": "nope",
        "recent_sessions": [None, 42],
    }),
]


def leak_cases() -> dict:
    """Emit the federation leak-audit parity cases with oracle-computed findings."""
    cases = []
    for name, manifest in _LEAK_CASES:
        findings = audit_manifest(manifest, f"{name}.l5.yaml")
        cases.append(
            {
                "name": name,
                "manifest": manifest,
                "expected_findings": [
                    {"kind": f.kind.value, "location": f.location} for f in findings
                ],
            }
        )
    return {
        "_doc": "Cross-impl federation leak-audit parity. Oracle = "
                "core.leak_audit.audit_manifest. Each case feeds `manifest` through "
                "the auditor; `expected_findings` pins [{kind, location}] in the "
                "oracle's emission order (visibility entities, visibility sessions, "
                "then the total credential walk). kind in {'credential','visibility'}; "
                "location is a json-path like 'known_entities[0].summary'. "
                "audit_manifest NEVER raises -- garbage manifests yield []. Credential "
                "cases use keyword-shaped triggers only; no token literal is committed.",
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "private_tag_families": sorted(PRIVATE_TAG_FAMILIES),
        "cases": cases,
    }


# ---------------------------------------------------------------------------
# STUBS -- fill in as each port phase reaches the family (extraction-first).
# Each must import the live oracle and emit its actual output, never hand-typed.
# ---------------------------------------------------------------------------
# recognition_vectors  (extracted from tests/test_recognition_{contract,parity}.py
# CASES + BENCHMARKS/recognition_golden_v1.yaml). Three vector families, all
# oracle-computed:
#   tier_vectors          -- match_tier ladder + TIER-ONLY recognition_confidence
#                            (the short-name guard, substring-not-token, alias
#                            best-tier, and "2 shared terms still buckets low
#                            because confidence is tier-only" cases).
#   confidence_buckets    -- normalized_confidence boundary arithmetic, incl. the
#                            EXACT 0.45 (TOKEN_OVERLAP + 2 anchor terms) and 0.80
#                            (NAME_SUBSTRING + recency) bucket edges.
#   recognition_strings   -- recognition_first end-to-end: detect_entities +
#                            build_recognition_string + visibility filtering +
#                            the temporal (archived) suffix + per-entity buckets.

# (prompt, names) -> best_match_tier(prompt, names).name +
# recognition_confidence(prompt, names) (TIER-ONLY: no cwd/recency/anchor-count).
_TIER_CASES: list[tuple[str, list[str]]] = [
    ("Bourdon", ["bourdon"]),                                # EXACT / high
    ("tell me about Bourdon", ["Bourdon"]),                  # NAME_SUBSTRING / medium
    ("how is ILTT going", ["ILTT"]),                         # NAME_SUBSTRING / medium
    ("we ILTTed the build", ["ILTT"]),                       # short-name guard: NONE / none
    ("pick a category", ["cat"]),                            # substring-not-token: NONE / none
    ("the federation substrate work", ["Bourdon federation engine"]),  # TOKEN_OVERLAP / low
    # 2 shared meaningful terms, but recognition_confidence is TIER-ONLY so it
    # still buckets `low` (n_anchor_terms is not folded into the emitted bucket).
    ("the federation memory substrate", ["Bourdon federation memory"]),
    ("we shipped DINOs Chess tonight", ["DINOs Chess"]),     # NAME_SUBSTRING / medium
    ("checkers tonight", ["DINOs Chess", "checkers"]),       # alias best-tier: NAME_SUBSTRING
    ("completely unrelated words", ["Bourdon"]),             # NONE / none
    # whole-token NAS matches; "bananas" does not -> NAME_SUBSTRING / medium
    ("i deployed to a NAS box, the bananas were fine", ["NAS"]),
    ("where are we on the Multi Word Project", ["Multi Word Project"]),  # NAME_SUBSTRING
]

# (tier, kwargs) -> normalized_confidence(tier, **kwargs). Pins the bucket
# arithmetic INCLUDING the present-signal lifts the tier-only emitted bucket does
# NOT use -- the two boundaries are load-bearing: 0.45 (low->medium) and 0.80
# (medium->high).
_CONFIDENCE_CASES: list[tuple[MatchTier, dict]] = [
    (MatchTier.NONE, {}),                                            # none
    (MatchTier.TOKEN_OVERLAP, {"n_anchor_terms": 1}),               # 0.30 -> low
    (MatchTier.TOKEN_OVERLAP, {"n_anchor_terms": 2}),               # 0.45 -> medium (ON 0.45 edge)
    (MatchTier.TOKEN_SUBSEQUENCE, {}),                              # 0.55 -> medium
    (MatchTier.NAME_SUBSTRING, {}),                                 # 0.75 -> medium (below 0.80)
    (MatchTier.NAME_SUBSTRING, {"recency_fresh": True}),            # 0.80 -> high (ON 0.80 edge)
    (MatchTier.NAME_SUBSTRING, {"cwd_hit": True}),                  # 0.85 -> high
    (MatchTier.EXACT, {}),                                          # 0.90 -> high
    # 0.30 + 0.15 + 0.10 + 0.05 = 0.60 -> medium
    (MatchTier.TOKEN_OVERLAP, {"n_anchor_terms": 2, "cwd_hit": True, "recency_fresh": True}),
]

# (name, prompt, manifest, access_level) -> recognition_first(...) end-to-end.
_RECOGNITION_STRING_CASES: list[tuple[str, str, dict, str]] = [
    ("single_with_type", "tell me about Bourdon",
     {"known_entities": [{"name": "Bourdon", "type": "project"}]}, "team"),
    ("single_no_type", "status on Alpha please",
     {"known_entities": [{"name": "Alpha"}]}, "team"),
    ("two_matches", "compare Bourdon and OMNIvour for me",
     {"known_entities": [{"name": "Bourdon", "type": "project"},
                         {"name": "OMNIvour", "type": "project"},
                         {"name": "Unrelated", "type": "project"}]}, "team"),
    ("three_matches", "status on Alpha, Beta, and Gamma please",
     {"known_entities": [{"name": "Alpha"}, {"name": "Beta"},
                         {"name": "Gamma"}, {"name": "Delta"}]}, "team"),
    ("short_name_guard", "the build ILTTed yesterday and broke",
     {"known_entities": [{"name": "ILTT", "type": "product"}]}, "team"),
    ("substring_not_token", "i deployed to a NAS box, the bananas were fine",
     {"known_entities": [{"name": "NAS", "type": "hardware"}]}, "team"),
    ("visibility_private_hidden", "tell me about SecretSauce and PublicProj",
     {"known_entities": [
         {"name": "PublicProj", "type": "project"},
         {"name": "SecretSauce", "type": "project", "visibility": "private"},
     ]}, "team"),
    ("archived_valid_to", "remind me what Coolculator was",
     {"known_entities": [
         {"name": "Coolculator", "type": "project", "valid_to": "2026-01-01"},
     ]}, "team"),
    ("archived_tag", "what about LegacyThing",
     {"known_entities": [
         {"name": "LegacyThing", "type": "tool", "tags": ["archived"]},
     ]}, "team"),
    ("alias_match", "any update on if_lift_then_that",
     {"known_entities": [
         {"name": "ILTT", "type": "product", "aliases": ["if_lift_then_that"]},
     ]}, "team"),
    ("negative_control", "what is the weather like today",
     {"known_entities": [{"name": "Bourdon"}, {"name": "OMNIvour"}]}, "team"),
    ("multi_word", "where are we on the Multi Word Project",
     {"known_entities": [{"name": "Multi Word Project", "type": "project"}]}, "team"),
]


def recognition_vectors() -> dict:
    """Emit the recognition parity vectors + the byte-identical golden copy.

    Multi-file producer: writes recognition_vectors.json (oracle-computed tier /
    confidence / recognition-string vectors) and a byte-for-byte copy of the
    BENCHMARKS golden dataset (the F1==1.0 gate input), and returns the marker
    listing both so `main` stamps each in manifest.json.
    """
    tier_vectors = [
        {
            "prompt": prompt,
            "names": names,
            "tier": best_match_tier(prompt, names).name,
            "confidence": recognition_confidence(prompt, names),
        }
        for prompt, names in _TIER_CASES
    ]

    confidence_buckets = [
        {
            "tier": tier.name,
            "n_anchor_terms": kwargs.get("n_anchor_terms", 1),
            "cwd_hit": kwargs.get("cwd_hit", False),
            "recency_fresh": kwargs.get("recency_fresh", False),
            "bucket": normalized_confidence(tier, **kwargs),
        }
        for tier, kwargs in _CONFIDENCE_CASES
    ]

    recognition_strings = []
    for name, prompt, manifest, access in _RECOGNITION_STRING_CASES:
        result = recognition_first(prompt, manifest, access_level=access)
        # Close the un-awaited hydration coroutine (no event loop touched), per
        # the recognition-eval pattern -- we score recognition, not hydration.
        close = getattr(result.hydration, "close", None)
        if callable(close):
            close()
        recognition_strings.append(
            {
                "name": name,
                "prompt": prompt,
                "manifest": manifest,
                "access_level": access,
                "matched_names": [str(e.get("name") or "") for e in result.matched_entities],
                "recognition": result.recognition,
                "confidence": result.confidence,
                "entity_confidences": result.entity_confidences,
            }
        )

    payload = {
        "_doc": "Cross-impl recognition parity. Python is the oracle. "
                "tier_vectors: best_match_tier(prompt,names).name + TIER-ONLY "
                "recognition_confidence(prompt,names). confidence_buckets: "
                "normalized_confidence(tier, n_anchor_terms/cwd_hit/recency_fresh) "
                "-- pins the 0.45 and 0.80 bucket edges. recognition_strings: "
                "recognition_first(prompt,manifest,access_level) end-to-end "
                "(detect + build_recognition_string + visibility + archived suffix).",
        "tier_vectors": tier_vectors,
        "confidence_buckets": confidence_buckets,
        "recognition_strings": recognition_strings,
    }

    written: list[str] = []
    out = CONFORMANCE / "recognition_vectors.json"
    _write_json(out, payload)
    written.append("recognition_vectors.json")

    # Byte-identical copy of the golden dataset (the F1==1.0 eval gate input).
    # Raw bytes -- never re-serialized -- so the TS mirror's eval runs the SAME
    # cases and the parity is on the dataset itself, not a re-emission of it.
    golden_src = REPO_ROOT / "BENCHMARKS" / "recognition_golden_v1.yaml"
    golden_out = CONFORMANCE / "recognition_golden_v1.yaml"
    golden_out.write_bytes(golden_src.read_bytes())
    written.append("recognition_golden_v1.yaml")

    return {"__multifile__": True, "files": written}


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


# ---------------------------------------------------------------------------
# fed_seed_library  (a 2-agent seeded agent-library for query + tier parity)
# ---------------------------------------------------------------------------
# These are INPUT fixtures (read by L6Store), not oracle output: a small,
# stable library spread across two agents with public / team / private entities
# and sessions, plus a cross-agent shared entity name ("Bourdon") and a same-date
# session pair (2026-06-08) so the visibility filter, the find_entity merge, and
# the list_recent_work stable-sort/cursor are all exercised by one corpus. The
# tier_matrix family drives the REAL server over a temp copy of this same seed,
# so the two federation families stay coherent.
#
# claude-code is the GRANTED namespace for the quarantined caller; codex is the
# UNGRANTED one. "SecretProj" is known ONLY by codex, so a quarantined
# find_entity for it must come back empty (filtered to granted agents).
_SEED_MANIFESTS: dict[str, dict] = {
    "claude-code.l5.yaml": {
        "spec_version": "0.1",
        "agent": {
            "id": "claude-code",
            "type": "code-assistant",
            "instance": "pc-threadripper",
            "role_narrative": "Lead code-assistant on the federation substrate.",
        },
        "last_updated": "2026-06-29T12:00:00+00:00",
        "capabilities": ["code-read", "code-write"],
        "known_entities": [
            {
                "name": "Bourdon",
                "type": "project",
                "aliases": ["NeuroLayer", "Continuo"],
                "summary": "Cross-agent memory federation (granted side).",
                "tags": ["infra"],
                "visibility": "public",
            },
            {
                "name": "Roadmap",
                "type": "concept",
                "summary": "Phase 1.7 federation roadmap.",
                "tags": ["team"],
                "visibility": "team",
            },
            {
                "name": "Quarterly Revenue",
                "type": "concept",
                "summary": "Q2 numbers.",
                "tags": ["financial"],
                "visibility": "private",
            },
        ],
        "recent_sessions": [
            {
                "date": "2026-06-08",
                "cwd": "/c/Users/cumul/repos/bourdon",
                "project_focus": ["Bourdon"],
                "key_actions": ["wired tier matrix fixtures"],
                "files_touched": ["tools/gen_conformance.py"],
                "visibility": "public",
            },
            {
                "date": "2026-06-07",
                "cwd": "/c/Users/cumul/repos/bourdon",
                "project_focus": ["Roadmap"],
                "key_actions": ["team planning"],
                "visibility": "team",
            },
            {
                "date": "2026-06-06",
                "cwd": "/c/Users/cumul/repos/bourdon",
                "project_focus": ["Quarterly Revenue"],
                "key_actions": ["private review"],
                "visibility": "private",
            },
        ],
        "visibility_policy": {
            "default": "public",
            "private_tags": ["financial", "personal"],
            "team_tags": ["team"],
        },
    },
    "codex.l5.yaml": {
        "spec_version": "0.1",
        "agent": {"id": "codex", "type": "code-assistant"},
        "last_updated": "2026-06-29T12:00:00+00:00",
        "known_entities": [
            {
                "name": "SecretProj",
                "type": "project",
                "summary": "Known only by codex (ungranted namespace).",
                "visibility": "public",
            },
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Federation, codex view.",
                "visibility": "public",
            },
            {
                "name": "Internal Notes",
                "type": "concept",
                "summary": "codex private.",
                "tags": ["personal"],
                "visibility": "private",
            },
        ],
        "recent_sessions": [
            {
                "date": "2026-06-08",
                "cwd": "/y",
                "project_focus": ["SecretProj"],
                "key_actions": ["codex work"],
                "visibility": "public",
            },
            {
                "date": "2026-06-05",
                "cwd": "/y",
                "project_focus": ["Internal Notes"],
                "key_actions": ["codex private"],
                "visibility": "team",
            },
        ],
    },
}


def _write_text_lf(path: Path, text: str) -> None:
    """Write text with hard LF endings (cross-platform stable sha, see _write_json)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(normalized, encoding="utf-8", newline="\n")


def _l5_validator() -> Draft202012Validator:
    schema = json.loads(SPEC_SCHEMA.read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def fed_seed_library() -> dict:
    """Emit the seeded agent-library (2 agents) for federation query parity.

    Each manifest is validated against the live L5 schema before it lands, so a
    malformed seed can never be committed. Written as LF yaml so the bytes are
    identical on every OS (the TS mirror loads the same files for its query
    parity tests).
    """
    validator = _l5_validator()
    agents_dir = CONFORMANCE / "fed_seed_library" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for fname, manifest in _SEED_MANIFESTS.items():
        if not validator.is_valid(manifest):
            errs = sorted(e.message for e in validator.iter_errors(manifest))
            raise SystemExit(f"REFUSING TO EMIT: seed {fname} does not validate: {errs}")
        text = yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
        _write_text_lf(agents_dir / fname, text)
        written.append(f"fed_seed_library/agents/{fname}")
    return {"__multifile__": True, "files": written}


# ---------------------------------------------------------------------------
# tier_matrix  (D4 enforcement: tool x trust-tier x granted -> allow|deny)
# ---------------------------------------------------------------------------
# Oracle = the REAL create_l6_server enforcement closures. We stand up the live
# FastMCP server over a temp copy of fed_seed_library with the real
# FederationRegistry (a quarantined `openclaw` granted ONLY `claude-code`) and a
# real FederationAudit, then invoke every tool fn under each caller identity --
# exactly as tests/test_federation_tiers.py does -- and record the resulting
# decision plus the verbatim structured-denial dict. Nothing here re-implements
# the policy: the allow/deny call and the denial shape are whatever the server
# actually returns.
#
# trusted  = the default OPERATOR identity (stdio / legacy peer = v0.8.0 behavior)
# quarantined = AgentIdentity(openclaw, grants=(claude-code,))
#
# `granted`: True/False for namespace-scoped tools (query_agent_memory,
# list_recent_work with an explicit agent, commit_to_federation namespace),
# None for the non-namespace-scoped tools (the aggregates that deny wholesale,
# and the filtered reads that allow-then-filter).

# (tool, kwargs, namespace, granted) -- the quarantined caller's surface.
_QUARANTINED_CASES: list[tuple[str, dict, str | None, bool | None]] = [
    ("query_agent_memory", {"agent": "claude-code", "topic": "Bourdon"}, "claude-code", True),
    ("query_agent_memory", {"agent": "codex", "topic": "Bourdon"}, "codex", False),
    ("list_recent_work", {"since": "2026-06-01"}, None, None),
    ("list_recent_work", {"since": "2026-06-01", "agent": "claude-code"}, "claude-code", True),
    ("list_recent_work", {"since": "2026-06-01", "agent": "codex"}, "codex", False),
    ("find_entity", {"name": "Bourdon"}, None, None),
    ("find_entity", {"name": "SecretProj"}, None, None),
    ("list_agents", {}, None, None),
    ("export_agents", {}, None, None),
    ("commit_to_federation",
     {"agent_id": "openclaw", "agent_type": "other",
      "entities": [{"name": "ClawFinding", "type": "topic", "summary": "from openclaw"}],
      "sessions": [{"date": "2026-06-09"}]},
     "openclaw", True),
    ("commit_to_federation",
     {"agent_id": "claude-code", "agent_type": "code-assistant",
      "entities": [{"name": "Poisoned", "summary": "injected"}]},
     "claude-code", False),
    ("get_cross_agent_summary", {"project": "Bourdon"}, None, None),
    ("prepare_recognition_context", {"prompt": "what about Bourdon"}, None, None),
    ("get_deeper_context", {"prompt": "what about Bourdon"}, None, None),
    ("compile_codex_turn", {"prompt": "what about Bourdon"}, None, None),
]

# (tool, kwargs, namespace, granted) -- the trusted operator's surface (all allow).
_TRUSTED_CASES: list[tuple[str, dict, str | None, bool | None]] = [
    ("query_agent_memory", {"agent": "claude-code", "topic": "Bourdon"}, "claude-code", None),
    ("query_agent_memory", {"agent": "codex", "topic": "Bourdon"}, "codex", None),
    ("list_recent_work", {"since": "2026-06-01"}, None, None),
    ("list_recent_work", {"since": "2026-06-01", "agent": "codex"}, "codex", None),
    ("find_entity", {"name": "Bourdon"}, None, None),
    ("list_agents", {}, None, None),
    ("export_agents", {}, None, None),
    ("commit_to_federation",
     {"agent_id": "clyde", "agent_type": "other",
      "entities": [{"name": "TrustedThing", "summary": "ok"}]},
     "clyde", None),
    ("get_cross_agent_summary", {"project": "Bourdon"}, None, None),
    ("prepare_recognition_context", {"prompt": "what about Bourdon"}, None, None),
    ("get_deeper_context", {"prompt": "what about Bourdon"}, None, None),
    ("compile_codex_turn", {"prompt": "what about Bourdon"}, None, None),
]


def _invoke_tool(server, name: str, identity, kwargs: dict):
    """Call one MCP tool fn under an optional caller identity (mirrors the test)."""
    import asyncio

    from core.federation_registry import reset_caller, set_caller

    async def _inner():
        tool = await server.get_tool(name)
        res = tool.fn(**kwargs)
        if asyncio.iscoroutine(res):
            res = await res
        return res

    ctx = set_caller(identity) if identity is not None else None
    try:
        return asyncio.run(_inner())
    finally:
        if ctx is not None:
            reset_caller(ctx)


def _classify(result: Any) -> tuple[str, dict | None]:
    if isinstance(result, dict) and result.get("error") == "access denied":
        return "deny", result
    return "allow", None


def tier_matrix() -> dict:
    """Drive the real D4 enforcement and pin tool x tier x granted -> allow|deny.

    The denial dict is captured verbatim so the TS mirror reproduces the exact
    structured-denial envelope (error/op/agent/tier/detail, plus the empty
    sessions page list_recent_work folds in). Run against a TEMP copy of the
    seed so the quarantined staged write never pollutes the committed library.
    """
    import shutil
    import tempfile

    from core.federation_audit import FederationAudit
    from core.federation_registry import AgentIdentity, FederationRegistry
    from core.l6_store import L6Store

    try:
        from core.l6_server import create_l6_server
    except ImportError as exc:  # pragma: no cover -- needs the [server] extra
        raise SkipFamilyError(
            "tier_matrix needs fastmcp (pip install 'bourdon[server]') to drive the "
            f"real L6 enforcement: {exc}"
        ) from exc

    quarantined = AgentIdentity(
        agent_id="openclaw", tier="quarantined", grants=("claude-code",)
    )

    with tempfile.TemporaryDirectory() as td:
        lib = Path(td) / "lib"
        agents = lib / "agents"
        agents.mkdir(parents=True)
        for fname, manifest in _SEED_MANIFESTS.items():
            _write_text_lf(
                agents / fname,
                yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
            )
        registry = FederationRegistry(Path(td) / "federation.yaml")
        registry.add_agent("openclaw", tier="quarantined", grants=["claude-code"])
        audit = FederationAudit(Path(td) / "audit.jsonl")
        store = L6Store(lib)
        server = create_l6_server(store, registry=registry, audit=audit)

        cases: list[dict] = []
        for tier, identity, raw_cases in (
            ("trusted", None, _TRUSTED_CASES),
            ("quarantined", quarantined, _QUARANTINED_CASES),
        ):
            for tool, kwargs, namespace, granted in raw_cases:
                result = _invoke_tool(server, tool, identity, dict(kwargs))
                decision, denial = _classify(result)
                # Self-check: a quarantined ungranted/aggregate call MUST deny;
                # a trusted call must NEVER deny. A regression in the enforcement
                # can therefore never be committed as a green fixture.
                if tier == "trusted" and decision != "allow":
                    raise SystemExit(
                        f"REFUSING TO EMIT: trusted {tool} unexpectedly denied"
                    )
                if tier == "quarantined" and granted is False and decision != "deny":
                    raise SystemExit(
                        f"REFUSING TO EMIT: quarantined ungranted {tool} did not deny"
                    )
                cases.append(
                    {
                        "tool": tool,
                        "tier": tier,
                        "namespace": namespace,
                        "granted": granted,
                        "args": kwargs,
                        "decision": decision,
                        "denial": denial,
                    }
                )

        # Cross-cut the seed in -- restore the committed copy if the staged write
        # touched the temp tree (defensive; the temp dir is discarded anyway).
        shutil.rmtree(lib, ignore_errors=True)

    return {
        "_doc": "Cross-impl D4 trust-tier enforcement. Oracle = the live "
                "create_l6_server closures, driven over a temp copy of "
                "fed_seed_library with a quarantined `openclaw` granted ONLY "
                "`claude-code`. Each case pins the allow|deny decision the server "
                "actually returned and, on deny, the VERBATIM structured-denial "
                "dict. `granted` is True/False for namespace-scoped tools, null "
                "otherwise. `denial.detail` is deterministic prose (embeds the "
                "synthetic caller id / namespace); the load-bearing invariant is "
                "decision + the denial KEY SET. trusted=OPERATOR (stdio / legacy "
                "peer); list_recent_work denials also fold in an empty "
                "sessions/next_cursor/has_more page.",
        "denial_shape": {
            "keys": ["error", "op", "agent", "tier", "detail"],
            "error_value": "access denied",
            "note": "list_recent_work denials additionally carry "
                    "sessions=[], next_cursor=null, has_more=false.",
        },
        "seed_library": "fed_seed_library",
        "caller": {
            "trusted": {"agent_id": "operator", "tier": "trusted"},
            "quarantined": {
                "agent_id": "openclaw",
                "tier": "quarantined",
                "grants": ["claude-code"],
            },
        },
        "cases": cases,
    }


# ---------------------------------------------------------------------------
# federation_on_disk  (Python-WRITTEN registry + audit log the TS side parses)
# ---------------------------------------------------------------------------
# These are on-disk artifacts the TS mirror must parse byte/structure-identically:
#   on_disk/federation.yaml  -- the registry, written through the REAL
#                               FederationRegistry serialization (sha256-only
#                               rows, sorted keys) for SYNTHETIC bdn_ tokens with
#                               frozen timestamps. A trusted member, a quarantined
#                               member (granted one namespace), and a revoked one.
#   on_disk/audit.jsonl      -- produced by the REAL FederationAudit.record (so
#                               the JSONL record shape is the oracle's), then the
#                               runtime `ts` is frozen for an idempotent fixture.
#   on_disk/auth_vectors.json -- synthetic tokens (fragment arrays, joined at
#                               load) + the oracle-computed authenticate() result
#                               against federation.yaml (round-trip parity).
#
# Synthetic tokens are stored as fragment arrays (harness rule #3) so no
# contiguous bdn_ literal lands in a committed file. Only their sha256 reaches
# federation.yaml; the plaintext lives nowhere on disk.

# (name, agent_id, token_fragments, tier, grants, revoked)
_REGISTRY_MEMBERS: list[tuple[str, str, list[str], str, list[str], bool]] = [
    ("trusted_member", "claude-code", ["bdn_", "a" * 48], "trusted", [], False),
    ("quarantined_member", "openclaw", ["bdn_", "b" * 48], "quarantined", ["claude-code"], False),
    ("revoked_member", "retired", ["bdn_", "c" * 48], "trusted", [], True),
]

_FROZEN_CREATED = "2026-06-29T00:00:00Z"
_FROZEN_REVOKED = "2026-06-29T00:05:00Z"

# Audit records to emit -- (agent, op, namespace, decision, detail). Run through
# the REAL record() so the line shape is the oracle's; ts is frozen afterward.
_AUDIT_RECORDS: list[tuple[str, str, str, str, str | None]] = [
    ("openclaw", "find_entity", "claude-code", "allow", None),
    ("openclaw", "query_agent_memory", "codex", "deny", "namespace 'codex' not granted"),
    ("openclaw", "commit_to_federation", "openclaw", "allow", "staged"),
    ("operator", "list_agents", "*", "allow", None),
    ("openclaw", "get_cross_agent_summary", "*", "deny",
     "tier 'quarantined' may not call this tool"),
]


def federation_on_disk() -> dict:
    """Emit the on-disk registry + audit log + auth round-trip vectors."""
    import re as _re
    import tempfile

    from core import federation_registry as fr
    from core.federation_audit import FederationAudit

    base = CONFORMANCE / "on_disk"
    base.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    tokens = {name: "".join(frags) for name, _aid, frags, *_ in _REGISTRY_MEMBERS}

    # -- 1. federation.yaml through the real registry serialization -------------
    with tempfile.TemporaryDirectory() as td:
        reg_path = Path(td) / "federation.yaml"
        reg = fr.FederationRegistry(reg_path)
        rows: dict[str, dict] = {}
        for name, agent_id, _frags, tier, grants, revoked in _REGISTRY_MEMBERS:
            row: dict[str, Any] = {
                "tier": tier,
                "token_sha256": fr._hash_token(tokens[name]),
                "created_at": _FROZEN_CREATED,
                "revoked": revoked,
                "grants": list(grants),
            }
            if revoked:
                row["revoked_at"] = _FROZEN_REVOKED
            rows[agent_id] = row
        reg._agents = rows
        reg._save()  # the REAL serialization (sorted keys, version envelope)
        yaml_text = reg_path.read_text(encoding="utf-8")

        # Self-check + oracle-compute the auth vectors via the REAL authenticate.
        rt = fr.FederationRegistry(reg_path)
        vectors: list[dict] = []
        for name, _agent_id, frags, _tier, _grants, _revoked in _REGISTRY_MEMBERS:
            ident = rt.authenticate(tokens[name])
            vectors.append(
                {
                    "name": name,
                    "token_fragments": frags,
                    "expect": None
                    if ident is None
                    else {
                        "agent_id": ident.agent_id,
                        "tier": ident.tier,
                        "grants": list(ident.grants),
                    },
                }
            )
        # Negative vectors -- never authenticate.
        for neg_name, neg_frags in (
            ("unknown_token", ["bdn_", "f" * 48]),
            ("empty_token", [""]),
        ):
            assert rt.authenticate("".join(neg_frags)) is None
            vectors.append({"name": neg_name, "token_fragments": neg_frags, "expect": None})

        # Oracle invariants the TS parser must reproduce.
        if rt.authenticate(tokens["revoked_member"]) is not None:
            raise SystemExit("REFUSING TO EMIT: revoked member still authenticates")
        if rt.authenticate(tokens["trusted_member"]).tier != "trusted":
            raise SystemExit("REFUSING TO EMIT: trusted member did not resolve trusted")

    _write_text_lf(base / "federation.yaml", yaml_text)
    written.append("on_disk/federation.yaml")

    # No synthetic token plaintext may leak into the registry file.
    fed_text = (base / "federation.yaml").read_text(encoding="utf-8")
    for tok in tokens.values():
        if tok in fed_text:
            raise SystemExit("REFUSING TO EMIT: token plaintext leaked into federation.yaml")

    # -- 2. audit.jsonl through the real record(), then freeze ts ---------------
    with tempfile.TemporaryDirectory() as td:
        audit_path = Path(td) / "audit.jsonl"
        audit = FederationAudit(audit_path)
        for agent, op, namespace, decision, detail in _AUDIT_RECORDS:
            audit.record(agent, op, namespace, decision, detail)
        raw_lines = [
            ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]

    ts_re = _re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
    frozen_lines: list[str] = []
    for i, ln in enumerate(raw_lines):
        entry = json.loads(ln)
        if not ts_re.match(entry["ts"]):
            raise SystemExit(f"REFUSING TO EMIT: audit ts not in expected format: {entry['ts']!r}")
        # Freeze the runtime timestamp; key order (ts,agent,op,namespace,decision,
        # [detail]) is preserved from the real record() emission.
        entry["ts"] = f"2026-06-29T00:00:{i:02d}.000000Z"
        frozen_lines.append(json.dumps(entry, ensure_ascii=False))
    _write_text_lf(base / "audit.jsonl", "\n".join(frozen_lines) + "\n")
    written.append("on_disk/audit.jsonl")

    # -- 3. auth_vectors.json (round-trip parity for the registry parser) -------
    _write_json(base / "auth_vectors.json", {
        "_doc": "Synthetic bdn_ tokens (fragment arrays -- join at load, NEVER a "
                "real secret) + the oracle authenticate() result against "
                "on_disk/federation.yaml. Oracle: FederationRegistry.authenticate "
                "(sha256 of the joined token, constant-time compare; revoked rows "
                "and the empty token resolve to null). The TS parser must reproduce "
                "each `expect`.",
        "registry_file": "federation.yaml",
        "vectors": vectors,
    })
    written.append("on_disk/auth_vectors.json")

    return {"__multifile__": True, "files": written}


# ---------------------------------------------------------------------------
# mcp_snapshots  (the JSON-in-TextContent wire contract for all 10 L6 tools)
# ---------------------------------------------------------------------------
# Oracle = the REAL create_l6_server tool closures, driven IN-PROCESS over a temp
# copy of fed_seed_library (reused -- the snapshots and tier_matrix share one
# corpus). Each tool returns a plain dict that fastmcp serializes onto the wire as
# a single TextContent whose `.text` is `json.dumps(payload)`; RemoteL6Client does
# `json.loads(item.text)` to recover it. We snapshot that recovered payload, run
# through the NORMALIZER below so a second implementation can match value-for-value
# without tripping on key order, float jitter, to_dict() omission, opaque cursors,
# or runtime-stamped timestamps/latencies/paths.
#
# NORMALIZER (codified here, fixture-tested via _normalizer.json):
#   1. recursively SORT object keys (arrays keep their order -- sessions/matches/
#      agents order IS contract); emitted via sort_keys=True at write time.
#   2. ROUND floats to 4 dp (recognition confidence / brief scores).
#   3. DROP null and empty-list dict fields (matches the dataclass to_dict()
#      omission rule the TS port reproduces).
#   4. FREEZE non-deterministic fields (applied last, by field name):
#      last_updated / generated_at / generated_from / path -> "<frozen>".
#   5. DROP latency fields (runtime-dependent): recognition_latency_us /
#      peer_latencies_us -- asserted "present and numeric" by the live run, never
#      pinned by value.
#   6. base64 CURSORS compared by DECODED payload: next_cursor -> {"offset": N}
#      via the real _decode_cursor, so the opaque token's encoding can differ
#      across impls as long as the position matches.
#
# compile_codex_turn is snapshotted as its DEFERRED structured stub: the live turn
# compiler's brief is environment-bound (resolves the real cwd, git repo name +
# remote, and repo-identity scoring) and is therefore not a portable parity
# fixture until the TS turn compiler lands (P7). The req is still pinned so the
# tool surface + arg defaults stay covered.

_SNAPSHOT_DROP_LATENCY = {"recognition_latency_us", "peer_latencies_us"}
_SNAPSHOT_FREEZE_FIELDS = {"last_updated", "generated_at", "generated_from", "path"}
_SNAPSHOT_CURSOR_FIELDS = {"next_cursor"}
_SNAPSHOT_FROZEN = "<frozen>"


def normalize_snapshot(value: Any, field_name: str | None = None) -> Any:
    """Canonicalize one MCP payload for cross-impl comparison (see the policy above).

    Returns a JSON-ready structure with object keys sorted at write time (via
    sort_keys=True), floats rounded, null/empty-list fields dropped, the freeze
    fields stamped, latency fields removed, and base64 cursors decoded to
    ``{"offset": N}``. Applied identically on both sides before compare.
    """
    from core.l6_store import _decode_cursor

    # Field-name-keyed transforms first (freeze last per the policy, but freeze and
    # cursor are mutually exclusive field sets so the order between them is moot).
    if field_name in _SNAPSHOT_CURSOR_FIELDS and isinstance(value, str) and value:
        return {"offset": _decode_cursor(value)}
    if field_name in _SNAPSHOT_FREEZE_FIELDS and value is not None:
        return _SNAPSHOT_FROZEN

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, raw in value.items():
            if key in _SNAPSHOT_DROP_LATENCY:
                continue  # latency: runtime-dependent, dropped wholesale
            norm = normalize_snapshot(raw, key)
            if norm is None:
                continue  # drop null (to_dict omission)
            if isinstance(norm, list) and not norm:
                continue  # drop empty list (to_dict omission)
            out[key] = norm
        return out
    if isinstance(value, list):
        return [normalize_snapshot(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return round(value, 4)
    return value


# Each tuple drives one tool: (tool, kwargs). Registration order in create_l6_server.
# Args are fixed and chosen to exercise the contract: team access so team rows show,
# a since window that spans the seed (the default 14-day window would exclude the
# 2026-06 seed sessions), and limit=2 on list_recent_work so a real next_cursor is
# produced (cursor-decode parity). commit_to_federation is a WRITE -- driven over the
# temp seed copy so it never mutates the committed library.
_SNAPSHOT_CASES: list[tuple[str, dict]] = [
    ("query_agent_memory",
     {"agent": "claude-code", "topic": "Bourdon", "access_level": "team"}),
    ("list_recent_work",
     {"since": "2026-06-01", "access_level": "team", "limit": 2}),
    ("find_entity", {"name": "Bourdon", "access_level": "team"}),
    ("list_agents", {}),
    ("export_agents", {}),
    ("commit_to_federation",
     {"agent_id": "clyde", "agent_type": "other",
      "entities": [{"name": "SnapshotEntity", "summary": "mcp snapshot probe"}],
      "sessions": [{"date": "2026-06-09"}],
      "mode": "merge"}),
    ("get_cross_agent_summary", {"project": "Bourdon", "access_level": "team"}),
    ("prepare_recognition_context",
     {"prompt": "what about Bourdon", "access_level": "team"}),
    ("compile_codex_turn",
     {"prompt": "Bourdon recognition", "access_level": "team"}),
    ("get_deeper_context",
     {"prompt": "what about Bourdon", "access_level": "team"}),
]

# compile_codex_turn's portable stand-in (see note above). The TS mirror reproduces
# THIS, not the env-bound brief, until the P7 turn compiler is ported.
_COMPILE_CODEX_TURN_DEFERRED = {
    "_status": "deferred",
    "schema_version": "codex-turn-brief/v1",
    "reason": (
        "compile_codex_turn output is environment-bound (resolves the live cwd, "
        "git repo name + remote, and repo-identity scoring) and is not a portable "
        "cross-impl parity fixture until the TS turn compiler lands (P7). The req "
        "pins the tool surface + arg defaults; the res is this deferred stub."
    ),
}

# {raw, normalized} pairs that PIN the normalizer rules themselves (oracle-computed
# by normalize_snapshot below) so the TS port's canonicalizer is fixture-tested too.
_NORMALIZER_RAW_CASES: list[dict] = [
    {"_doc": "drops null + empty-list fields (to_dict omission parity)",
     "raw": {"agent": None, "files_touched": [], "kept": "x", "zero": 0}},
    {"_doc": "rounds floats to 4 dp; bools are untouched",
     "raw": {"score": 71.66666666, "ok": True, "ratio": 0.1234567}},
    {"_doc": "freezes last_updated / path / generated_from / generated_at",
     "raw": {"last_updated": "2026-06-30T02:59:09.096243+00:00",
             "path": "/tmp/x/clyde.l5.yaml",
             "generated_from": "/tmp/x/agents",
             "generated_at": "2026-06-30T02:59:09Z"}},
    {"_doc": "drops latency fields wholesale (runtime-dependent)",
     "raw": {"recognition_latency_us": 140.5, "peer_latencies_us": {"mac": 9.1},
             "confidence": "medium"}},
    {"_doc": "decodes a base64 next_cursor to its {offset} payload; null cursor drops",
     "raw": {"next_cursor": "eyJvZmZzZXQiOjJ9", "has_more": True}},
    {"_doc": "null next_cursor (last page) is dropped like any other null",
     "raw": {"next_cursor": None, "has_more": False}},
    {"_doc": "nested arrays keep their order; nested null/empty dropped per-field",
     "raw": {"sessions": [{"agent": "codex", "cwd": None, "project_focus": ["A"],
                           "files_touched": []},
                          {"agent": "claude-code", "project_focus": ["B"]}]}},
]


def mcp_snapshots() -> dict:
    """Snapshot each of the 10 L6 MCP tools' normalized req/res over the seed.

    Multi-file producer. Drives the live ``create_l6_server`` closures in-process
    (OPERATOR / trusted, matching stdio) over a TEMP copy of ``fed_seed_library``,
    normalizes every response, and writes ``mcp_snapshots/<tool>.{req,res}.json``
    plus a ``seed_library`` pointer and the normalizer self-test. Returns the
    marker listing every file so ``main`` stamps each in manifest.json.
    """
    import shutil
    import tempfile

    from core.l6_store import L6Store

    try:
        from core.l6_server import create_l6_server
    except ImportError as exc:  # pragma: no cover -- needs the [server] extra
        raise SkipFamilyError(
            "mcp_snapshots needs fastmcp (pip install 'bourdon[server]') to drive "
            f"the real L6 tool surface: {exc}"
        ) from exc

    snap_dir = CONFORMANCE / "mcp_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    # Deterministic machine label so export_agents.machine is stable across hosts.
    prior_local_name = os.environ.get("BOURDON_LOCAL_NAME")
    os.environ["BOURDON_LOCAL_NAME"] = "conformance-host"
    try:
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "lib"
            agents = lib / "agents"
            agents.mkdir(parents=True)
            for fname, manifest in _SEED_MANIFESTS.items():
                _write_text_lf(
                    agents / fname,
                    yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
                )
            store = L6Store(lib)
            server = create_l6_server(store)

            for tool, kwargs in _SNAPSHOT_CASES:
                req = {"tool": tool, "args": kwargs}
                if tool == "compile_codex_turn":
                    res = _COMPILE_CODEX_TURN_DEFERRED
                else:
                    raw = _invoke_tool(server, tool, None, dict(kwargs))
                    # Round-trip through the wire encoding (json.dumps in TextContent
                    # -> json.loads) exactly as RemoteL6Client recovers it, so the
                    # snapshot is the post-serialization payload, never a live object.
                    raw = json.loads(json.dumps(raw, ensure_ascii=False))
                    # Latency fields must be present + numeric before we drop them.
                    if tool == "prepare_recognition_context":
                        lat = raw.get("recognition_latency_us")
                        if not isinstance(lat, (int, float)):
                            raise SystemExit(
                                "REFUSING TO EMIT: prepare_recognition_context lost "
                                "its numeric recognition_latency_us latency field"
                            )
                    # commit_to_federation must have actually written (not errored).
                    if tool == "commit_to_federation" and raw.get("error"):
                        raise SystemExit(
                            f"REFUSING TO EMIT: commit_to_federation errored: {raw['error']!r}"
                        )
                    res = normalize_snapshot(raw)

                req_rel = f"mcp_snapshots/{tool}.req.json"
                res_rel = f"mcp_snapshots/{tool}.res.json"
                _write_snapshot_json(snap_dir / f"{tool}.req.json", req)
                _write_snapshot_json(snap_dir / f"{tool}.res.json", res)
                written.extend([req_rel, res_rel])

            shutil.rmtree(lib, ignore_errors=True)
    finally:
        if prior_local_name is None:
            os.environ.pop("BOURDON_LOCAL_NAME", None)
        else:
            os.environ["BOURDON_LOCAL_NAME"] = prior_local_name

    # seed_library pointer -- the snapshots reuse the committed fed_seed_library
    # corpus verbatim (one seed for both federation families).
    _write_snapshot_json(snap_dir / "seed_library.json", {
        "_doc": "The 10 mcp_snapshots/*.res.json were produced by driving the live "
                "create_l6_server tool closures (OPERATOR / trusted, = stdio) over a "
                "TEMP copy of this seed library, then normalizing each response. The "
                "TS mirror loads the SAME seed and must reproduce every normalized "
                "res. Each tool's payload travels the wire as a single TextContent "
                "whose .text = json.dumps(payload); the snapshot is the json.loads "
                "of that text.",
        "seed_library": "fed_seed_library",
        "transport": "in-process (create_l6_server tool closures); wire-equivalent "
                     "to stdio / streamable-HTTP JSON-in-TextContent",
        "caller": {"agent_id": "operator", "tier": "trusted"},
        "tools": [tool for tool, _ in _SNAPSHOT_CASES],
        "deferred": ["compile_codex_turn"],
    })
    written.append("mcp_snapshots/seed_library.json")

    # Normalizer self-test -- {raw, normalized} pairs, normalized BY the oracle.
    normalizer_cases = [
        {"_doc": case["_doc"], "raw": case["raw"],
         "normalized": normalize_snapshot(case["raw"])}
        for case in _NORMALIZER_RAW_CASES
    ]
    _write_snapshot_json(snap_dir / "_normalizer.json", {
        "_doc": "Fixture-tests the MCP snapshot NORMALIZER itself. Each `normalized` "
                "is normalize_snapshot(`raw`) computed by the oracle. Rules: sort "
                "object keys (write-time), round floats to 4 dp, drop null + "
                "empty-list dict fields, freeze {last_updated, generated_at, "
                "generated_from, path} -> '<frozen>', drop latency fields "
                "{recognition_latency_us, peer_latencies_us}, decode next_cursor "
                "base64 -> {offset:N}. The TS canonicalizer must match each pair.",
        "frozen_sentinel": _SNAPSHOT_FROZEN,
        "freeze_fields": sorted(_SNAPSHOT_FREEZE_FIELDS),
        "drop_latency_fields": sorted(_SNAPSHOT_DROP_LATENCY),
        "cursor_fields": sorted(_SNAPSHOT_CURSOR_FIELDS),
        "float_precision": 4,
        "cases": normalizer_cases,
    })
    written.append("mcp_snapshots/_normalizer.json")

    return {"__multifile__": True, "files": written}


# ---------------------------------------------------------------------------
# native_stores  (external-agent native store -> the REAL participant -> L5)
# ---------------------------------------------------------------------------
# Participants are external-agent readers: they walk a native store (SQLite /
# files / a network TTL cache) and normalize it into an L5 manifest. The PARITY
# contract is ONLY the export_l5() OUTPUT SHAPE (its to_dict()) -- the internal
# scraping is each reader's own business and is NOT pinned. So each case here
# seeds a tiny, hermetic native store, runs the REAL participant.export_l5()
# against it, and pins the resulting to_dict() as expected_l5.json. The TS mirror
# seeds the byte-identical store, runs ITS reader, and must reproduce the same
# normalized manifest.
#
# Three reader categories are covered (one each, the minimum slice):
#   hermes          -- SQLite reader   (state.db sessions + memories/*.md)
#   claude_code     -- file reader     (claude-brain + auto-memory + KG jsonl)
#   github_copilot  -- network reader  (seeded TTL cache, runs offline-from-cache)
#
# DETERMINISM. export_l5() stamps two runtime-bound fields that are NOT part of
# the parity contract: agent.last_updated (datetime.now) and, for claude_code,
# agent.instance (socket.gethostname). Both are FROZEN before emission -- to a
# VALID date-time / a fixed label, so the frozen manifest still validates against
# L5_schema.json. Every other field is a pure function of the seeded store.
#
# REDACTION. The readers redact native strings themselves (the SSOT). The seeds
# carry credential-shaped content using KEYWORD triggers only (".env", "bearer
# token", "service_role") -- never a token literal -- so the redaction is visible
# in the output (hermes memory entry / github PR title -> the REDACTED sentinel;
# claude_code's credential graph entity + person entity -> dropped as private)
# while no contiguous secret ever lands in a committed store fixture.

# Frozen runtime fields (valid date-time keeps the frozen manifest schema-valid).
_NATIVE_FROZEN_TS = "2026-06-29T00:00:00+00:00"
_NATIVE_FROZEN_INSTANCE = "conformance-host"
# Frozen cache fetched_at for the network reader. Value is irrelevant to freshness
# (the generator forces an infinite TTL) but MUST be constant for an idempotent
# committed cache fixture.
_NATIVE_FROZEN_FETCHED_AT = 1782604800.0


def _freeze_native_l5(d: dict) -> dict:
    """Freeze the two runtime-bound fields so the manifest is deterministic.

    last_updated -> a fixed valid date-time; agent.instance -> a fixed label
    (only present on readers that stamp the hostname). Everything else is a pure
    function of the seeded store and is left untouched.
    """
    frozen = json.loads(json.dumps(d))
    frozen["last_updated"] = _NATIVE_FROZEN_TS
    agent = frozen.get("agent")
    if isinstance(agent, dict) and "instance" in agent:
        agent["instance"] = _NATIVE_FROZEN_INSTANCE
    return frozen


def _seed_hermes_state_db(db_path: Path) -> None:
    """Seed a tiny ~/.hermes/state.db (2 active sessions + 1 archived, 1 with tools).

    No literal secrets: the credential surface is exercised by the memory files,
    not the session DB. Created deterministically (fixed schema, no AUTOINCREMENT,
    rollback journal closed on commit) so the committed bytes are reproducible.
    """
    import sqlite3

    # Idempotency: a stale file (or its sidecars) would break CREATE TABLE / drift
    # the bytes. Start from a clean slate every run.
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = db_path.with_name(db_path.name + suffix) if suffix else db_path
        p.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "CREATE TABLE sessions(id TEXT, source TEXT, model TEXT, title TEXT, "
            "cwd TEXT, started_at REAL, ended_at REAL, message_count INT, "
            "tool_call_count INT, archived INT)"
        )
        db.execute(
            "CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, "
            "content TEXT, tool_name TEXT)"
        )
        # Fixed epoch (UTC) -- no datetime.now anywhere in the seed.
        ts = 1781827200.0  # 2026-06-19T00:00:00Z (date-only is the L5 surface)
        db.execute(
            "INSERT INTO sessions VALUES('s1','tui','m','Refactor auth flow',"
            "'/projects/bourdon',?,?,10,4,0)",
            (ts, ts),
        )
        db.execute(
            "INSERT INTO sessions VALUES('s2','cli','m','Fix flaky tests',"
            "'/projects/bourdon',?,?,5,2,0)",
            (ts, ts),
        )
        # Archived row -- must be excluded from the manifest (COALESCE(archived,0)).
        db.execute(
            "INSERT INTO sessions VALUES('s3','slack','m','archived work',"
            "'/projects/secretwork',?,?,2,0,1)",
            (ts, ts),
        )
        for tn in ("terminal", "read_file", "patch"):
            db.execute("INSERT INTO messages VALUES(1,'s1','tool','x',?)", (tn,))
        db.commit()
    finally:
        db.close()


def _native_hermes(base: Path) -> tuple[dict, list[str]]:
    """Seed a ~/.hermes store, run the REAL HermesParticipant, return (frozen, files)."""
    from participants.hermes import HermesParticipant

    home = base / "store" / ".hermes"
    (home / "memories").mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    _seed_hermes_state_db(home / "state.db")
    written.append("native_stores/hermes/store/.hermes/state.db")

    # memory.md carries a credential-shaped line (keyword trigger, no literal) so
    # the redaction shows up in the emitted entity; user.md is benign preferences.
    _write_text_lf(
        home / "memories" / "memory.md",
        "- Project uses pytest with xdist\n"
        "- The bearer token is stored in .env\n",
    )
    written.append("native_stores/hermes/store/.hermes/memories/memory.md")
    _write_text_lf(
        home / "memories" / "user.md",
        "- Prefers concise answers\n- Works in the Pacific timezone\n",
    )
    written.append("native_stores/hermes/store/.hermes/memories/user.md")

    manifest = HermesParticipant(hermes_home=home).export_l5()
    return _freeze_native_l5(manifest.to_dict()), written


def _native_claude_code(base: Path) -> tuple[dict, list[str]]:
    """Seed claude-brain + auto-memory + KG, run the REAL ClaudeCodeParticipant."""
    from participants.claude_code import ClaudeCodeParticipant

    store = base / "store"
    brain = store / "brain"
    auto = store / "auto_memory"
    kg = store / "knowledge_graph"
    (brain / "PROJECTS" / "Bourdon").mkdir(parents=True, exist_ok=True)
    (brain / "PROJECTS" / "OldThing").mkdir(parents=True, exist_ok=True)
    (brain / "LOG").mkdir(parents=True, exist_ok=True)
    auto.mkdir(parents=True, exist_ok=True)
    kg.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def emit(path: Path, text: str, rel: str) -> None:
        _write_text_lf(path, text)
        written.append(rel)

    emit(
        brain / "PROJECTS" / "Bourdon" / "OVERVIEW.md",
        "# Bourdon -- Cross-agent memory federation\n\n"
        "Cross-agent memory federation substrate. L5 manifests + L6 library.\n",
        "native_stores/claude_code/store/brain/PROJECTS/Bourdon/OVERVIEW.md",
    )
    # Archived project -> tags=['archived'] + a recovered valid_to date.
    emit(
        brain / "PROJECTS" / "OldThing" / "OVERVIEW.md",
        "# OldThing\n\nA retired experiment.\n\n## Status: Archived (2026-01-15)\n",
        "native_stores/claude_code/store/brain/PROJECTS/OldThing/OVERVIEW.md",
    )
    emit(
        brain / "LOG" / "2026-06-28-pc.md",
        "# Session 2026-06-28\n\n"
        "Wired the native_stores conformance family and validated the fixtures.\n",
        "native_stores/claude_code/store/brain/LOG/2026-06-28-pc.md",
    )
    # MEMORY.md is the index file the parser skips.
    emit(
        auto / "MEMORY.md",
        "# Project Memory Index\n\n- see entity files\n",
        "native_stores/claude_code/store/auto_memory/MEMORY.md",
    )
    emit(
        auto / "clyde.md",
        "---\nname: Clyde\ntype: project\n"
        "description: Local AI assistant entity.\ntags: [infra]\n---\n# Clyde\n",
        "native_stores/claude_code/store/auto_memory/clyde.md",
    )
    # type: person -> PRIVATE by default -> MUST be filtered out (visibility guard).
    emit(
        auto / "ry-guy.md",
        "---\nname: Ry Guy\ntype: person\ndescription: The owner.\n---\n# Ry Guy\n",
        "native_stores/claude_code/store/auto_memory/ry-guy.md",
    )
    # KG: one public entity + one whose observation is a credential -> PRIVATE -> dropped.
    emit(
        kg / "memory.jsonl",
        '{"type":"entity","name":"OMNIvour","entityType":"project",'
        '"observations":["File conversion app."]}\n'
        '{"type":"entity","name":"SecretCreds","entityType":"concept",'
        '"observations":["the service_role key for supabase"]}\n'
        '{"type":"relation","from":"OMNIvour","to":"Bourdon","relationType":"uses"}\n',
        "native_stores/claude_code/store/knowledge_graph/memory.jsonl",
    )

    # Run the REAL reader. Path *resolution* (env/home probing) is not the parity
    # contract -- seed the three discovered sources directly so the run is hermetic.
    p = ClaudeCodeParticipant()
    p._brain_path = brain
    p._auto_memory_path = auto
    p._knowledge_graph_path = kg / "memory.jsonl"
    manifest = p.export_l5()
    return _freeze_native_l5(manifest.to_dict()), written


def _native_github_copilot(base: Path) -> tuple[dict, list[str]]:
    """Seed the network reader's TTL cache, run export_l5() offline-from-cache."""
    from participants.github_copilot import GitHubCopilotParticipant

    cache_root = base / "cache"
    slug_dir = cache_root / "github-copilot"
    slug_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    # The {fetched_at, payload} cache the base class reads. The payload is what the
    # GitHub fetch would have returned; one PR title carries a credential keyword
    # (no literal) so the reader's redact_text shows up in the emitted session.
    payload = {
        "fetched_user": "ryan",
        "fetched_at": _NATIVE_FROZEN_TS,
        "items": [
            {
                "title": "Add OAuth login flow",
                "number": 12,
                "updated_at": "2026-06-20T10:00:00Z",
                "repository_url": "https://api.github.com/repos/ryan/bourdon",
            },
            {
                "title": "Rotate the service_role key in CI",
                "number": 9,
                "updated_at": "2026-06-18T10:00:00Z",
                "repository_url": "https://api.github.com/repos/ryan/bourdon",
            },
        ],
    }
    _write_json(slug_dir / "payload.json", {
        "fetched_at": _NATIVE_FROZEN_FETCHED_AT,
        "payload": payload,
    })
    written.append("native_stores/github_copilot/cache/github-copilot/payload.json")

    # Infinite TTL -> the seeded cache is always "fresh", so export_l5 reads it
    # without a token and never touches the network. No auth provider on purpose.
    p = GitHubCopilotParticipant(auth_provider=lambda: None, cache_root=cache_root)
    p.cache_ttl_seconds = float("inf")
    manifest = p.export_l5()
    return _freeze_native_l5(manifest.to_dict()), written


_NATIVE_READERS: list[tuple[str, Any]] = [
    ("hermes", _native_hermes),
    ("claude_code", _native_claude_code),
    ("github_copilot", _native_github_copilot),
]


def native_stores() -> dict:
    """Seed each reader's native store, run the REAL participant, pin its L5 to_dict.

    Multi-file producer. For each reader it writes the seeded store fixtures plus
    an ``expected_l5.json`` (the frozen-then-validated to_dict of the live
    participant.export_l5()). The expected manifest is VALIDATED against the live
    L5 schema before it lands, so a participant that emits a non-conformant
    manifest can never be committed as a green fixture.
    """
    validator = _l5_validator()
    root = CONFORMANCE / "native_stores"
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    for name, seed_fn in _NATIVE_READERS:
        base = root / name
        base.mkdir(parents=True, exist_ok=True)
        expected, store_files = seed_fn(base)

        # The frozen manifest MUST still validate (frozen fields stay valid-shaped).
        if not validator.is_valid(expected):
            errs = sorted(e.message for e in validator.iter_errors(expected))
            raise SystemExit(
                f"REFUSING TO EMIT: native_stores/{name} expected_l5 does not "
                f"validate against L5_schema.json: {errs}"
            )

        out = base / "expected_l5.json"
        _write_json(out, expected)
        # Guard: no contiguous token-shaped secret may leak into the manifest.
        _assert_no_literal_secret(out)
        written.extend(store_files)
        written.append(f"native_stores/{name}/expected_l5.json")

    # Index doc so the family is self-describing alongside the others.
    _write_json(root / "README.json", {
        "_doc": "Participant parity fixtures: external-agent native store -> the "
                "REAL participant.export_l5() -> its to_dict(), pinned as "
                "expected_l5.json. ONLY the export_l5 OUTPUT SHAPE is the contract "
                "(internal scraping is not). agent.last_updated (+ claude_code's "
                "agent.instance) are FROZEN to fixed valid values; every other field "
                "is a pure function of the seeded store. Readers redact native "
                "strings themselves -- seeds use keyword-only credential triggers so "
                "no token literal lands in a store fixture. Each expected_l5 "
                "validates against L5_schema.json.",
        "frozen_fields": {
            "last_updated": _NATIVE_FROZEN_TS,
            "agent.instance": _NATIVE_FROZEN_INSTANCE,
        },
        "readers": {
            "hermes": {
                "category": "sqlite",
                "store": "store/.hermes/ (state.db sessions + memories/*.md)",
                "reads": "non-archived sessions + project-from-cwd entities + "
                         "memory.md/user.md facts; redacts memory lines; team default",
            },
            "claude_code": {
                "category": "file",
                "store": "store/{brain,auto_memory,knowledge_graph}/",
                "reads": "PROJECTS/*/OVERVIEW.md + LOG/*.md + auto-memory frontmatter "
                         "+ KG jsonl, deduped/sorted; person + credential entities "
                         "filtered as private",
            },
            "github_copilot": {
                "category": "network",
                "store": "cache/github-copilot/payload.json (TTL cache)",
                "reads": "offline-from-cache (infinite TTL, no token); PR items -> "
                         "sessions + repository entities; redacts PR titles",
            },
        },
    })
    written.append("native_stores/README.json")

    return {"__multifile__": True, "files": written}


# ---------------------------------------------------------------------------
# turn_compiler_vectors  (codex + cursor turn compilers, oracle-driven)
# ---------------------------------------------------------------------------
# Oracle = the REAL compile_codex_turn / compile_cursor_turn. Each case is a
# seeded (prompt, cwd) driven over a TEMP copy of the dedicated seed library
# below, with the wall clock FROZEN to _TURN_FROZEN_* so the recency bands
# (codex _recency_score / cursor last_touched lift) are deterministic. The codex
# brief's resolved top-level `cwd` field -- the ONLY environment-bound output --
# is normalized back to the logical input string; repo identity is name-only
# (the cwd is a synthetic non-git path, so repo.root / repo.remote are null and
# repo.name is just the basename). codex_home is an empty temp dir (no
# state_5.sqlite) so native_stage1 = "unknown" and no machine-local Codex thread
# leaks in. The brief is then a pure function of (seed, prompt, cwd, frozen clock).
#
# The PINNED case (codex_cwd_hit) exercises cwd-hit (25.0) + recency (15.0) +
# a NAME_SUBSTRING prompt tier + BriefItem.score round(.,1) in ONE item: a prior
# session whose cwd == the turn cwd, dated one day before the frozen clock, whose
# project_focus name ("Bourdon") is a substring of the prompt.
#
# Cross-compiler difference pinned by the recency-only cases: codex's recognition
# gate DROPS a recency-only non-vague candidate (-> observe / empty items), while
# cursor INCLUDES any entity with score > 0 (recency alone) yet still buckets the
# recognition_confidence as "none" when no name/alias tier matches the prompt.

_TURN_FROZEN_DATE_ISO = "2026-06-29"  # the frozen wall clock for recency bands

# A dedicated 2-agent seed. Bourdon is cross-agent (both agents know it -> the
# codex cross_agent component fires); the claude-code session's cwd == the pinned
# turn cwd (the 25.0 cwd-hit) and is dated one day before the frozen clock (the
# 15.0 freshest recency band). Roadmap is team-visibility (exercises the team
# access filter) and carries a mid-band last_touched for the cursor recency lift.
_TURN_SEED_MANIFESTS: dict[str, dict] = {
    "claude-code.l5.yaml": {
        "spec_version": "0.1",
        "agent": {"id": "claude-code", "type": "code-assistant"},
        "last_updated": "2026-06-29T12:00:00+00:00",
        "known_entities": [
            {
                "name": "Bourdon",
                "type": "project",
                "aliases": ["NeuroLayer", "Continuo"],
                "summary": "Cross-agent memory federation.",
                "last_touched": "2026-06-28",
                "visibility": "public",
            },
            {
                "name": "Roadmap",
                "type": "concept",
                "summary": "Phase 1.7 federation roadmap.",
                "tags": ["team"],
                "last_touched": "2026-06-10",
                "visibility": "team",
            },
        ],
        "recent_sessions": [
            {
                "date": "2026-06-28",
                "cwd": "/projects/bourdon",
                "project_focus": ["Bourdon"],
                "key_actions": ["wired the turn compiler"],
                "files_touched": ["core/codex_turn_compiler.py"],
                "visibility": "public",
            }
        ],
    },
    "codex.l5.yaml": {
        "spec_version": "0.1",
        "agent": {"id": "codex", "type": "code-assistant"},
        "last_updated": "2026-06-29T12:00:00+00:00",
        "known_entities": [
            {
                "name": "Bourdon",
                "type": "project",
                "summary": "Federation, codex view.",
                "last_touched": "2026-06-28",
                "visibility": "public",
            }
        ],
    },
}

# (name, prompt, cwd) -- cwd is a synthetic logical path (no real .git ancestor).
_CODEX_TURN_CASES: list[tuple[str, str, str]] = [
    ("codex_cwd_hit", "what's next on Bourdon", "/projects/bourdon"),
    ("codex_prompt_only", "tell me about the Roadmap", "/projects/unrelated"),
    ("codex_observe_no_match", "what is the weather today", "/projects/unrelated"),
]
_CURSOR_TURN_CASES: list[tuple[str, str, str]] = [
    ("cursor_cwd_hit", "what's next on Bourdon", "/projects/bourdon"),
    ("cursor_prompt_only", "tell me about the Roadmap", "/projects/unrelated"),
    ("cursor_recency_only", "what is the weather today", "/projects/unrelated"),
]


def turn_compiler_vectors() -> dict:
    """Drive the REAL codex + cursor turn compilers and pin their oracle output.

    Single-file producer. The wall clock is frozen (so recency is deterministic)
    and the codex brief's resolved top-level cwd is normalized to the logical
    input (the only env-bound field). Self-checks refuse to emit if the pinned
    invariants (cwd-hit + recency + tier on the top codex item, observe-on-no-
    match, cursor recency-only-with-none-confidence) ever regress.
    """
    import tempfile
    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone

    import core.codex_turn_compiler as cc
    import core.cursor_turn_compiler as cu
    from core.codex_turn_compiler import SCHEMA_VERSION as CODEX_SCHEMA
    from core.codex_turn_compiler import compile_codex_turn
    from core.cursor_turn_compiler import SCHEMA_VERSION as CURSOR_SCHEMA
    from core.cursor_turn_compiler import compile_cursor_turn

    frozen_dt = _datetime(2026, 6, 29, 12, 0, 0, tzinfo=_timezone.utc)
    frozen_date = _date(2026, 6, 29)

    class _FrozenTurnDateTime(_datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 -- match datetime.now signature
            return frozen_dt if tz is None else frozen_dt.astimezone(tz)

    class _FrozenTurnDate(_date):
        @classmethod
        def today(cls):
            return frozen_date

    def _freeze_codex_brief(brief, logical_cwd: str) -> dict:
        """to_dict with the resolved cwd swapped for the logical input string.

        The top-level cwd is the ONLY env-bound field (repo.root/remote are null
        for a non-git synthetic path); everything else is portable. The TS mirror
        passes the same logical cwd and applies the identical swap before compare.
        """
        d = brief.to_dict()
        d["cwd"] = logical_cwd
        return d

    codex_cases: list[dict] = []
    cursor_cases: list[dict] = []

    saved = (cc.datetime, cc.date, cu.date)
    cc.datetime = _FrozenTurnDateTime
    cc.date = _FrozenTurnDate
    cu.date = _FrozenTurnDate
    try:
        with tempfile.TemporaryDirectory() as td:
            lib = Path(td) / "lib"
            agents = lib / "agents"
            agents.mkdir(parents=True)
            for fname, manifest in _TURN_SEED_MANIFESTS.items():
                _write_text_lf(
                    agents / fname,
                    yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
                )
            codex_home = Path(td) / "codex_home"  # empty: no state_5.sqlite
            codex_home.mkdir()

            for name, prompt, cwd in _CODEX_TURN_CASES:
                brief = compile_codex_turn(
                    prompt,
                    cwd=cwd,
                    codex_home=codex_home,
                    library_path=lib,
                    access_level="team",
                )
                # Self-checks: the env-bound surfaces must be neutralized, so a
                # stray .git ancestor (or a clock leak) can never ship green.
                if brief.repo.root is not None:
                    raise SystemExit(
                        f"REFUSING TO EMIT: turn case {name!r} resolved a real git "
                        f"root ({brief.repo.root!r}); cwd is not hermetic"
                    )
                items = brief.items
                if name == "codex_cwd_hit":
                    if not items or items[0].kind != "session":
                        raise SystemExit(
                            "REFUSING TO EMIT: codex_cwd_hit top item is not the session"
                        )
                    reason = items[0].reason
                    for needle in ("prompt matched", "cwd matched", "recent work"):
                        if needle not in reason:
                            raise SystemExit(
                                f"REFUSING TO EMIT: codex_cwd_hit lost {needle!r} "
                                f"(reason={reason!r})"
                            )
                    if items[0].to_dict()["score"] != round(items[0].score, 1):
                        raise SystemExit(
                            "REFUSING TO EMIT: codex_cwd_hit score is not round(.,1)"
                        )
                    if brief.routing.get("confidence") != "medium":
                        raise SystemExit(
                            "REFUSING TO EMIT: codex_cwd_hit confidence != medium"
                        )
                if name == "codex_observe_no_match" and (
                    items or brief.routing.get("mode") != "observe"
                ):
                    raise SystemExit(
                        "REFUSING TO EMIT: codex_observe_no_match did not observe-empty"
                    )
                codex_cases.append(
                    {
                        "name": name,
                        "prompt": prompt,
                        "cwd": cwd,
                        "access_level": "team",
                        "recognition_confidence": brief.routing.get("confidence"),
                        "item_scores": [
                            {
                                "rank": it.rank,
                                "name": it.name,
                                "kind": it.kind,
                                "source": it.source,
                                "score": round(it.score, 1),
                            }
                            for it in items
                        ],
                        "brief": _freeze_codex_brief(brief, cwd),
                    }
                )

            for name, prompt, cwd in _CURSOR_TURN_CASES:
                brief = compile_cursor_turn(
                    prompt, cwd=cwd, library_path=lib, access_level="team"
                )
                conf = brief.routing.get("confidence")
                if name == "cursor_cwd_hit" and (
                    not brief.matched_entities or conf != "medium"
                ):
                    raise SystemExit(
                        "REFUSING TO EMIT: cursor_cwd_hit lost its match/medium bucket"
                    )
                if name == "cursor_recency_only" and (
                    not brief.matched_entities or conf != "none"
                ):
                    raise SystemExit(
                        "REFUSING TO EMIT: cursor_recency_only expected matches with "
                        "a 'none' tier confidence"
                    )
                cursor_cases.append(
                    {
                        "name": name,
                        "prompt": prompt,
                        "cwd": cwd,
                        "access_level": "team",
                        "recognition_confidence": conf,
                        "cwd_project": brief.cwd_project,
                        "prompt_tokens": brief.prompt_tokens,
                        "matched_entities": brief.matched_entities,
                        "routing": brief.routing,
                    }
                )
    finally:
        cc.datetime, cc.date, cu.date = saved

    return {
        "_doc": "Cross-impl turn-compiler parity. Oracle = the REAL "
                "compile_codex_turn / compile_cursor_turn, driven over a temp copy "
                "of `seed_library` with the wall clock FROZEN to frozen_clock so "
                "recency is deterministic. codex `brief` is TurnBrief.to_dict() with "
                "the resolved top-level cwd swapped back to the logical input string "
                "(the only env-bound field; repo.root/remote are null for the "
                "synthetic non-git cwd) -- the TS mirror passes the same cwd and "
                "applies the identical swap. BriefItem.score is round(.,1); "
                "recognition_confidence is the shared tier-only bucket. The pinned "
                "codex_cwd_hit item folds cwd-hit (25) + recency (15) + a "
                "NAME_SUBSTRING prompt tier into one session item. cursor briefs drop "
                "the runtime compile_latency_us; matched_entities[].score is "
                "round(.,2). codex DROPS recency-only non-vague candidates (observe), "
                "cursor INCLUDES score>0 recency matches but still buckets confidence "
                "'none' without a name/alias tier hit.",
        "frozen_clock": _TURN_FROZEN_DATE_ISO,
        "codex_schema_version": CODEX_SCHEMA,
        "cursor_schema_version": CURSOR_SCHEMA,
        "seed_library": _TURN_SEED_MANIFESTS,
        "codex_cases": codex_cases,
        "cursor_cases": cursor_cases,
    }


# The active families (stubs excluded until wired). Single-file producers
# return a payload dict; multi-file producers (l5) write their own tree and
# return {"__multifile__": True, "files": [...]}.
FAMILIES = {
    "redaction_battery.json": ("redaction_battery", redaction_battery),
    "leak_cases.json": ("leak_cases", leak_cases),
    "recognition_vectors": ("recognition_vectors", recognition_vectors),
    "l5_schema_and_manifests": ("l5_schema_and_manifests", l5_schema_and_manifests),
    # L6 federation families (the cross-machine trust boundary).
    "fed_seed_library": ("fed_seed_library", fed_seed_library),
    "tier_matrix.json": ("tier_matrix", tier_matrix),
    "federation_on_disk": ("federation_on_disk", federation_on_disk),
    "mcp_snapshots": ("mcp_snapshots", mcp_snapshots),
    # Participant readers: native store -> the real export_l5() -> its to_dict().
    "native_stores": ("native_stores", native_stores),
    # Turn compilers: the real compile_codex_turn / compile_cursor_turn (P7).
    "turn_compiler_vectors.json": ("turn_compiler_vectors", turn_compiler_vectors),
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


def _write_snapshot_json(path: Path, payload: Any) -> None:
    """Write an MCP snapshot with SORTED object keys (the normalizer's rule #1).

    Same LF-deterministic bytes as _write_json, but sort_keys=True so the object
    key order is canonical and a second implementation compares structure, not
    insertion order. Arrays keep their order (that order is contract).
    """
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
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


class SkipFamilyError(Exception):
    """Raised by a producer that cannot run in the current environment (e.g. a
    fastmcp-dependent family in the lint lane that installs only ``[dev]``). The
    family's already-committed fixtures + manifest entries are PRESERVED verbatim,
    so the drift gate stays clean wherever the optional deps are absent, while a
    full regen still runs where they are present (the ``[server]`` extra)."""


def _existing_manifest_entries_by_producer() -> dict[str, list[dict]]:
    """Load the committed manifest, grouped by producer function name, so a
    skipped family's entries can be preserved in place."""
    path = CONFORMANCE / "manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    by_producer: dict[str, list[dict]] = {}
    for entry in data.get("fixtures", []):
        producer = str(entry.get("producer", ""))
        name = producer.rsplit("::", 1)[-1] if "::" in producer else producer
        by_producer.setdefault(name, []).append(entry)
    return by_producer


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate conformance parity fixtures.")
    parser.add_argument("--check", action="store_true",
                        help="(reserved) CI mode -- regenerate and let the caller git-diff.")
    parser.parse_args()

    CONFORMANCE.mkdir(exist_ok=True)
    preserved = _existing_manifest_entries_by_producer()
    fixtures_meta = []
    for filename, (producer_name, fn) in FAMILIES.items():
        try:
            result = fn()
        except SkipFamilyError as skip:
            # Optional dep missing: keep this family's committed fixtures + entries
            # untouched so the drift gate stays clean.
            kept = preserved.get(producer_name, [])
            fixtures_meta.extend(kept)
            print(f"  SKIP {producer_name}: {skip} (preserved {len(kept)} committed fixture(s))")
            continue
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
