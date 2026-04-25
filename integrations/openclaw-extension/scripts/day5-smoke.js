#!/usr/bin/env node
/*
 * Day 5 Sovereign Memory smoke test.
 * Verifies OpenClaw plugin bridge -> Unix socket -> sovrd -> Sovereign DB.
 */

const {
  health,
  identity,
  full,
  learn,
  recall,
  getMemorySearchManager,
} = require("../dist/index.js");

async function main() {
  const marker = `DAY5-SMOKE-${Date.now()}`;
  const checks = [];

  const h = await health();
  checks.push(["health", h.status === "ok", h]);

  const agentIdentity = await identity("agent-a");
  checks.push(["identity endpoint", Object.prototype.hasOwnProperty.call(agentIdentity, "identity"), agentIdentity]);

  const agentFull = await full("agent-a");
  checks.push(["full endpoint", typeof agentFull.context === "string" && agentFull.context.length > 0, { length: String(agentFull.context || "").length }]);

  const firstLearn = await learn(marker, "fact", "agent-a", undefined, "workspace-agent-a");
  checks.push(["learn", firstLearn.status === "learned", firstLearn]);

  const duplicateLearn = await learn(marker, "fact", "agent-a", undefined, "workspace-agent-a");
  checks.push(["duplicate learn", duplicateLearn.status === "duplicate", duplicateLearn]);

  const agentRecall = await recall(marker, "agent-a", undefined, "workspace-agent-a", 5);
  checks.push(["agent exact recall", JSON.stringify(agentRecall).includes(marker), agentRecall]);

  const sharedKnowledgeRecall = await recall(marker, "agent-b", "knowledge", "workspace-agent-b", 5);
  checks.push(["cross-agent knowledge recall", JSON.stringify(sharedKnowledgeRecall).includes(marker), sharedKnowledgeRecall]);

  const manager = getMemorySearchManager("agent-a", "workspace-agent-a");
  const vectorAvailable = await manager.probeVectorAvailability();
  checks.push(["manager vector probe", vectorAvailable === true, vectorAvailable]);

  const managerResults = await manager.search(marker, { maxResults: 5, minScore: 0 });
  checks.push(["manager search exact recall", JSON.stringify(managerResults).includes(marker), managerResults[0] || null]);

  const failed = checks.filter(([, ok]) => !ok);
  for (const [name, ok, detail] of checks) {
    console.log(`${ok ? "PASS" : "FAIL"} ${name}: ${JSON.stringify(detail).slice(0, 500)}`);
  }
  if (failed.length) {
    console.error(`Day 5 smoke failed: ${failed.map(([name]) => name).join(", ")}`);
    process.exit(1);
  }
  console.log(`DAY5_SMOKE_OK marker=${marker}`);
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
