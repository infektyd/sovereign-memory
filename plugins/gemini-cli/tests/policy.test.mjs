import assert from "node:assert/strict";
import test from "node:test";

import { assessLearningQuality, routeMemoryIntent } from "../dist/policy.js";

test("routeMemoryIntent allows automatic recall but not learning", () => {
  const recall = routeMemoryIntent("continue testing the Gemini CLI Memory plugin from prior context");
  assert.equal(recall.action, "recall");
  assert.equal(recall.automaticAllowed, true);
  assert.equal(recall.suggestedTool, "gemini_cli_recall");

  const learn = routeMemoryIntent("remember this decision in Gemini CLI Memory");
  assert.equal(learn.action, "learn");
  assert.equal(learn.automaticAllowed, false);
  assert.equal(learn.suggestedTool, "gemini_cli_learn");
});

test("assessLearningQuality rewards sourced durable notes and warns on weak notes", () => {
  const good = assessLearningQuality({
    title: "Gemini CLI recall ranking decision",
    content: "Gemini CLI recall should show vault context packs before broad daemon semantic results.",
    category: "decision",
    source: "unit-test",
  });
  assert.equal(good.ok, true);
  assert.equal(good.warnings.length, 0);

  const weak = assessLearningQuality({
    title: "todo",
    content: "maybe store the thing later",
  });
  assert.equal(weak.ok, false);
  assert.match(weak.summary, /Title is very short/);
  assert.match(weak.summary, /vague wording/);
});
