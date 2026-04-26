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
      wikilink: "[[wiki/sessions/20260425-gemini_cli-gemini-cli-plugin-backend-handoff-clean]]",
      relativePath: "wiki/sessions/20260425-gemini_cli-gemini-cli-plugin-backend-handoff-clean.md",
      snippet: "Frontend/dashboard work should wait until the plugin backend stabilizes further.",
      score: 84,
      authority: "handoff",
      freshness: "fresh",
      privacyLevel: "safe",
      reasons: ["lexical match", "fresh handoff", "fresh note"],
    },
  ],
  recall: { daemonOk: true },
  afm: { requested: true, used: true, url: "http://127.0.0.1:11437/v1/chat/completions" },
  contextMarkdown: "# Gemini CLI Task Packet\n\nSample packet.",
};

const sampleOutcome = {
  task: "Ship frontend console",
  summary: "Added a local packet console backed by DESIGN.md tokens.",
  profile: "compact",
  mode: "deterministic",
  changedFiles: ["plugins/gemini-cli/frontend/app.js"],
  verification: ["npm test passed", "DESIGN.md lint passed"],
  outcomeDraft: {
    learnCandidates: ["Gemini CLI Memory frontend should mirror DESIGN.md tokens and avoid automatic learning."],
    logOnly: ["npm test passed", "DESIGN.md lint passed"],
    expires: ["Refresh UI screenshots after the next frontend pass."],
    doNotStore: ["Do not store raw logs, vault raw material, DBs, or adapter paths."],
  },
  afm: { requested: false, used: false },
  contextMarkdown: "# Gemini CLI Outcome Packet\n\nSample outcome.",
};

let bridgeOnline = false;

function $(selector) {
  return document.querySelector(selector);
}

function setJson(target, value) {
  target.value = JSON.stringify(value, null, 2);
}

function parseLines(value) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function parseJson(textarea) {
  try {
    return JSON.parse(textarea.value);
  } catch (error) {
    alert(`Invalid JSON: ${error.message}`);
    return null;
  }
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error ?? `HTTP ${response.status}`);
  return payload;
}

function setBridgeState(online, label) {
  bridgeOnline = online;
  $("#bridgeState").textContent = online ? "live bridge" : "static fallback";
  $("#bridgeState").className = online ? "badge safe" : "badge warn";
  $("#railStatusText").textContent = label;
}

function renderStatus(status) {
  $("#socketState").textContent = status.socket?.ok ? "socket ok" : "socket offline";
  $("#afmState").textContent = status.afm?.ok ? "AFM-ready" : "AFM offline";
  $("#auditCount").textContent = String(status.audit?.entries ?? 0);
  $("#vaultPath").textContent = status.vault?.path ?? "unknown vault";
}

async function refreshStatus() {
  try {
    await getJson("/api/health");
    const status = await getJson("/api/status");
    setBridgeState(true, "Local bridge is connected. No automatic learning from this UI.");
    renderStatus(status);
  } catch {
    setBridgeState(false, "Open through `npm run console` to generate live packets.");
    renderStatus({ socket: { ok: false }, afm: { ok: false }, audit: { entries: 0 }, vault: { path: "static file mode" } });
  }
}

async function refreshAudit() {
  if (!bridgeOnline) {
    $("#auditText").textContent = "Audit tail is available when the local bridge is running.";
    return;
  }
  try {
    const tail = await getJson("/api/audit-tail?limit=20");
    $("#auditText").textContent = tail.text || "No audit entries yet.";
  } catch (error) {
    $("#auditText").textContent = error.message;
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
  $("#contextMarkdown").value = packet.contextMarkdown ?? "";

  const sources = packet.relevantSources ?? [];
  const safeCount = sources.filter((source) => source.privacyLevel === "safe").length;
  $("#safeSourceSummary").textContent = `${safeCount} safe`;
  $("#sourceTable").innerHTML =
    sources.length === 0
      ? `<div class="source-row empty"><strong>No sources</strong><small>Generate or paste a packet with relevantSources.</small></div>`
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
  $("#outcomeMode").textContent = packet.mode ?? "deterministic";
  $("#learnCandidates").innerHTML = listItems(draft.learnCandidates);
  $("#logOnly").innerHTML = listItems(draft.logOnly);
  $("#expires").innerHTML = listItems(draft.expires);
  $("#doNotStore").innerHTML = listItems(draft.doNotStore);
  $("#outcomeMarkdown").value = packet.contextMarkdown ?? "";
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

function preparePayload() {
  return {
    task: $("#taskInput").value,
    profile: $("#profileInput").value,
    budgetTokens: Number($("#budgetInput").value) || undefined,
    limit: Number($("#limitInput").value) || undefined,
    useAfm: $("#useAfmInput").checked,
    includeVault: $("#includeVaultInput").checked,
  };
}

function outcomePayload() {
  return {
    task: $("#outcomeTaskInput").value,
    summary: $("#summaryInput").value,
    profile: $("#outcomeProfileInput").value,
    useAfm: $("#outcomeUseAfmInput").checked,
    changedFiles: parseLines($("#changedFilesInput").value),
    verification: parseLines($("#verificationInput").value),
  };
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
    button.classList.add("active");
    $(`#${button.dataset.view}-view`).classList.add("active");
    if (button.dataset.view === "audit") void refreshAudit();
  });
});

$("#generatePrepare").addEventListener("click", async () => {
  try {
    $("#generatePrepare").disabled = true;
    const packet = await postJson("/api/prepare-task", preparePayload());
    setJson($("#packetInput"), packet);
    renderPrepare(packet);
    await refreshStatus();
  } catch (error) {
    alert(`Prepare failed: ${error.message}`);
  } finally {
    $("#generatePrepare").disabled = false;
  }
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
  $("#contextMarkdown").value = "";
});

$("#generateOutcome").addEventListener("click", async () => {
  try {
    $("#generateOutcome").disabled = true;
    const packet = await postJson("/api/prepare-outcome", outcomePayload());
    setJson($("#outcomeInput"), packet);
    renderOutcome(packet);
  } catch (error) {
    alert(`Outcome failed: ${error.message}`);
  } finally {
    $("#generateOutcome").disabled = false;
  }
});

$("#loadOutcome").addEventListener("click", () => {
  setJson($("#outcomeInput"), sampleOutcome);
  renderOutcome(sampleOutcome);
});

$("#analyzeOutcome").addEventListener("click", () => {
  const packet = parseJson($("#outcomeInput"));
  if (packet) renderOutcome(packet);
});

$("#refreshStatus").addEventListener("click", refreshStatus);
$("#refreshAudit").addEventListener("click", refreshAudit);

setJson($("#packetInput"), samplePrepare);
setJson($("#outcomeInput"), sampleOutcome);
renderPrepare(samplePrepare);
renderOutcome(sampleOutcome);
renderTokens();
await refreshStatus();
