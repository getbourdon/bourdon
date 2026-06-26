"""Federation leak auditor -- static scan of published L5 manifests for leaks.

Visibility is enforced *inside each participant*, before emission (see
PARTICIPANT_CONTRACT.md: "L6 trusts the manifest it receives; there is no second
filter layer. If a participant emits a private-tagged entity, it leaks. Test
this."). That makes every adapter a potential leak site, and "test this" is a
per-adapter aspiration, not a library-wide guarantee.

This module is the library-wide guarantee: a static auditor that walks every
``*.l5.yaml`` already on disk and flags two leak classes:

  1. CREDENTIAL leaks -- a field value that matches the canonical
     ``core.redaction`` credential patterns. If redaction was correctly applied
     at emit time these never appear; finding one means a participant emitted
     raw text that should have been scrubbed.
  2. VISIBILITY leaks -- an entity or session whose resolved visibility is
     ``private`` but which is sitting in a federated manifest anyway. The
     participant's pre-emission filter should have dropped it.

It is read-only and side-effect-free: it reports, it never edits a manifest. Use
it as a pre-commit hook, a CI gate (``--strict`` -> non-zero exit on any
finding), or an ad-hoc ``bourdon audit leaks`` check.

Design parity with the rest of the codebase: credential detection reuses
``core.redaction.contains_secret`` (the single source of truth), and visibility
resolution reuses the same tag rules participants apply, so the auditor can
never drift from what it is auditing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from core.redaction import contains_secret

logger = logging.getLogger(__name__)

AUDIT_SCHEMA_VERSION = "federation-leak-audit/v1"

# Tags that force an entity/session to PRIVATE regardless of declared
# visibility. Mirrors the private-tag families participants apply (see e.g.
# participants.hermes.DEFAULT_POLICY / participants.codex.DEFAULT_POLICY) so the
# auditor's notion of "should have been private" matches the emitters'.
PRIVATE_TAG_FAMILIES = frozenset(
    {
        "personal",
        "financial",
        "credential",
        "secret",
        "health",
        "family",
        "legal",
        "private",
    }
)

# Manifest fields whose free-text values we scan for credential shapes.
_SCANNED_TEXT_KEYS = ("name", "summary", "key_actions", "project_focus", "aliases")


class LeakKind(str, Enum):
    CREDENTIAL = "credential"
    VISIBILITY = "visibility"


@dataclass
class Finding:
    """A single leak finding."""

    kind: LeakKind
    agent_file: str
    location: str  # e.g. "known_entities[3].summary"
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "agent_file": self.agent_file,
            "location": self.location,
            "detail": self.detail,
        }


@dataclass
class AuditReport:
    schema_version: str
    files_scanned: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def by_kind(self, kind: LeakKind) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "files_scanned": self.files_scanned,
            "n_findings": len(self.findings),
            "n_credential": len(self.by_kind(LeakKind.CREDENTIAL)),
            "n_visibility": len(self.by_kind(LeakKind.VISIBILITY)),
            "findings": [f.to_dict() for f in self.findings],
        }


# -- Visibility resolution (mirrors participant tag rules) ---------------------


def _resolves_private(thing: dict[str, Any]) -> bool:
    """True if a tag forces this entity/session to private, OR it self-declares
    private. This is the condition under which it must NOT be in a federated
    manifest."""
    if not isinstance(thing, dict):
        return False
    tags = thing.get("tags") or []
    if isinstance(tags, list) and PRIVATE_TAG_FAMILIES.intersection(
        str(t).lower() for t in tags
    ):
        return True
    return str(thing.get("visibility") or "").lower() == "private"


# -- Credential scanning -------------------------------------------------------


def _iter_text_values(thing: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """Yield (subkey, text) for each scannable free-text field of an entity/session."""
    for key in _SCANNED_TEXT_KEYS:
        val = thing.get(key)
        if isinstance(val, str):
            yield key, val
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, str):
                    yield f"{key}[{i}]", item


def _scan_collection(
    agent_file: str,
    collection: Any,
    collection_name: str,
    findings: list[Finding],
) -> None:
    """Scan a known_entities / recent_sessions list for both leak classes."""
    if not isinstance(collection, list):
        return
    for idx, thing in enumerate(collection):
        if not isinstance(thing, dict):
            continue
        base = f"{collection_name}[{idx}]"
        # Visibility leak: private thing sitting in a federated manifest.
        if _resolves_private(thing):
            ident = thing.get("name") or thing.get("date") or "?"
            singular = "entity" if collection_name == "known_entities" else "session"
            findings.append(
                Finding(
                    kind=LeakKind.VISIBILITY,
                    agent_file=agent_file,
                    location=base,
                    detail=(
                        f"{singular} {ident!r} resolves to PRIVATE "
                        "but is present in a federated manifest"
                    ),
                )
            )
        # Credential leak: any text field that matches a secret shape.
        for subkey, text in _iter_text_values(thing):
            if contains_secret(text):
                findings.append(
                    Finding(
                        kind=LeakKind.CREDENTIAL,
                        agent_file=agent_file,
                        location=f"{base}.{subkey}",
                        detail="value matches a credential pattern (should have been redacted)",
                    )
                )


def audit_manifest(manifest: dict[str, Any], agent_file: str) -> list[Finding]:
    """Scan a single parsed L5 manifest. Never raises."""
    findings: list[Finding] = []
    if not isinstance(manifest, dict):
        return findings
    _scan_collection(agent_file, manifest.get("known_entities"), "known_entities", findings)
    _scan_collection(agent_file, manifest.get("recent_sessions"), "recent_sessions", findings)
    return findings


def audit_library(
    library_path: Path, *, agents_subdir: str = "agents"
) -> AuditReport:
    """Walk every ``*.l5.yaml`` under ``library_path/agents`` and audit each.

    A file that fails to parse is itself reported as a credential-kind finding
    with detail ``unparseable`` rather than silently skipped -- a manifest L6
    can't read is its own integrity problem.
    """
    agents_dir = library_path / agents_subdir
    findings: list[Finding] = []
    files = sorted(agents_dir.glob("*.l5.yaml")) if agents_dir.is_dir() else []
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            findings.append(
                Finding(
                    kind=LeakKind.CREDENTIAL,
                    agent_file=str(path),
                    location="<file>",
                    detail=f"unparseable manifest: {exc}",
                )
            )
            continue
        findings.extend(audit_manifest(data, str(path)))
    return AuditReport(
        schema_version=AUDIT_SCHEMA_VERSION,
        files_scanned=len(files),
        findings=findings,
    )
