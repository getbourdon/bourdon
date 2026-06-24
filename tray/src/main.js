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

// Official product branding for each agent. Display name + the brand mark
// (single-path SVG from the simple-icons set; CC0 icon files, trademarks remain
// each owner's — used nominatively to identify the product). All product names
// and logos are trademarks of their respective owners; Bourdon is not affiliated
// with or endorsed by them.
const AGENT_BRANDING = {
  "claude-code": {
    name: "Claude Code",
    d: "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z",
  },
  codex: {
    name: "Codex",
    d: "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z",
  },
  copilot: {
    name: "GitHub Copilot",
    d: "M23.922 16.997C23.061 18.492 18.063 22.02 12 22.02 5.937 22.02.939 18.492.078 16.997A.641.641 0 0 1 0 16.741v-2.869a.883.883 0 0 1 .053-.22c.372-.935 1.347-2.292 2.605-2.656.167-.429.414-1.055.644-1.517a10.098 10.098 0 0 1-.052-1.086c0-1.331.282-2.499 1.132-3.368.397-.406.89-.717 1.474-.952C7.255 2.937 9.248 1.98 11.978 1.98c2.731 0 4.767.957 6.166 2.093.584.235 1.077.546 1.474.952.85.869 1.132 2.037 1.132 3.368 0 .368-.014.733-.052 1.086.23.462.477 1.088.644 1.517 1.258.364 2.233 1.721 2.605 2.656a.841.841 0 0 1 .053.22v2.869a.641.641 0 0 1-.078.256Zm-11.75-5.992h-.344a4.359 4.359 0 0 1-.355.508c-.77.947-1.918 1.492-3.508 1.492-1.725 0-2.989-.359-3.782-1.259a2.137 2.137 0 0 1-.085-.104L4 11.746v6.585c1.435.779 4.514 2.179 8 2.179 3.486 0 6.565-1.4 8-2.179v-6.585l-.098-.104s-.033.045-.085.104c-.793.9-2.057 1.259-3.782 1.259-1.59 0-2.738-.545-3.508-1.492a4.359 4.359 0 0 1-.355-.508Zm2.328 3.25c.549 0 1 .451 1 1v2c0 .549-.451 1-1 1-.549 0-1-.451-1-1v-2c0-.549.451-1 1-1Zm-5 0c.549 0 1 .451 1 1v2c0 .549-.451 1-1 1-.549 0-1-.451-1-1v-2c0-.549.451-1 1-1Zm3.313-6.185c.136 1.057.403 1.913.878 2.497.442.544 1.134.938 2.344.938 1.573 0 2.292-.337 2.657-.751.384-.435.558-1.15.558-2.361 0-1.14-.243-1.847-.705-2.319-.477-.488-1.319-.862-2.824-1.025-1.487-.161-2.192.138-2.533.529-.269.307-.437.808-.438 1.578v.021c0 .265.021.562.063.893Zm-1.626 0c.042-.331.063-.628.063-.894v-.02c-.001-.77-.169-1.271-.438-1.578-.341-.391-1.046-.69-2.533-.529-1.505.163-2.347.537-2.824 1.025-.462.472-.705 1.179-.705 2.319 0 1.211.175 1.926.558 2.361.365.414 1.084.751 2.657.751 1.21 0 1.902-.394 2.344-.938.475-.584.742-1.44.878-2.497Z",
  },
  cursor: {
    name: "Cursor",
    d: "M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726v11.188c0 .3.162.575.42.724l9.609 5.55a1 1 0 0 0 .998 0l9.61-5.55a.84.84 0 0 0 .42-.724V6.404a.84.84 0 0 0-.42-.726L12.497.131a1.01 1.01 0 0 0-.996 0M2.657 6.338h18.55c.263 0 .43.287.297.515L12.23 22.918c-.062.107-.229.064-.229-.06V12.335a.59.59 0 0 0-.295-.51l-9.11-5.257c-.109-.063-.064-.23.061-.23",
  },
  cascade: {
    name: "Windsurf",
    d: "M23.55 5.067c-1.2038-.002-2.1806.973-2.1806 2.1765v4.8676c0 .972-.8035 1.7594-1.7597 1.7594-.568 0-1.1352-.286-1.4718-.7659l-4.9713-7.1003c-.4125-.5896-1.0837-.941-1.8103-.941-1.1334 0-2.1533.9635-2.1533 2.153v4.8957c0 .972-.7969 1.7594-1.7596 1.7594-.57 0-1.1363-.286-1.4728-.7658L.4076 5.1598C.2822 4.9798 0 5.0688 0 5.2882v4.2452c0 .2147.0656.4228.1884.599l5.4748 7.8183c.3234.462.8006.8052 1.3509.9298 1.3771.313 2.6446-.747 2.6446-2.0977v-4.893c0-.972.7875-1.7593 1.7596-1.7593h.003a1.798 1.798 0 0 1 1.4718.7658l4.9723 7.0994c.4135.5905 1.05.941 1.8093.941 1.1587 0 2.1515-.9645 2.1515-2.153v-4.8948c0-.972.7875-1.7594 1.7596-1.7594h.194a.22.22 0 0 0 .2204-.2202v-4.622a.22.22 0 0 0-.2203-.2203Z",
  },
};

// Official brand hex per agent (from simple-icons metadata). Most of these
// products use a black/near-black mark; Claude is coral.
const AGENT_HEX = {
  "claude-code": "#D97757",
  codex: "#000000",
  copilot: "#000000",
  cursor: "#000000",
  cascade: "#0B100F",
};

function agentBrand(id) {
  const base = (id || "").replace(/-automations$/, "");
  const b = AGENT_BRANDING[base];
  if (!b) return { name: id || "agent", d: null, hex: null };
  return {
    name: id.endsWith("-automations") ? `${b.name} · Automations` : b.name,
    d: b.d,
    hex: AGENT_HEX[base] || null,
  };
}

function logoMarkup(d, cls, hex) {
  if (!d) return `<span class="agent-logo agent-logo--fallback ${cls}"></span>`;
  const style = hex ? ` style="fill:${hex}"` : "";
  return `<svg class="agent-logo ${cls}" viewBox="0 0 24 24" aria-hidden="true"${style}><path d="${d}" /></svg>`;
}

// ---- view state -------------------------------------------------------------

let lastResult = null; // most recent AgentsResult
let openAgentId = null; // id of the agent in the detail view, or null
let openAgentSource = null; // source machine of the open agent (federated disambiguation)
let scope = "local"; // "local" | "federated"

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

function buildAgentRow(a) {
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

  const brand = agentBrand(a.id);
  const touch = a.parse_error ? "" : humanize(a.last_updated);

  row.innerHTML = `
    <span class="agent-logo-wrap">
      ${logoMarkup(brand.d, "agent-logo--row", brand.hex)}
      <span class="agent-pulse ${pulseClass(a)}"></span>
    </span>
    <span class="agent-main">
      <div class="agent-id">${esc(brand.name)}</div>
      <div class="agent-sub">${esc(sub)}</div>
    </span>
    <span class="agent-touch">${esc(touch)}</span>
    <span class="chev">›</span>`;

  const open = () => openDetail(a.id, a.source || null);
  row.addEventListener("click", open);
  row.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") open();
  });
  return row;
}

// Flat list (This machine scope).
function renderOverview(agents) {
  el("agent-count").textContent = String(agents.length);
  const root = el("agent-list");
  root.innerHTML = "";
  for (const a of agents) root.appendChild(buildAgentRow(a));
}

// Grouped-by-machine list (Federated scope).
function renderGrouped(agents) {
  el("agent-count").textContent = String(agents.length);
  const root = el("agent-list");
  root.innerHTML = "";

  const bySource = {};
  for (const a of agents) {
    const key = a.source || "?";
    (bySource[key] = bySource[key] || []).push(a);
  }

  // Source order: local first, then peers (as reported); synthesize any source
  // that has agents but wasn't in the sources summary.
  const sources = (lastResult && lastResult.report && lastResult.report.sources) || [];
  const ordered = [...sources].sort(
    (x, y) => (x.kind === "local" ? 0 : 1) - (y.kind === "local" ? 0 : 1)
  );
  const seen = new Set(ordered.map((s) => s.name));
  for (const name of Object.keys(bySource)) {
    if (!seen.has(name)) {
      ordered.push({ name, kind: "peer", reachable: true, agent_count: bySource[name].length });
    }
  }

  for (const s of ordered) {
    const list = bySource[s.name] || [];
    const count = s.agent_count != null ? s.agent_count : list.length;
    const dot = s.kind === "local" ? "ok" : s.reachable ? "ok" : "down";
    const meta =
      s.kind === "local"
        ? "this machine"
        : s.reachable
          ? "peer"
          : "peer · unreachable";

    const header = document.createElement("div");
    header.className = "machine-head";
    header.innerHTML = `
      <span class="machine-dot ${dot}"></span>
      <span class="machine-name">${esc(s.name)}</span>
      <span class="machine-meta">${meta} · ${count} agent${count === 1 ? "" : "s"}</span>`;
    root.appendChild(header);

    if (!list.length) {
      const note = document.createElement("div");
      note.className = "machine-empty muted small";
      note.textContent = s.reachable
        ? "No agents published."
        : "Unreachable — is this peer's Bourdon server running the latest build?";
      root.appendChild(note);
      continue;
    }
    for (const a of list) root.appendChild(buildAgentRow(a));
  }
}

function renderListForScope(agents) {
  if (scope === "federated") renderGrouped(agents);
  else renderOverview(agents);
}

// ---- detail (deep report) ---------------------------------------------------

function openDetail(id, source) {
  openAgentId = id;
  openAgentSource = source || null;
  renderDetail();
  if (!el("state-detail").classList.contains("hidden")) return;
  showOnly("state-detail");
}

// Match the open agent by (id, source); fall back to id alone (local scope).
function findOpenAgent(agents) {
  return (
    agents.find(
      (x) => x.id === openAgentId && (x.source || null) === openAgentSource
    ) || agents.find((x) => x.id === openAgentId)
  );
}

function renderDetail() {
  const agents = (lastResult && lastResult.report && lastResult.report.agents) || [];
  const a = findOpenAgent(agents);
  const body = el("detail-body");
  if (!a) {
    // Agent vanished between refreshes — fall back to the overview.
    showOverview();
    return;
  }

  if (a.parse_error) {
    body.innerHTML = `
      <div class="detail-head">
        ${logoMarkup(agentBrand(a.id).d, "agent-logo--detail", agentBrand(a.id).hex)}
        <div>
          <div class="detail-id">${esc(agentBrand(a.id).name)}</div>
          <div class="detail-type">manifest error</div>
        </div>
      </div>
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

  const brand = agentBrand(a.id);
  const srcTag =
    scope === "federated" && a.source
      ? ` · <span class="src-tag">${esc(a.source)}</span>`
      : "";
  body.innerHTML = `
    <div class="detail-head">
      ${logoMarkup(brand.d, "agent-logo--detail", brand.hex)}
      <div>
        <div class="detail-id">${esc(brand.name)}</div>
        <div class="detail-type">${esc(a.type || "agent")}${
    a.instance ? " · " + esc(a.instance) : ""
  }${srcTag}</div>
      </div>
    </div>
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
  openAgentSource = null;
  const agents = (lastResult && lastResult.report && lastResult.report.agents) || [];
  renderListForScope(agents);
  showOnly("state-overview");
}

// ---- main render dispatch ---------------------------------------------------

function render(result) {
  lastResult = result;
  setHealth(result.health);
  updateScopeStatus(result);

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
  if (openAgentId && findOpenAgent(agents)) {
    renderListForScope(agents); // keep the back-target fresh
    renderDetail();
    showOnly("state-detail");
  } else {
    showOverview();
  }
}

// ---- load + wiring ----------------------------------------------------------

let inFlight = false;

function setScopeStatus(text) {
  el("scope-status").textContent = text || "";
}

// Footer status: in federated scope, summarize peer reachability.
function updateScopeStatus(result) {
  if (scope !== "federated") {
    setScopeStatus("");
    return;
  }
  const sources = (result && result.report && result.report.sources) || [];
  const peers = sources.filter((s) => s.kind === "peer");
  if (!peers.length) {
    setScopeStatus("no peers configured");
    return;
  }
  const up = peers.filter((s) => s.reachable).length;
  setScopeStatus(`${up}/${peers.length} peer${peers.length === 1 ? "" : "s"} online`);
}

async function load() {
  if (inFlight) return;
  inFlight = true;
  const btn = el("refresh-btn");
  btn.disabled = true;
  if (scope === "federated") setScopeStatus("syncing peers…");
  try {
    const cmd = scope === "federated" ? "get_agents_federated" : "get_agents";
    const result = await invoke(cmd);
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

function setScope(next) {
  if (scope === next || inFlight) return;
  scope = next;
  el("scope-local").classList.toggle("active", scope === "local");
  el("scope-federated").classList.toggle("active", scope === "federated");
  openAgentId = null;
  openAgentSource = null;
  load();
}

el("refresh-btn").addEventListener("click", load);
el("back-btn").addEventListener("click", showOverview);
el("scope-local").addEventListener("click", () => setScope("local"));
el("scope-federated").addEventListener("click", () => setScope("federated"));

// Tray "Refresh" pushes a LOCAL payload. In federated scope, ignore it and
// re-fetch federated instead so the view doesn't flip to local data.
listen("agents-refreshed", (event) => {
  if (scope === "federated") {
    load();
    return;
  }
  if (event.payload) render(event.payload);
});

// Re-read when the window regains focus (covers tray "Open Bourdon" + reshow).
window.addEventListener("focus", load);

load();
