"""Federate shadcn/improve-style plan backlogs into the L6 store.

shadcn/improve (github.com/shadcn/improve, MIT) writes self-contained
implementation plans into a repo's ``plans/`` directory with a status index
at ``plans/README.md``. Those backlogs are repo-scoped; this adapter reads
one and commits it upward via :meth:`core.l6_store.L6Store.commit_l5` so
every federated agent can answer "what's executable right now in repo X"
without opening repo X.

This is deliberately NOT a ``participants/`` class: participants scrape
agent-native session stores, while an improve backlog is repo-scoped, not
agent-scoped. The parsers here stay reusable if that promotion ever happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from core.l6_store import L6Store

# Statuses defined by the improve plan format. BLOCKED/REJECTED may carry a
# parenthetical reason in the index, e.g. ``BLOCKED (waiting on #134)``.
_KNOWN_STATUSES = ("todo", "in progress", "done", "blocked", "rejected")

_STATUS_RE = re.compile(r"^(?P<status>[^()]*?)\s*(?:\((?P<reason>.*)\))?\s*$", re.DOTALL)
_BOLD_BULLET_RE = re.compile(r"^[-*]\s+\*\*(?P<key>.+?)\*\*\s*:?\s*(?P<value>.*)$")
_PLANNED_AT_COMMIT_RE = re.compile(r"`(?P<sha>[0-9a-fA-F]{6,40})`")
_PLANNED_AT_DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")

_INDEX_SECTION = "execution order & status"
_REJECTED_SECTION = "findings considered and rejected"
_STATUS_SECTION = "status"


@dataclass
class BacklogRow:
    """One row of the ``plans/README.md`` status table."""

    plan: str
    title: str
    priority: str | None = None
    effort: str | None = None
    depends_on: str | None = None
    status: str | None = None  # normalized: casefolded, reason stripped
    status_reason: str | None = None  # parenthetical BLOCKED/REJECTED reason


@dataclass
class BacklogIndex:
    """Parsed ``plans/README.md``: status table + rejected-findings bullets."""

    rows: list[BacklogRow] = field(default_factory=list)
    rejected_findings: list[str] = field(default_factory=list)


@dataclass
class PlanMeta:
    """The ``## Status`` bullet block of one ``plans/NNN-slug.md`` file."""

    stem: str  # file stem, e.g. "001-improve-backlog-adapter"
    priority: str | None = None
    effort: str | None = None
    risk: str | None = None
    depends_on: str | None = None
    category: str | None = None
    planned_commit: str | None = None
    planned_date: str | None = None


# ---------------------------------------------------------------------------
# Parsers (pure functions, no writes)
# ---------------------------------------------------------------------------


def _sections(text: str) -> dict[str, list[str]]:
    """Split markdown into ``##`` sections keyed by casefolded heading."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip().casefold(), [])
            continue
        if line.startswith("#") and not line.startswith("##"):
            current = None
            continue
        if current is not None:
            current.append(line)
    return sections


def _split_table_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _is_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def _parse_status_cell(raw: str) -> tuple[str | None, str | None]:
    """Normalize a status cell; capture a parenthetical reason if present."""
    match = _STATUS_RE.match(raw.strip())
    if not match:
        return raw.strip().casefold() or None, None
    status = match.group("status").strip().casefold() or None
    reason = match.group("reason")
    if reason is not None:
        reason = reason.strip() or None
    return status, reason


def parse_index(path: Path | str) -> BacklogIndex:
    """Parse ``plans/README.md``: the status table + rejected findings.

    Tolerant by design: extra columns are allowed and columns are mapped by
    header name, not position (if shadcn/improve adds or reorders columns,
    only header names matter).
    """
    text = Path(path).read_text(encoding="utf-8")
    sections = _sections(text)
    index = BacklogIndex()

    table_lines = [
        line for line in sections.get(_INDEX_SECTION, []) if line.strip().startswith("|")
    ]
    if len(table_lines) >= 2:
        headers = [h.casefold() for h in _split_table_row(table_lines[0])]
        body = table_lines[1:]
        if body and _is_separator_row(_split_table_row(body[0])):
            body = body[1:]
        for line in body:
            cells = _split_table_row(line)
            row_map = dict(zip(headers, cells, strict=False))
            plan = row_map.get("plan", "").strip()
            if not plan:
                continue
            status, reason = _parse_status_cell(row_map.get("status", ""))
            index.rows.append(
                BacklogRow(
                    plan=plan,
                    title=row_map.get("title", "").strip(),
                    priority=row_map.get("priority", "").strip() or None,
                    effort=row_map.get("effort", "").strip() or None,
                    depends_on=row_map.get("depends on", "").strip() or None,
                    status=status,
                    status_reason=reason,
                )
            )

    bullets: list[str] = []
    for line in sections.get(_REJECTED_SECTION, []):
        if re.match(r"^[-*]\s+", line):
            bullets.append(re.sub(r"^[-*]\s+", "", line).strip())
        elif line.strip() and bullets and (line.startswith(" ") or line.startswith("\t")):
            # Continuation line of a wrapped bullet.
            bullets[-1] += " " + line.strip()
    index.rejected_findings = bullets

    return index


def parse_plan_status(path: Path | str) -> PlanMeta:
    """Parse the ``## Status`` bold-bullet block of one plan file.

    Missing fields become ``None``; partial blocks never raise.
    """
    path = Path(path)
    meta = PlanMeta(stem=path.stem)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return meta
    for line in _sections(text).get(_STATUS_SECTION, []):
        match = _BOLD_BULLET_RE.match(line.strip())
        if not match:
            continue
        key = match.group("key").strip().casefold()
        value = match.group("value").strip() or None
        if key == "priority":
            meta.priority = value
        elif key == "effort":
            meta.effort = value
        elif key == "risk":
            meta.risk = value
        elif key == "depends on":
            meta.depends_on = value
        elif key == "category":
            meta.category = value
        elif key == "planned at" and value:
            commit_match = _PLANNED_AT_COMMIT_RE.search(value)
            date_match = _PLANNED_AT_DATE_RE.search(value)
            meta.planned_commit = commit_match.group("sha") if commit_match else None
            meta.planned_date = date_match.group("date") if date_match else None
    return meta


# ---------------------------------------------------------------------------
# Entity builders
# ---------------------------------------------------------------------------


def _parse_deps(raw: str | None) -> list[str]:
    if raw is None:
        return []
    cleaned = raw.strip()
    if cleaned in ("", "-", "—", "–") or cleaned.casefold() in ("none", "n/a"):
        return []
    return [dep.strip() for dep in cleaned.split(",") if dep.strip()]


def _next_executable(rows: list[BacklogRow]) -> BacklogRow | None:
    """Lowest-numbered TODO row whose dependencies are all DONE."""
    done = {row.plan for row in rows if row.status == "done"}
    todos = [row for row in rows if row.status == "todo"]
    for row in sorted(todos, key=lambda r: r.plan):
        if all(dep in done for dep in _parse_deps(row.depends_on)):
            return row
    return None


def _plan_summary(row: BacklogRow, meta: PlanMeta | None) -> str:
    status = (row.status or "unknown").upper()
    parts = [f"{row.title} — {status}"]
    detail: list[str] = []
    if row.priority:
        detail.append(row.priority)
    if row.effort:
        detail.append(f"effort {row.effort}")
    if meta and meta.risk:
        detail.append(f"risk {meta.risk}")
    if detail:
        parts.append(", ".join(detail))
    deps = _parse_deps(row.depends_on)
    parts.append(f"depends on {', '.join(deps) if deps else 'none'}")
    if meta and (meta.planned_commit or meta.planned_date):
        planned = " ".join(p for p in (meta.planned_commit, meta.planned_date) if p)
        parts.append(f"planned at {planned}")
    summary = "; ".join(parts)
    if row.status_reason:
        summary += f"; reason: {row.status_reason}"
    return summary


def build_entities(
    repo_name: str,
    index: BacklogIndex,
    plan_metas: dict[str, PlanMeta | None],
) -> list[dict]:
    """One entity per plan + one rollup entity for the repo backlog.

    Entity names are the federation merge/dedupe key — they must be stable
    across syncs: ``plan:<repo>/<file-stem>`` (falling back to the plan
    number when the plan file is missing) and ``improve-backlog:<repo>``.
    """
    entities: list[dict] = []
    counts: dict[str, int] = dict.fromkeys(_KNOWN_STATUSES, 0)

    for row in index.rows:
        meta = plan_metas.get(row.plan)
        if row.status in counts:
            counts[row.status] += 1
        tags = ["improve-plan", repo_name]
        if meta and meta.category:
            tags.append(meta.category)
        if row.status:
            tags.append(row.status)
        entities.append(
            {
                "name": f"plan:{repo_name}/{meta.stem if meta else row.plan}",
                "summary": _plan_summary(row, meta),
                "tags": tags,
                "visibility": "public",
            }
        )

    next_row = _next_executable(index.rows)
    count_text = ", ".join(f"{n} {status}" for status, n in counts.items() if n)
    next_text = f"{next_row.plan} ({next_row.title})" if next_row else "none"
    rollup_summary = (
        f"improve backlog for {repo_name}: {len(index.rows)} plan(s)"
        f"{' — ' + count_text if count_text else ''}. "
        f"Next executable plan: {next_text}."
    )
    entities.append(
        {
            "name": f"improve-backlog:{repo_name}",
            "summary": rollup_summary,
            "tags": ["improve-backlog", repo_name],
            "visibility": "public",
        }
    )
    return entities


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(
    repo_path: Path | str,
    library_path: Path | str,
    agent_id: str = "improve",
    agent_type: str = "other",
    dry_run: bool = False,
) -> dict:
    """Read ``<repo>/plans/`` and commit the backlog to the federation.

    Goes in-process through :meth:`L6Store.commit_l5` (NOT the MCP
    transport). With ``dry_run=True``, prints what would be committed and
    performs zero writes.
    """
    repo_path = Path(repo_path).resolve()
    repo_name = repo_path.name
    plans_dir = repo_path / "plans"
    index_path = plans_dir / "README.md"
    if not index_path.is_file():
        raise FileNotFoundError(
            f"no improve backlog found: {index_path} does not exist"
        )

    index = parse_index(index_path)
    plan_metas: dict[str, PlanMeta | None] = {}
    for row in index.rows:
        matches = sorted(plans_dir.glob(f"{row.plan}-*.md"))
        plan_metas[row.plan] = parse_plan_status(matches[0]) if matches else None

    entities = build_entities(repo_name, index, plan_metas)
    counts = {
        status: sum(1 for row in index.rows if row.status == status)
        for status in _KNOWN_STATUSES
    }
    session = {
        "date": date.today().isoformat(),
        "cwd": str(repo_path),
        "project_focus": [repo_name, "improve-backlog"],
        "key_actions": [
            f"synced {len(index.rows)} plans: {counts['done']} done, "
            f"{counts['todo']} todo, {counts['blocked']} blocked, "
            f"{counts['rejected']} rejected"
        ],
    }

    if dry_run:
        payload = {
            "dry_run": True,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "library": str(library_path),
            "entities": entities,
            "sessions": [session],
        }
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
        return payload

    return L6Store(Path(library_path)).commit_l5(
        agent_id,
        agent_type=agent_type,
        entities=entities,
        sessions=[session],
    )
