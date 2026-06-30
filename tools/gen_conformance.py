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

CONFORMANCE_VERSION = "1.4.0"  # bump on any fixture change (see manifest.json doc)
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
        raise SystemExit(
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
    "leak_cases.json": ("leak_cases", leak_cases),
    "recognition_vectors": ("recognition_vectors", recognition_vectors),
    "l5_schema_and_manifests": ("l5_schema_and_manifests", l5_schema_and_manifests),
    # L6 federation families (the cross-machine trust boundary).
    "fed_seed_library": ("fed_seed_library", fed_seed_library),
    "tier_matrix.json": ("tier_matrix", tier_matrix),
    "federation_on_disk": ("federation_on_disk", federation_on_disk),
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
