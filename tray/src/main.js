// Bourdon Desktop Tray — Phase 0 frontend logic.
// BSL 1.1 (see repo root LICENSE). No framework: plain DOM.
//
// Data flow: invoke the Rust `get_agents` command, which runs the Bourdon CLI
// (argv array, no shell), returns a typed AgentsResult { health, report, error,
// command }, and as a side effect updates the tray icon. We render the result
// into one of four UI states. The tray "Refresh" item emits an `agents-refreshed`
// event carrying the same payload, so an open window stays in sync.

// `withGlobalTauri: true` exposes the API on window.__TAURI__.
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

// ---- helpers ----------------------------------------------------------------

const HEALTH_TEXT = {
  grey: "No agents yet",
  green: "Present & fresh",
  yellow: "Attention warranted",
  red: "Can't read memory",
};

function el(id) {
  return document.getElementById(id);
}

function showOnly(stateId) {
  for (const s of ["state-loading", "state-error", "state-empty", "state-ok"]) {
    el(s).classList.toggle("hidden", s !== stateId);
  }
}

function setHealth(health) {
  const badge = el("health-badge");
  badge.dataset.health = health;
  el("health-text").textContent = HEALTH_TEXT[health] || health;
}

// Humanize an ISO-ish timestamp into "today / N days ago / a date".
function humanizeLastTouched(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const now = new Date();
  const dayMs = 86400000;
  const days = Math.floor((startOfDay(now) - startOfDay(d)) / dayMs);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  if (days < 30) return `${Math.floor(days / 7)} wk ago`;
  return d.toISOString().slice(0, 10);
}

function startOfDay(dt) {
  return new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
}

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function visClass(v) {
  if (v === "public") return "vis-public";
  if (v === "private") return "vis-private";
  return "";
}

// ---- renderers --------------------------------------------------------------

function renderAgents(agents) {
  const root = el("agent-list");
  root.innerHTML = "";
  for (const a of agents) {
    const card = document.createElement("div");
    card.className = "agent-card" + (a.parse_error ? " has-error" : "");

    if (a.parse_error) {
      card.innerHTML = `
        <div class="agent-head">
          <span class="agent-id">${esc(a.id)}</span>
          <span class="agent-type">manifest error</span>
        </div>
        <div class="agent-error">⚠ Could not parse this agent's memory: ${esc(
          a.parse_error
        )}</div>`;
      root.appendChild(card);
      continue;
    }

    const sessions = a.session_count;
    const hasSessions = typeof sessions === "number" && sessions > 0;
    const sessionText = hasSessions
      ? `${sessions} session${sessions === 1 ? "" : "s"}`
      : `<span class="badge-nosessions">no sessions yet</span>`;
    const caps =
      typeof a.capability_count === "number"
        ? `${a.capability_count} capabilit${
            a.capability_count === 1 ? "y" : "ies"
          }`
        : "—";

    card.innerHTML = `
      <div class="agent-head">
        <span class="agent-id">${esc(a.id)}</span>
        <span class="agent-type">${esc(a.type || "agent")}${
      a.instance ? " · " + esc(a.instance) : ""
    }</span>
      </div>
      <div class="agent-meta">
        <span>last touched ${esc(humanizeLastTouched(a.last_updated))}</span>
        <span>${caps}</span>
        <span>${sessionText}</span>
      </div>
      ${
        a.role_narrative
          ? `<div class="agent-role">${esc(a.role_narrative)}</div>`
          : ""
      }`;
    root.appendChild(card);
  }
}

function renderActivity(agents) {
  // Aggregate every agent's recent_activity into one date-desc list, cap ~30.
  const rows = [];
  for (const a of agents) {
    if (a.parse_error) continue;
    for (const act of a.recent_activity || []) {
      rows.push({ agentId: a.id, ...act });
    }
  }
  rows.sort((x, y) => (y.date || "").localeCompare(x.date || ""));
  const capped = rows.slice(0, 30);

  const root = el("activity-list");
  root.innerHTML = "";
  if (capped.length === 0) {
    root.innerHTML = `<p class="muted small">No recorded activity yet.</p>`;
    return;
  }
  for (const r of capped) {
    const action = (r.key_actions && r.key_actions[0]) || "";
    const focus =
      r.project_focus && r.project_focus.length
        ? r.project_focus.join(", ")
        : "";
    const row = document.createElement("div");
    row.className = "activity-row";
    row.innerHTML = `
      <div class="activity-line1">
        <span class="activity-date">${esc(r.date || "—")}</span>
        <span class="activity-agent">${esc(r.agentId)}</span>
        ${focus ? `<span class="activity-focus">${esc(focus)}</span>` : ""}
        <span class="vis-badge ${visClass(r.visibility)}">${esc(
      r.visibility || "team"
    )}</span>
      </div>
      ${action ? `<div class="activity-action">${esc(action)}</div>` : ""}`;
    root.appendChild(row);
  }
}

// ---- main render dispatch ---------------------------------------------------

function render(result) {
  setHealth(result.health);

  // Red CLI-failure path: no report, show error + the command we ran.
  if (result.health === "red" && !result.report) {
    el("error-detail").textContent =
      result.error || "The Bourdon CLI could not be read.";
    el("error-command").textContent = "$ " + (result.command || "");
    showOnly("state-error");
    return;
  }

  const report = result.report || { agents: [], generated_from: "" };
  const agents = report.agents || [];

  if (agents.length === 0) {
    el("empty-source").textContent = report.generated_from
      ? `Looked in: ${report.generated_from}`
      : "";
    showOnly("state-empty");
    return;
  }

  el("agent-count").textContent = String(agents.length);
  renderAgents(agents);
  renderActivity(agents);
  showOnly("state-ok");
}

// ---- load + wiring ----------------------------------------------------------

let inFlight = false;

async function load() {
  if (inFlight) return;
  inFlight = true;
  const btn = el("refresh-btn");
  btn.disabled = true;
  try {
    const result = await invoke("get_agents");
    render(result);
  } catch (e) {
    // Hard failure invoking the command itself (should be rare).
    setHealth("red");
    el("error-detail").textContent =
      "Internal error invoking the read command: " + String(e);
    el("error-command").textContent = "";
    showOnly("state-error");
  } finally {
    btn.disabled = false;
    inFlight = false;
  }
}

el("refresh-btn").addEventListener("click", load);

// Tray "Refresh" pushes a fresh payload — render it without a second CLI call.
listen("agents-refreshed", (event) => {
  if (event.payload) render(event.payload);
});

// Re-read when the window regains focus (covers tray "Open Bourdon" + reshow).
window.addEventListener("focus", load);

// Initial load.
load();
