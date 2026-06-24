"""Tests for `bourdon codex sync-native --from-library` (issue #75).

Covers the federation-sourced renderer added to make `sync-native` produce
a non-trivial `bourdon_fallback.md` on a fresh machine where Codex has no
local sessions yet but `~/agent-library/agents/*.l5.yaml` is populated
(e.g. via cross-machine federation transport).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from participants.codex import (
    _build_codex_native_memory_payload,
    _render_codex_federation_memory_text,
    _render_codex_native_memory_text,
)


def _write_l5(library: Path, agent_id: str, manifest: dict) -> Path:
    target = library / "agents" / f"{agent_id}.l5.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return target


def _stub_manifest(
    agent_id: str,
    entities: list[dict],
    sessions: list[dict] | None = None,
) -> dict:
    return {
        "spec_version": "0.1",
        "agent": {"id": agent_id, "type": "code-assistant"},
        "known_entities": entities,
        "recent_sessions": sessions or [],
    }


@pytest.fixture
def populated_library(tmp_path):
    """An agent-library populated with two agents' L5 manifests.

    Returns the library root path. Codex and Claude-Code both claim the
    "Bourdon" entity (so dedupe + source-attribution can be verified);
    Codex alone claims "Continuo"; Claude-Code alone claims "ILTT".
    """
    library = tmp_path / "agent-library"
    _write_l5(
        library,
        "codex",
        _stub_manifest(
            "codex",
            entities=[
                {
                    "name": "Bourdon",
                    "type": "project",
                    "summary": "Cross-agent memory federation protocol.",
                    "visibility": "team",
                },
                {
                    "name": "Continuo",
                    "type": "project",
                    "summary": "Public-name federation product.",
                    "visibility": "team",
                },
                {
                    "name": "recognition-first",
                    "type": "concept",
                    "summary": "Recognition layer ahead of retrieval.",
                    "visibility": "team",
                },
            ],
            sessions=[
                {
                    "date": "2026-05-15",
                    "cwd": "/Users/cumul/.bourdon-venv",
                    "project_focus": ["Bourdon"],
                    "key_actions": ["Validated cross-account proof on PC"],
                    "visibility": "team",
                }
            ],
        ),
    )
    _write_l5(
        library,
        "claude-code",
        _stub_manifest(
            "claude-code",
            entities=[
                {
                    "name": "Bourdon",
                    "type": "project",
                    "summary": "Federation substrate. Same project, claude-code POV.",
                    "visibility": "team",
                },
                {
                    "name": "ILTT",
                    "type": "project",
                    "summary": "iOS+Android language teaching app.",
                    "visibility": "team",
                },
                {
                    "name": "private-fact",
                    "type": "concept",
                    "summary": "Only visible at private access level.",
                    "visibility": "private",
                },
            ],
        ),
    )
    return library


def test_renderer_on_fresh_substrate_surfaces_federation_anchors(populated_library):
    """On a fresh machine the federation library must drive non-trivial output.

    This is the empirical gap from the 2026-05-17 cross-machine test:
    the old renderer produced "No project anchors recovered yet." with
    285 entities sitting right next to it. Validates the new path closes
    that gap.
    """
    text = _render_codex_federation_memory_text(
        library_path=populated_library,
        access_level="team",
    )

    assert "# Bourdon Fallback Memory" in text
    assert "## Recovered Projects" in text
    assert "## Recovered Concepts" in text
    # Federated entities surface (not the empty placeholders).
    assert "Bourdon" in text
    assert "Continuo" in text
    assert "ILTT" in text
    assert "recognition-first" in text
    assert "No project anchors recovered yet." not in text


def test_renderer_attributes_source_agents_per_entity(populated_library):
    """Each entity row must carry `(via <agent>, <agent>)` provenance."""
    text = _render_codex_federation_memory_text(
        library_path=populated_library,
        access_level="team",
    )
    # Bourdon is shared across both agents -- source list shows both.
    assert "(via claude-code, codex)" in text or "(via codex, claude-code)" in text
    # ILTT only comes from claude-code.
    assert "ILTT" in text
    iltt_line = next(line for line in text.splitlines() if line.startswith("- ILTT"))
    assert "(via claude-code)" in iltt_line
    assert "codex" not in iltt_line


def test_access_level_team_excludes_private_entities(populated_library):
    """`visibility: private` entity must not appear at access_level=team."""
    text = _render_codex_federation_memory_text(
        library_path=populated_library,
        access_level="team",
    )
    assert "private-fact" not in text


def test_access_level_public_excludes_team_entities(populated_library):
    """Federation lib written at `visibility: team` must not leak at public level."""
    text = _render_codex_federation_memory_text(
        library_path=populated_library,
        access_level="public",
    )
    # All seeded entities are team or private -- nothing should surface at public.
    # ("Bourdon" still appears in the header "# Bourdon Fallback Memory", so
    # check for the list-item shape instead.)
    assert "- Bourdon" not in text
    assert "- ILTT" not in text
    assert "- No project anchors recovered yet." in text
    assert "- No concept anchors recovered yet." in text


def test_access_level_private_includes_private_entities(populated_library):
    text = _render_codex_federation_memory_text(
        library_path=populated_library,
        access_level="private",
    )
    assert "private-fact" in text


def test_renderer_surfaces_recent_federation_sessions(populated_library):
    text = _render_codex_federation_memory_text(
        library_path=populated_library,
        access_level="team",
    )
    assert "## Recent Federation Sessions" in text
    assert "2026-05-15" in text
    assert "Validated cross-account proof on PC" in text


def test_include_local_appends_local_codex_history_section(
    populated_library, tmp_path
):
    """--include-local must concatenate the local renderer's output."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    text = _render_codex_federation_memory_text(
        library_path=populated_library,
        access_level="team",
        include_local=True,
        codex_home=codex_home,
    )
    assert "## Recovered Projects" in text  # federation section first
    assert "Bourdon" in text
    assert "## Local Codex History" in text
    # Local section's own renderer output is embedded; with empty codex_home
    # it falls back to the canonical "No Codex session records found." line.
    assert "No Codex session records found." in text


def test_payload_dispatches_to_local_renderer_by_default(tmp_path):
    """No flags = unchanged behavior (back-compat for callers like prepare-turn)."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    expected = _render_codex_native_memory_text(codex_home, codex_brain=None)
    payload = _build_codex_native_memory_payload(
        codex_home=codex_home,
        codex_brain=None,
    )
    assert payload["text"] == expected
    assert "## Recent Codex Threads" in payload["text"]  # local section name


def test_payload_dispatches_to_federation_renderer_with_flag(
    populated_library, tmp_path
):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    payload = _build_codex_native_memory_payload(
        codex_home=codex_home,
        codex_brain=None,
        from_library=True,
        library_path=populated_library,
        access_level="team",
    )
    assert "Bourdon" in payload["text"]
    assert "## Recent Federation Sessions" in payload["text"]
    # Fresh codex_home -> fallback_recall still computed correctly.
    assert payload["fallback_recall"]["status"] == "missing"
    assert payload["bytes"] == len(payload["text"].encode("utf-8"))


def test_empty_library_renders_safe_placeholders(tmp_path):
    empty_library = tmp_path / "empty-library"
    (empty_library / "agents").mkdir(parents=True)
    text = _render_codex_federation_memory_text(
        library_path=empty_library,
        access_level="team",
    )
    assert "- No project anchors recovered yet." in text
    assert "- No concept anchors recovered yet." in text
    assert "- No federation sessions recovered yet." in text


def test_federation_renderer_redacts_credential_shaped_summary(tmp_path):
    """L5 manifests authored by other agents may contain credential-shaped
    text in entity or session summaries. `_safe_native_memory_text`'s
    redaction must apply uniformly through the federation render path.

    Locks the redaction observed in the live smoke run into CI -- prior to
    this test, the only evidence that the redaction filter still applied
    after the federation render rewrite was the manual 22KB preview.
    """
    library = tmp_path / "agent-library"
    _write_l5(
        library,
        "claude-code",
        _stub_manifest(
            "claude-code",
            entities=[
                {
                    "name": "Acme deploy creds",
                    "type": "concept",
                    # Three different credential shapes from the sensitive-pattern set:
                    # the literal word "password", an api_key style identifier, and
                    # a Stripe sk_live_ token. All must redact.
                    "summary": (
                        "password=hunter2 api_key=AKIA1234567890 "
                        "sk_live_abcdef0123456789xyz"
                    ),
                    "visibility": "team",
                },
            ],
            sessions=[
                {
                    "date": "2026-05-18",
                    "cwd": "/tmp",
                    "project_focus": ["Acme deploy creds"],
                    "key_actions": ["rotated bearer token after leak"],
                    "visibility": "team",
                }
            ],
        ),
    )
    text = _render_codex_federation_memory_text(
        library_path=library,
        access_level="team",
    )
    # Entity-summary redaction.
    assert "[redacted credential-like text]" in text
    assert "hunter2" not in text
    assert "AKIA1234567890" not in text
    assert "sk_live_abcdef0123456789xyz" not in text
    # Session-action redaction (bearer token pattern).
    assert "rotated bearer token after leak" not in text


def test_from_library_with_memory_md_wraps_federation_output(
    populated_library, tmp_path
):
    """`--from-library --memory-md` is the actual user-facing recognition
    surface on Codex (bounded BOURDON section inside ~/.codex/memories/MEMORY.md
    rather than the standalone bourdon_fallback.md). Verify the merge wraps
    federation content between the BOURDON markers and preserves any
    existing user-authored content in MEMORY.md.
    """
    from cli.main import _build_parser

    codex_home = tmp_path / ".codex"
    memories = codex_home / "memories"
    memories.mkdir(parents=True)
    memory_md = memories / "MEMORY.md"
    # Pre-existing user-authored MEMORY.md content the merge must not destroy.
    memory_md.write_text(
        "# Codex Memory\n\nUser-authored note one.\nUser-authored note two.\n",
        encoding="utf-8",
    )

    parser = _build_parser()
    args = parser.parse_args(
        [
            "codex",
            "sync-native",
            "--write",
            "--from-library",
            "--memory-md",
            "--library-path",
            str(populated_library),
            "--access-level",
            "team",
            "--codex-home",
            str(codex_home),
        ]
    )
    rc = args.func(args)
    assert rc == 0

    merged = memory_md.read_text(encoding="utf-8")
    # Pre-existing content survives.
    assert "User-authored note one." in merged
    assert "User-authored note two." in merged
    # Federation content lands between the canonical BOURDON markers.
    assert "<!-- BEGIN BOURDON FALLBACK MEMORY -->" in merged
    assert "<!-- END BOURDON FALLBACK MEMORY -->" in merged
    begin_idx = merged.index("<!-- BEGIN BOURDON FALLBACK MEMORY -->")
    end_idx = merged.index("<!-- END BOURDON FALLBACK MEMORY -->")
    bourdon_block = merged[begin_idx:end_idx]
    # Federation entities surface inside the bounded block specifically.
    assert "Bourdon" in bourdon_block
    assert "(via" in bourdon_block
    # User content is outside the bourdon block, not inside it.
    assert "User-authored note one." not in bourdon_block


def test_from_library_with_memory_md_replaces_stale_bourdon_section(
    populated_library, tmp_path
):
    """Re-running --from-library --memory-md must replace the prior BOURDON
    block in-place rather than appending a second one."""
    from cli.main import _build_parser

    codex_home = tmp_path / ".codex"
    memories = codex_home / "memories"
    memories.mkdir(parents=True)
    memory_md = memories / "MEMORY.md"
    memory_md.write_text(
        "# Codex Memory\n\n"
        "<!-- BEGIN BOURDON FALLBACK MEMORY -->\n"
        "stale content from a previous run\n"
        "<!-- END BOURDON FALLBACK MEMORY -->\n",
        encoding="utf-8",
    )

    parser = _build_parser()
    args = parser.parse_args(
        [
            "codex",
            "sync-native",
            "--write",
            "--from-library",
            "--memory-md",
            "--library-path",
            str(populated_library),
            "--codex-home",
            str(codex_home),
        ]
    )
    rc = args.func(args)
    assert rc == 0

    merged = memory_md.read_text(encoding="utf-8")
    assert merged.count("<!-- BEGIN BOURDON FALLBACK MEMORY -->") == 1
    assert merged.count("<!-- END BOURDON FALLBACK MEMORY -->") == 1
    assert "stale content from a previous run" not in merged
    assert "Bourdon" in merged


def test_cli_handler_threads_flags(populated_library, tmp_path, monkeypatch, capsys):
    """End-to-end: argparse subparser routes the new flags into the payload."""
    from cli.main import _build_parser

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    target = tmp_path / "out.md"

    parser = _build_parser()
    args = parser.parse_args(
        [
            "codex",
            "sync-native",
            "--write",
            "--from-library",
            "--library-path",
            str(populated_library),
            "--access-level",
            "team",
            "--out",
            str(target),
            "--codex-home",
            str(codex_home),
        ]
    )
    rc = args.func(args)
    assert rc == 0
    written = target.read_text(encoding="utf-8")
    assert "Bourdon" in written
    assert "(via" in written  # source attribution present
