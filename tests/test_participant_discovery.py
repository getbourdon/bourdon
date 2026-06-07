"""Tests for participants.discover_participants -- scan-based auto-discovery.

These lock in the single-source-of-truth contract: the participant set is a scan
of the ``participants/`` package, not a hand-maintained list and not entry-point
metadata (which is empty when bourdon runs from source). They also prove the
"drop a module, get a participant" property end to end.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import participants
from participants import discover_participants
from participants.base import BourdonParticipant

# The participants that ship in-tree today. If you add a top-level participant
# module, add its agent id here -- that is the *only* list that should need
# touching, and it exists to make new participants a deliberate, reviewed change.
EXPECTED_AGENT_IDS = [
    "cascade",
    "claude-code",
    "claude-code-automations",
    "claude-desktop-code",
    "claude-desktop-cowork",
    "codex",
    "codex-automations",
    "copilot",
    "cursor",
]


def test_discover_returns_expected_ids_sorted():
    ids = [agent_id for agent_id, _ in discover_participants()]
    assert ids == EXPECTED_AGENT_IDS
    assert ids == sorted(ids)


def test_discover_skips_base_and_private_modules():
    ids = [agent_id for agent_id, _ in discover_participants()]
    # base is the Protocol module; _cursor_sqlite / llama_cpp_backend are helpers.
    assert "base" not in ids
    assert "_cursor_sqlite" not in ids
    assert "llama_cpp_backend" not in ids


def test_discover_returns_no_duplicate_ids():
    ids = [agent_id for agent_id, _ in discover_participants()]
    assert len(ids) == len(set(ids))


def test_discovered_classes_conform_to_protocol():
    """Every discovered class satisfies the BourdonParticipant Protocol.

    The marker attributes are exactly what discovery keys on, and the Protocol is
    ``runtime_checkable`` so a structural ``isinstance`` is the strongest check.
    Constructors that require no args are additionally instantiated and checked.
    """
    for agent_id, cls in discover_participants():
        assert isinstance(cls, type)
        for attr in ("agent_id", "agent_type", "export_l5", "health_check"):
            assert hasattr(cls, attr), f"{agent_id} missing {attr}"
        assert cls.agent_id == agent_id
        try:
            instance = cls()
        except TypeError:
            # Constructor needs explicit args (none of the shipped ones do today,
            # but stay tolerant); the attr checks above already cover the class.
            continue
        assert isinstance(instance, BourdonParticipant)


def test_discover_is_pure_and_repeatable():
    """Two back-to-back calls return identical (id, class) pairs."""
    first = discover_participants()
    second = discover_participants()
    assert first == second


def test_dropping_in_a_module_registers_a_new_participant(tmp_path):
    """Drop-in demonstration: writing a new participant module into the package
    makes it appear in discovery with no edits to any registry, then removing it
    makes it disappear again. This is the whole point of the refactor.
    """
    module_name = "dropinprobe"
    pkg_dir = Path(participants.__file__).parent
    probe_path = pkg_dir / f"{module_name}.py"
    assert not probe_path.exists(), "probe module name collided with a real module"

    probe_path.write_text(
        textwrap.dedent(
            '''
            """Throwaway participant used by the discovery drop-in test."""
            from __future__ import annotations

            from pathlib import Path


            class DropInProbeParticipant:
                agent_id = "dropin-probe"
                agent_type = "other"
                native_path = "/tmp/dropin-probe"

                @classmethod
                def default_native_path(cls, home: Path | None = None) -> Path:
                    return (home or Path.home()) / ".dropin-probe"

                def discover(self):  # pragma: no cover - not exercised here
                    ...

                def export_l5(self, since=None):  # pragma: no cover
                    ...

                def export_sessions(self, since, limit=100):  # pragma: no cover
                    ...

                def health_check(self):  # pragma: no cover
                    ...
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    try:
        # pkgutil.iter_modules reads the directory; invalidate caches so the
        # freshly written file is visible to the import machinery.
        importlib.invalidate_caches()
        found = dict(discover_participants())
        assert "dropin-probe" in found
        assert found["dropin-probe"].__name__ == "DropInProbeParticipant"
    finally:
        probe_path.unlink()
        sys.modules.pop(f"participants.{module_name}", None)
        importlib.invalidate_caches()

    # After removal, discovery is back to the shipped set.
    ids_after = [agent_id for agent_id, _ in discover_participants()]
    assert "dropin-probe" not in ids_after
    assert ids_after == EXPECTED_AGENT_IDS


def test_broken_module_is_skipped_not_fatal(tmp_path, caplog):
    """A module that raises on import is logged and skipped, never crashing
    discovery -- one broken participant must not take down the whole CLI.
    """
    module_name = "brokenprobe"
    pkg_dir = Path(participants.__file__).parent
    probe_path = pkg_dir / f"{module_name}.py"
    assert not probe_path.exists()

    probe_path.write_text(
        'raise RuntimeError("boom on import")\n',
        encoding="utf-8",
    )
    try:
        importlib.invalidate_caches()
        with caplog.at_level("WARNING", logger="participants"):
            ids = [agent_id for agent_id, _ in discover_participants()]
        # The shipped participants still discovered; the broken one contributes nothing.
        assert ids == EXPECTED_AGENT_IDS
        assert any(module_name in rec.message for rec in caplog.records)
    finally:
        probe_path.unlink()
        sys.modules.pop(f"participants.{module_name}", None)
        importlib.invalidate_caches()
