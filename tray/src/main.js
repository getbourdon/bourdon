// Bourdon Desktop Tray — Phase 0 frontend logic.
// BSL 1.1 (repo root LICENSE). No framework: plain DOM.
//
// Data flow: invoke the Rust `get_agents` command (runs the Bourdon CLI via an
// argv array, no shell), which returns AgentsResult { health, report, error,
// command } and updates the tray icon as a side effect. We render one of:
//   loading / error / empty / overview / detail
// The OVERVIEW is a quiet agent list; clicking an agent opens its DEEP REPORT
// (detail). The tray "Refresh" item emits `agents-refreshed` with the same
// payload so an open window stays in sync.

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const FRESHNESS_DAYS = 7;

const HEALTH_TEXT = {
  grey: "No agents",
  green: "Fresh",
  yellow: "Attention",
  red: "Unreadable",
};

// ---- view state -------------------------------------------------------------

let lastResult = null; // most recent AgentsResult
let openAgentId = null; // id of the agent in the detail view, or null

// ---- helpers ----------------------------------------------------------------

function el(id) {
  return document.getElementById(id);
}

const STATES = [
  "state-loading",
  "state-error",
  "state-empty",
  "state-overview",
  "state-detail",
];
function showOnly(stateId) {
  for (const s of STATES) el(s).classList.toggle("hidden", s !== stateId);
}

function setHealth(health) {
  const badge = el("health-badge");
  badge.dataset.health = health;
  el("health-text").textContent = HEALTH_TEXT[health] || health;
}

function startOfDay(dt) {
  return new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
}

function daysSince(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  return Math.floor((startOfDay(new Date()) - startOfDay(d)) / 86400000);
}

function humanize(iso) {
  const days = daysSince(iso);
  if (days == null) return "unknown";
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
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

// Freshness "pulse" class for an agent's overview dot.
function pulseClass(a) {
  if (a.parse_error) return "err";
  const days = daysSince(a.freshest_session_date);
  if (days == null) return "idle"; // no sessions yet
  if (days <= FRESHNESS_DAYS) return "fresh";
  return "stale";
}

// ---- overview ---------------------------------------------------------------

function renderOverview(agents) {
  el("agent-count").textContent = String(agents.length);
  const root = el("agent-list");
  root.innerHTML = "";
  for (const a of agents) {
    const row = document.createElement("div");
    row.className = "agent-row";
    row.tabIndex = 0;

    const sub = a.parse_error
      ? "manifest error"
      : [
          a.type || "agent",
          a.instance || null,
          typeof a.session_count === "number"
            ? a.session_count > 0
              ? `${a.session_count} session${a.session_count === 1 ? "" : "s"}`
              : "no sessions yet"
            : null,
        ]
          .filter(Boolean)
          .join(" · ");

    const touch = a.parse_error ? "" : humanize(a.last_updated);

    row.innerHTML = `
      <span class="agent-pulse ${pulseClass(a)}"></span>
      <span class="agent-main">
        <div class="agent-id">${esc(a.id)}</div>
        <div class="agent-sub">${esc(sub)}</div>
      </span>
      <span class="agent-touch">${esc(touch)}</span>
      <span class="chev">›</span>`;

    row.addEventListener("click", () => openDetail(a.id));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") openDetail(a.id);
    });
    root.appendChild(row);
  }
}

// ---- detail (deep report) ---------------------------------------------------

function openDetail(id) {
  openAgentId = id;
  renderDetail();
  if (!el("state-detail").classList.contains("hidden")) return;
  showOnly("state-detail");
}

function renderDetail() {
  const agents = (lastResult && lastResult.report && lastResult.report.agents) || [];
  const a = agents.find((x) => x.id === openAgentId);
  const body = el("detail-body");
  if (!a) {
    // Agent vanished between refreshes — fall back to the overview.
    showOverview();
    return;
  }

  if (a.parse_error) {
    body.innerHTML = `
      <div class="detail-id">${esc(a.id)}</div>
      <div class="detail-type">manifest error</div>
      <div class="detail-error" style="margin-top:12px">⚠ Could not parse this agent's memory:<br />${esc(
        a.parse_error
      )}</div>`;
    return;
  }

  const stat = (val, label) =>
    `<div class="stat"><b>${esc(val)}</b><span>${esc(label)}</span></div>`;

  const sessions = (a.recent_activity || [])
    .map((s) => {
      const focus =
        s.project_focus && s.project_focus.length
          ? `<div class="sess-focus">${esc(s.project_focus.join(", "))}</div>`
          : "";
      const actions =
        s.key_actions && s.key_actions.length
          ? `<ul class="sess-actions">${s.key_actions
              .map((k) => `<li>${esc(k)}</li>`)
              .join("")}</ul>`
          : "";
      return `
        <div class="sess">
          <div class="sess-head">
            <span class="sess-date">${esc(s.date || "—")}</span>
            <span class="vis-badge ${visClass(s.visibility)}">${esc(
        s.visibility || "team"
      )}</span>
          </div>
          ${focus}
          ${actions}
        </div>`;
    })
    .join("");

  const sessionBlock =
    a.session_count > 0
      ? `<div class="detail-section-title">Recent sessions</div>${
          sessions || `<p class="muted small">No detail recorded.</p>`
        }`
      : `<p class="muted small">No sessions recorded yet — this agent is registered but hasn't published activity.</p>`;

  body.innerHTML = `
    <div class="detail-id">${esc(a.id)}</div>
    <div class="detail-type">${esc(a.type || "agent")}${
    a.instance ? " · " + esc(a.instance) : ""
  }</div>
    <div class="detail-stats">
      ${stat(humanize(a.last_updated), "last touched")}
      ${stat(a.session_count ?? "—", "sessions")}
      ${stat(a.capability_count ?? "—", "capabilities")}
    </div>
    ${
      a.role_narrative
        ? `<div class="detail-role">${esc(a.role_narrative)}</div>`
        : ""
    }
    ${sessionBlock}`;
}

function showOverview() {
  openAgentId = null;
  const agents = (lastResult && lastResult.report && lastResult.report.agents) || [];
  renderOverview(agents);
  showOnly("state-overview");
}

// ---- main render dispatch ---------------------------------------------------

function render(result) {
  lastResult = result;
  setHealth(result.health);

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

  // If we were viewing a detail and that agent still exists, stay there.
  if (openAgentId && agents.some((a) => a.id === openAgentId)) {
    renderOverview(agents); // keep the back-target fresh
    renderDetail();
    showOnly("state-detail");
  } else {
    showOverview();
  }
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
el("back-btn").addEventListener("click", showOverview);

listen("agents-refreshed", (event) => {
  if (event.payload) render(event.payload);
});

// Re-read when the window regains focus (covers tray "Open Bourdon" + reshow).
window.addEventListener("focus", load);

load();
