"""Tests for core.leak_audit -- the federation leak auditor."""

from __future__ import annotations

from pathlib import Path

from core.leak_audit import (
    AUDIT_SCHEMA_VERSION,
    PRIVATE_TAG_FAMILIES,
    LeakKind,
    _resolves_private,
    audit_library,
    audit_manifest,
)

_FIXTURE_LIBRARY = Path(__file__).resolve().parent / "fixtures" / "leak_audit_library"


# ---- visibility resolution --------------------------------------------------


def test_resolves_private_explicit():
    assert _resolves_private({"name": "x", "visibility": "private"})
    assert not _resolves_private({"name": "x", "visibility": "team"})
    assert not _resolves_private({"name": "x"})


def test_resolves_private_by_tag():
    assert _resolves_private({"name": "x", "tags": ["financial"]})
    assert _resolves_private({"name": "x", "tags": ["personal", "other"]})
    assert not _resolves_private({"name": "x", "tags": ["project", "active"]})


def test_resolves_private_handles_non_dict():
    assert not _resolves_private("nope")  # type: ignore[arg-type]


# ---- credential scanning ----------------------------------------------------


def test_audit_manifest_flags_credential_in_summary():
    manifest = {
        "known_entities": [
            {"name": "API", "summary": "token sk-ant-abcdefghijklmnopqrstuvwx", "visibility": "team"},
        ]
    }
    findings = audit_manifest(manifest, "f.yaml")
    cred = [f for f in findings if f.kind == LeakKind.CREDENTIAL]
    assert len(cred) == 1
    assert cred[0].location == "known_entities[0].summary"


def test_audit_manifest_flags_credential_in_session_key_actions():
    manifest = {
        "recent_sessions": [
            {"date": "2026-06-20", "key_actions": ["deployed with AKIA1234567890ABCDEF"]},
        ]
    }
    findings = audit_manifest(manifest, "f.yaml")
    cred = [f for f in findings if f.kind == LeakKind.CREDENTIAL]
    assert len(cred) == 1
    assert "key_actions[0]" in cred[0].location


def test_audit_manifest_clean_manifest_has_no_findings():
    manifest = {
        "known_entities": [
            {"name": "Bourdon", "type": "project", "summary": "memory federation", "visibility": "team"},
        ],
        "recent_sessions": [
            {"date": "2026-06-20", "key_actions": ["refactored the L6 store"], "visibility": "team"},
        ],
    }
    assert audit_manifest(manifest, "f.yaml") == []


# ---- visibility leaks -------------------------------------------------------


def test_audit_manifest_flags_private_entity():
    manifest = {
        "known_entities": [
            {"name": "SecretProj", "type": "project", "visibility": "private"},
        ]
    }
    findings = audit_manifest(manifest, "f.yaml")
    vis = [f for f in findings if f.kind == LeakKind.VISIBILITY]
    assert len(vis) == 1
    assert "SecretProj" in vis[0].detail
    assert "entity" in vis[0].detail


def test_audit_manifest_flags_private_tagged_session():
    manifest = {
        "recent_sessions": [
            {"date": "2026-06-20", "tags": ["personal"]},
        ]
    }
    findings = audit_manifest(manifest, "f.yaml")
    vis = [f for f in findings if f.kind == LeakKind.VISIBILITY]
    assert len(vis) == 1
    assert "session" in vis[0].detail


def test_audit_manifest_never_raises_on_garbage():
    assert audit_manifest("not a dict", "f.yaml") == []  # type: ignore[arg-type]
    assert audit_manifest({"known_entities": "nope"}, "f.yaml") == []
    assert audit_manifest({"known_entities": [None, 42]}, "f.yaml") == []


# ---- library walk -----------------------------------------------------------


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_audit_library_mixed(tmp_path: Path):
    agents = tmp_path / "agents"
    _write(
        agents / "clean.l5.yaml",
        "spec_version: '0.1'\n"
        "agent: {id: clean, type: code-assistant}\n"
        "last_updated: '2026-06-26T00:00:00Z'\n"
        "known_entities:\n"
        "  - {name: Bourdon, type: project, summary: memory, visibility: team}\n",
    )
    _write(
        agents / "leaky.l5.yaml",
        "spec_version: '0.1'\n"
        "agent: {id: leaky, type: code-assistant}\n"
        "last_updated: '2026-06-26T00:00:00Z'\n"
        "known_entities:\n"
        "  - {name: P, summary: 'sk-ant-abcdefghijklmnopqrstuvwxyz', visibility: team}\n"
        "  - {name: S, type: project, visibility: private}\n",
    )
    report = audit_library(tmp_path)
    assert report.schema_version == AUDIT_SCHEMA_VERSION
    assert report.files_scanned == 2
    assert not report.clean
    assert len(report.by_kind(LeakKind.CREDENTIAL)) == 1
    assert len(report.by_kind(LeakKind.VISIBILITY)) == 1
    # findings name the offending file
    files = {Path(f.agent_file).name for f in report.findings}
    assert files == {"leaky.l5.yaml"}


def test_audit_library_clean_is_clean(tmp_path: Path):
    agents = tmp_path / "agents"
    _write(
        agents / "ok.l5.yaml",
        "spec_version: '0.1'\n"
        "agent: {id: ok, type: code-assistant}\n"
        "last_updated: '2026-06-26T00:00:00Z'\n"
        "known_entities:\n"
        "  - {name: Bourdon, type: project, visibility: team}\n",
    )
    report = audit_library(tmp_path)
    assert report.clean
    assert report.files_scanned == 1


def test_audit_library_unparseable_is_reported(tmp_path: Path):
    agents = tmp_path / "agents"
    _write(agents / "broken.l5.yaml", "this: is: not: valid: yaml: : :\n[unclosed")
    report = audit_library(tmp_path)
    assert not report.clean
    assert any("unparseable" in f.detail for f in report.findings)


def test_audit_library_missing_dir_is_empty(tmp_path: Path):
    report = audit_library(tmp_path / "does-not-exist")
    assert report.clean
    assert report.files_scanned == 0


# ---- regression: fields the old allowlist silently skipped ------------------


def test_audit_manifest_flags_credential_in_agent_role_narrative():
    """agent.role_narrative is federated free-text prose (and a redaction target
    in core.agents_export) -- the old allowlist never scanned it."""
    manifest = {
        "agent": {
            "id": "x",
            "type": "code-assistant",
            "role_narrative": "deploys with token sk-ant-abcdefghijklmnopqrstuvwxyz",
        }
    }
    findings = audit_manifest(manifest, "f.yaml")
    cred = [f for f in findings if f.kind == LeakKind.CREDENTIAL]
    assert len(cred) == 1
    assert cred[0].location == "agent.role_narrative"


def test_audit_manifest_flags_credential_in_session_cwd():
    manifest = {
        "recent_sessions": [
            {"date": "2026-06-20", "cwd": "/work/AKIA1234567890ABCDEF/repo"},
        ]
    }
    cred = [f for f in audit_manifest(manifest, "f.yaml") if f.kind == LeakKind.CREDENTIAL]
    assert len(cred) == 1
    assert cred[0].location == "recent_sessions[0].cwd"


def test_audit_manifest_flags_credential_in_files_touched():
    """A .env path in files_touched is exactly the leak class contains_secret
    catches via its keyword pattern."""
    manifest = {
        "recent_sessions": [
            {"date": "2026-06-20", "files_touched": ["src/app.ts", "config/.env"]},
        ]
    }
    cred = [f for f in audit_manifest(manifest, "f.yaml") if f.kind == LeakKind.CREDENTIAL]
    assert len(cred) == 1
    assert cred[0].location == "recent_sessions[0].files_touched[1]"


def test_audit_manifest_scans_top_level_capabilities():
    """The credential walk is total: even a top-level scalar list is covered."""
    manifest = {"capabilities": ["state_db", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"]}
    cred = [f for f in audit_manifest(manifest, "f.yaml") if f.kind == LeakKind.CREDENTIAL]
    assert len(cred) == 1
    assert cred[0].location == "capabilities[1]"


# ---- regression: visibility honors the manifest's own declared private tags --


def test_audit_manifest_honors_manifest_declared_private_tag():
    """A participant may declare a private tag the auditor doesn't hardcode. The
    backstop unions the manifest's own visibility_policy.private_tags, so such an
    entity is still flagged instead of sailing past."""
    manifest = {
        "known_entities": [
            {"name": "Vault", "type": "project", "tags": ["client-confidential"]},
        ],
        "visibility_policy": {"default": "team", "private_tags": ["client-confidential"]},
    }
    vis = [f for f in audit_manifest(manifest, "f.yaml") if f.kind == LeakKind.VISIBILITY]
    assert len(vis) == 1
    assert "Vault" in vis[0].detail


def test_audit_manifest_does_not_self_flag_visibility_policy():
    """visibility_policy enumerates tag-family NAMES (declarations, not content),
    so the credential walk must skip it -- otherwise every manifest self-flags."""
    manifest = {
        "known_entities": [{"name": "Bourdon", "type": "project", "visibility": "team"}],
        "visibility_policy": {
            "default": "team",
            "private_tags": ["credential", "secret", "password", "private_key"],
        },
    }
    assert audit_manifest(manifest, "f.yaml") == []


def test_private_tag_families_cover_known_participant_policies():
    """Defense in depth: PRIVATE_TAG_FAMILIES should still cover every shipped
    participant's declared private tags. (The auditor also unions the manifest's
    own private_tags at scan time, so a drift here is no longer a silent leak --
    but keeping the hardcoded set tight avoids surprises for manifests that omit
    a policy block.)"""
    import importlib
    import pkgutil

    import participants as participants_pkg

    union: set[str] = set()
    for mod in pkgutil.iter_modules(participants_pkg.__path__):
        module = importlib.import_module(f"participants.{mod.name}")
        policy = getattr(module, "DEFAULT_POLICY", None)
        tags = getattr(policy, "private_tags", None)
        if tags:
            union.update(str(t).lower() for t in tags)
    assert union, "expected at least one participant DEFAULT_POLICY.private_tags"
    assert union <= PRIVATE_TAG_FAMILIES, f"uncovered tags: {union - PRIVATE_TAG_FAMILIES}"


# ---- CI gate wiring ---------------------------------------------------------


def test_fixture_library_audits_clean_and_nonempty():
    """The committed CI fixture library must scan >0 files and stay clean."""
    report = audit_library(_FIXTURE_LIBRARY)
    assert report.files_scanned >= 1
    assert report.clean, [f.to_dict() for f in report.findings]


def test_audit_leaks_handler_require_files_guards_empty(tmp_path: Path):
    """--require-files turns a zero-file scan into a hard failure, so the CI gate
    can't silently pass by pointing at an empty library."""
    import argparse

    from cli.main import _handle_audit_leaks

    empty = argparse.Namespace(
        library=str(tmp_path / "empty-lib"),
        strict=True,
        summary=True,
        require_files=True,
        report_out=None,
    )
    assert _handle_audit_leaks(empty) == 1

    ok = argparse.Namespace(
        library=str(_FIXTURE_LIBRARY),
        strict=True,
        summary=True,
        require_files=True,
        report_out=None,
    )
    assert _handle_audit_leaks(ok) == 0
