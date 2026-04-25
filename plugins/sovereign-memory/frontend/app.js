const designTokens = {
  primary: "#151819",
  secondary: "#4F5A57",
  tertiary: "#2F7D68",
  accent: "#A95533",
  neutral: "#F4F1EA",
  surface: "#FFFFFF",
  success: "#2F7D68",
  warning: "#B9852A",
  danger: "#A4483F",
};

const samplePrepare = {
  task: "Inspect frontend dashboard readiness before agent work",
  budgetTokens: 4000,
  profile: "standard",
  budget: { tokens: 4000, sourceLimit: 6, afmSourceLimit: 4 },
  mode: "afm",
  constraints: [
    "Default automatic behavior is recall-only; durable learning and vault writes must stay explicit.",
    "Frontend/dashboard work should wait until the plugin backend behavior is stable and verified.",
  ],
  relevantSources: [
    {
      title: "Backend handoff clean",
      wikilink: "[[wiki/sessions/20260425-codex-sovereign-memory-plugin-backend-handoff-clean]]",
      relativePath: "wiki/sessions/20260425-codex-sovereign-memory-plugin-backend-handoff-clean.md",
      snippet: "Frontend/dashboard work should wait until the plugin backend stabilizes further.",
      score: 84,
      authority: "handoff",
      freshness: "fresh",
      privacyLevel: "safe",
      reasons: ["lexical match", "fresh handoff", "fresh note"],
    },
    {
      title: "AFM runtime note",
      wikilink: "[[wiki/sessions/local-afm-runtime]]",
      relativePath: "wiki/sessions/local-afm-runtime.md",
      snippet: "AFM bridge is local-only and should not receive private raw session material.",
      score: 42,
      authority: "session",
      freshness: "fresh",
      privacyLevel: "local-only",
      reasons: ["lexical match", "local-only source"],
    },
  ],
  afm: { requested: true, used: true, url: "http://127.0.0.1:11437/v1/chat/completions" },
};

const sampleOutcome = {
  task: "Ship frontend console",
  summary: "Added a static packet inspector backed by DESIGN.md tokens.",
  profile: "compact",
  mode: "deterministic",
  outcomeDraft: {
    learnCandidates: ["Sovereign Memory frontend should mirror DESIGN.md tokens and avoid automatic learning."],
    logOnly: ["npm test passed", "DESIGN.md lint passed"],
    expires: ["Refresh UI screenshots after the next frontend pass."],
    doNotStore: ["Do not store raw logs, vault raw material, DBs, or adapter paths."],
  },
  afm: { requested: false, used: false },
};

function $(selector) {
  return document.querySelector(selector);
}

function setJson(target, value) {
  target.value = JSON.stringify(value, null, 2);
}

function parseJson(textarea) {
  try {
    return JSON.parse(textarea.value);
  } catch (error) {
    alert(`Invalid JSON: ${error.message}`);
    return null;
  }
}

function renderPrepare(packet) {
  $("#profileState").textContent = packet.profile ?? "standard";
  $("#packetMode").textContent = packet.mode ?? "deterministic";
  $("#budgetTokens").textContent = Number(packet.budgetTokens ?? packet.budget?.tokens ?? 0).toLocaleString();
  $("#sourceCount").textContent = String(packet.relevantSources?.length ?? 0);
  $("#afmUsed").textContent = String(packet.afm?.used ?? false);
  const budget = Number(packet.budgetTokens ?? 4000);
  $("#budgetFill").style.width = `${Math.max(12, Math.min(100, (budget / 12000) * 100))}%`;
  $("#constraintsList").innerHTML = (packet.constraints ?? []).map((item) => `<div>${escapeHtml(item)}</div>`).join("");

  const sources = packet.relevantSources ?? [];
  const safeCount = sources.filter((source) => source.privacyLevel === "safe").length;
  $("#safeSourceSummary").textContent = `${safeCount} safe`;
  $("#sourceTable").innerHTML =
    sources.length === 0
      ? `<div class="source-row"><strong>No sources</strong><small>Paste a packet with relevantSources.</small></div>`
      : sources.map(renderSource).join("");
}

function renderSource(source) {
  const privacy = source.privacyLevel ?? "safe";
  return `
    <article class="source-row">
      <div>
        <strong>${escapeHtml(source.title ?? source.wikilink ?? "Untitled source")}</strong>
        <small>${escapeHtml(source.relativePath ?? "")}</small>
      </div>
      <span class="mono">${escapeHtml(source.authority ?? "vault")}</span>
      <span class="mono">${escapeHtml(source.freshness ?? "unknown")}</span>
      <span class="mono privacy-${escapeHtml(privacy)}">${escapeHtml(privacy)}</span>
      <small>${escapeHtml((source.reasons ?? ["included"]).join(", "))}</small>
    </article>
  `;
}

function renderOutcome(packet) {
  const draft = packet.outcomeDraft ?? {};
  $("#learnCandidates").innerHTML = listItems(draft.learnCandidates);
  $("#doNotStore").innerHTML = listItems(draft.doNotStore);
}

function renderTokens() {
  $("#tokenGrid").innerHTML = Object.entries(designTokens)
    .map(
      ([name, value]) => `
        <article class="token-card">
          <div class="swatch" style="background:${value}"></div>
          <strong>${name}</strong>
          <div class="mono">${value}</div>
        </article>
      `,
    )
    .join("");
}

function listItems(items = []) {
  return items.length === 0 ? "<li>None</li>" : items.map((item) => `<li>${escapeHtml(item)}</li>`).join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
    button.classList.add("active");
    $(`#${button.dataset.view}-view`).classList.add("active");
  });
});

$("#loadPrepare").addEventListener("click", () => {
  setJson($("#packetInput"), samplePrepare);
  renderPrepare(samplePrepare);
});

$("#analyzePrepare").addEventListener("click", () => {
  const packet = parseJson($("#packetInput"));
  if (packet) renderPrepare(packet);
});

$("#clearInput").addEventListener("click", () => {
  $("#packetInput").value = "";
});

$("#loadOutcome").addEventListener("click", () => {
  setJson($("#outcomeInput"), sampleOutcome);
  renderOutcome(sampleOutcome);
});

$("#analyzeOutcome").addEventListener("click", () => {
  const packet = parseJson($("#outcomeInput"));
  if (packet) renderOutcome(packet);
});

setJson($("#packetInput"), samplePrepare);
setJson($("#outcomeInput"), sampleOutcome);
renderPrepare(samplePrepare);
renderOutcome(sampleOutcome);
renderTokens();
