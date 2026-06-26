"""Tests for core.leak_audit -- the federation leak auditor."""

from __future__ import annotations

from pathlib import Path

from core.leak_audit import (
    AUDIT_SCHEMA_VERSION,
    LeakKind,
    _resolves_private,
    audit_library,
    audit_manifest,
)


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
