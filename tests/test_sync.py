"""Tests for core.sync -- the bourdon sync push/pull rsync wrapper (#74)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from core.sync import (
    ACCESS_LEVELS,
    DEFAULT_PUSH_ACCESS_LEVEL,
    RsyncMissingError,
    SyncError,
    filter_l5_manifest,
    stage_filtered_library,
    sync_pull,
    sync_push,
    visible_counts,
)


_SAMPLE_MANIFEST = {
    "spec_version": "0.1",
    "agent": {
        "id": "codex",
        "type": "code-assistant",
        "instance": "test-host",
        "spec_version_compat": ">=0.1",
        "role_narrative": "Test narrative.",
    },
    "last_updated": "2026-05-26T12:00:00+00:00",
    "capabilities": ["sessions_dir"],
    "recent_sessions": [
        {
            "date": "2026-05-26",
            "cwd": "/projects/bourdon",
            "project_focus": ["bourdon"],
            "key_actions": ["wrote tests"],
            "visibility": "public",
        },
        {
            "date": "2026-05-25",
            "cwd": "/projects/secret",
            "project_focus": ["secret"],
            "key_actions": ["confidential"],
            "visibility": "team",
        },
        {
            "date": "2026-05-24",
            "cwd": "/projects/banking",
            "key_actions": ["budget edits"],
            "visibility": "private",
        },
    ],
    "known_entities": [
        {
            "name": "Bourdon",
            "type": "project",
            "summary": "Cross-agent memory federation.",
            "visibility": "public",
        },
        {
            "name": "TeamProject",
            "type": "project",
            "summary": "Internal collaboration.",
            "visibility": "team",
        },
        {
            "name": "MyBankAccount",
            "type": "financial",
            "summary": "Personal banking.",
            "visibility": "private",
        },
    ],
}


# ---------------------------------------------------------------------------
# filter_l5_manifest
# ---------------------------------------------------------------------------


def test_filter_public_keeps_only_public_entries():
    out = filter_l5_manifest(_SAMPLE_MANIFEST, "public")
    assert [e["name"] for e in out["known_entities"]] == ["Bourdon"]
    assert [s["cwd"] for s in out["recent_sessions"]] == ["/projects/bourdon"]


def test_filter_team_keeps_public_and_team():
    out = filter_l5_manifest(_SAMPLE_MANIFEST, "team")
    assert {e["name"] for e in out["known_entities"]} == {"Bourdon", "TeamProject"}
    assert {s["cwd"] for s in out["recent_sessions"]} == {
        "/projects/bourdon",
        "/projects/secret",
    }


def test_filter_private_keeps_all():
    out = filter_l5_manifest(_SAMPLE_MANIFEST, "private")
    assert len(out["known_entities"]) == 3
    assert len(out["recent_sessions"]) == 3


def test_filter_passes_through_other_keys():
    out = filter_l5_manifest(_SAMPLE_MANIFEST, "public")
    assert out["spec_version"] == "0.1"
    assert out["agent"]["id"] == "codex"
    assert out["capabilities"] == ["sessions_dir"]


def test_filter_does_not_mutate_input():
    before_entities = len(_SAMPLE_MANIFEST["known_entities"])
    before_sessions = len(_SAMPLE_MANIFEST["recent_sessions"])
    filter_l5_manifest(_SAMPLE_MANIFEST, "public")
    assert len(_SAMPLE_MANIFEST["known_entities"]) == before_entities
    assert len(_SAMPLE_MANIFEST["recent_sessions"]) == before_sessions


def test_filter_rejects_bad_access_level():
    with pytest.raises(SyncError, match="access_level"):
        filter_l5_manifest(_SAMPLE_MANIFEST, "secret")


def test_filter_handles_missing_entity_visibility_as_public():
    manifest = {
        "known_entities": [
            {"name": "NoVisibility", "type": "project"},  # default: public
            {"name": "Private", "type": "project", "visibility": "private"},
        ],
        "recent_sessions": [],
    }
    out = filter_l5_manifest(manifest, "public")
    assert [e["name"] for e in out["known_entities"]] == ["NoVisibility"]


def test_filter_skips_non_dict_entries():
    manifest = {
        "known_entities": [
            {"name": "Good", "visibility": "public"},
            "not a dict",
            None,
        ],
        "recent_sessions": [],
    }
    out = filter_l5_manifest(manifest, "public")
    assert [e["name"] for e in out["known_entities"]] == ["Good"]


# ---------------------------------------------------------------------------
# stage_filtered_library
# ---------------------------------------------------------------------------


def _write_library(root: Path) -> Path:
    """Build a tmp agent-library with sample L5 + a non-L5 file."""
    agents = root / "agents"
    agents.mkdir(parents=True)
    (agents / "codex.l5.yaml").write_text(
        yaml.safe_dump(_SAMPLE_MANIFEST, sort_keys=False),
        encoding="utf-8",
    )
    # A non-L5 file to confirm pass-through.
    (root / "reports").mkdir()
    (root / "reports" / "metric.json").write_text('{"ok": true}', encoding="utf-8")
    return root


def test_stage_filtered_library_filters_l5_manifests(tmp_path):
    src = _write_library(tmp_path / "src")
    staging = tmp_path / "staging"
    staged = stage_filtered_library(src, "public", staging)

    assert staged.is_dir()
    assert staged.name == "agent-library"

    # L5 manifest filtered
    filtered = yaml.safe_load((staged / "agents" / "codex.l5.yaml").read_text(encoding="utf-8"))
    assert [e["name"] for e in filtered["known_entities"]] == ["Bourdon"]

    # Non-L5 file copied unchanged
    assert (staged / "reports" / "metric.json").read_text(encoding="utf-8") == '{"ok": true}'


def test_stage_filtered_library_overwrites_existing(tmp_path):
    src = _write_library(tmp_path / "src")
    staging = tmp_path / "staging"
    # First stage
    stage_filtered_library(src, "public", staging)
    # Add a stray file in the staged dir
    stale = staging / "agent-library" / "stale.txt"
    stale.write_text("stale", encoding="utf-8")
    # Re-stage should wipe it
    stage_filtered_library(src, "public", staging)
    assert not stale.exists()


def test_stage_filtered_library_rejects_missing_library(tmp_path):
    with pytest.raises(SyncError, match="does not exist"):
        stage_filtered_library(tmp_path / "nope", "public", tmp_path / "staging")


def test_stage_filtered_library_rejects_bad_access_level(tmp_path):
    src = _write_library(tmp_path / "src")
    with pytest.raises(SyncError, match="access_level"):
        stage_filtered_library(src, "leaky", tmp_path / "staging")


def test_stage_filtered_library_keeps_malformed_yaml_unchanged(tmp_path):
    src = tmp_path / "src"
    (src / "agents").mkdir(parents=True)
    bad = src / "agents" / "broken.l5.yaml"
    bad.write_text("[unclosed yaml\n", encoding="utf-8")

    staged = stage_filtered_library(src, "public", tmp_path / "staging")
    assert (staged / "agents" / "broken.l5.yaml").read_text(encoding="utf-8") == "[unclosed yaml\n"


# ---------------------------------------------------------------------------
# visible_counts
# ---------------------------------------------------------------------------


def test_visible_counts_per_agent(tmp_path):
    library = _write_library(tmp_path)
    counts = visible_counts(library, "team")
    assert counts == {"codex": {"entities": 2, "sessions": 2}}


def test_visible_counts_public_strictest(tmp_path):
    library = _write_library(tmp_path)
    counts = visible_counts(library, "public")
    assert counts == {"codex": {"entities": 1, "sessions": 1}}


def test_visible_counts_missing_agents_dir(tmp_path):
    assert visible_counts(tmp_path, "public") == {}


# ---------------------------------------------------------------------------
# sync_push / sync_pull (real rsync)
# ---------------------------------------------------------------------------


def _rsync_available() -> bool:
    return shutil.which("rsync") is not None


requires_rsync = pytest.mark.skipif(
    not _rsync_available(),
    reason="rsync not on PATH",
)


@requires_rsync
def test_sync_push_writes_filtered_payload_to_local_dest(tmp_path):
    src_lib = _write_library(tmp_path / "src")
    dest = tmp_path / "dest"
    dest.mkdir()

    result = sync_push(
        str(dest) + "/",
        access_level="public",
        library_path=src_lib,
    )
    assert result.returncode == 0
    assert result.access_level == "public"

    # codex manifest landed
    landed = dest / "agents" / "codex.l5.yaml"
    assert landed.is_file()
    pushed = yaml.safe_load(landed.read_text(encoding="utf-8"))
    # Only public entity made it across
    assert [e["name"] for e in pushed["known_entities"]] == ["Bourdon"]


@requires_rsync
def test_sync_push_with_team_access_includes_team_entries(tmp_path):
    src_lib = _write_library(tmp_path / "src")
    dest = tmp_path / "dest"
    dest.mkdir()

    result = sync_push(
        str(dest) + "/",
        access_level="team",
        library_path=src_lib,
    )
    assert result.returncode == 0
    pushed = yaml.safe_load((dest / "agents" / "codex.l5.yaml").read_text(encoding="utf-8"))
    assert {e["name"] for e in pushed["known_entities"]} == {"Bourdon", "TeamProject"}
    # Private entry still excluded
    names = {e["name"] for e in pushed["known_entities"]}
    assert "MyBankAccount" not in names


@requires_rsync
def test_sync_push_dry_run_does_not_write_dest(tmp_path):
    src_lib = _write_library(tmp_path / "src")
    dest = tmp_path / "dest"
    dest.mkdir()

    result = sync_push(
        str(dest) + "/",
        access_level="public",
        library_path=src_lib,
        dry_run=True,
    )
    assert result.returncode == 0
    assert result.dry_run is True
    # rsync --dry-run leaves the destination untouched
    assert not (dest / "agents").exists()


@requires_rsync
def test_sync_push_is_idempotent(tmp_path):
    src_lib = _write_library(tmp_path / "src")
    dest = tmp_path / "dest"
    dest.mkdir()

    first = sync_push(str(dest) + "/", access_level="public", library_path=src_lib)
    second = sync_push(str(dest) + "/", access_level="public", library_path=src_lib)
    assert first.returncode == 0
    assert second.returncode == 0
    # Second run should be a no-op at the byte level
    landed = (dest / "agents" / "codex.l5.yaml").read_text(encoding="utf-8")
    assert "Bourdon" in landed


@requires_rsync
def test_sync_pull_copies_remote_into_local_library(tmp_path):
    # Stage a "remote" library and pull it.
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "agents").mkdir()
    (remote / "agents" / "codex.l5.yaml").write_text(
        yaml.safe_dump({"spec_version": "0.1", "known_entities": [{"name": "FromRemote"}]}),
        encoding="utf-8",
    )

    local = tmp_path / "local"
    result = sync_pull(str(remote) + "/", library_path=local)
    assert result.returncode == 0
    assert result.access_level is None  # pull does not filter
    pulled = yaml.safe_load((local / "agents" / "codex.l5.yaml").read_text(encoding="utf-8"))
    assert [e["name"] for e in pulled["known_entities"]] == ["FromRemote"]


@requires_rsync
def test_sync_pull_creates_local_library_if_missing(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "marker.txt").write_text("hi", encoding="utf-8")

    local = tmp_path / "does_not_exist_yet" / "agent-library"
    assert not local.exists()
    result = sync_pull(str(remote) + "/", library_path=local)
    assert result.returncode == 0
    assert (local / "marker.txt").read_text(encoding="utf-8") == "hi"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_sync_push_rejects_bad_access_level(tmp_path):
    library = _write_library(tmp_path)
    with pytest.raises(SyncError, match="access_level"):
        sync_push("/tmp/nope", access_level="leak", library_path=library)


def test_sync_push_rejects_missing_library(tmp_path):
    with pytest.raises(SyncError, match="library does not exist"):
        sync_push("/tmp/nope", library_path=tmp_path / "missing")


def test_sync_push_surfaces_rsync_missing(tmp_path, monkeypatch):
    library = _write_library(tmp_path)
    # Force the rsync lookup to fail.
    monkeypatch.setattr("core.sync.shutil.which", lambda name: None)
    with pytest.raises(RsyncMissingError):
        sync_push("/tmp/nope", library_path=library)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


def test_access_levels_ordered():
    assert ACCESS_LEVELS == ("public", "team", "private")


def test_default_push_access_level_is_public():
    assert DEFAULT_PUSH_ACCESS_LEVEL == "public"
