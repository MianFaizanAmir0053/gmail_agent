import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { MAX_CORRECTION_CHARS, parseDecisionForm, describeAnswer } from "./decisionForm.ts";

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return data;
}

const CARD = { message_id: "18c0f2a3b4c5d6e7", revision: "2" };

describe("parseDecisionForm", () => {
  it("reads a confirm or a cancel on the card's revision", () => {
    assert.deepEqual(parseDecisionForm(form({ ...CARD, action: "confirm" })), {
      ok: true,
      value: { message_id: CARD.message_id, revision: 2, action: "confirm", correction: "" },
    });
    assert.equal(parseDecisionForm(form({ ...CARD, action: "cancel" })).ok, true);
  });

  it("reads an edit with its correction, trimmed", () => {
    const parsed = parseDecisionForm(form({ ...CARD, action: "edit", correction: "  4pm not 3pm " }));
    assert.deepEqual(parsed, {
      ok: true,
      value: { message_id: CARD.message_id, revision: 2, action: "edit", correction: "4pm not 3pm" },
    });
  });

  it("refuses an edit with nothing to change", () => {
    // The graph would route it to reject and log it as a decline.
    assert.equal(parseDecisionForm(form({ ...CARD, action: "edit", correction: "  " })).ok, false);
  });

  it("refuses what the web app never sends", () => {
    for (const fields of [
      { ...CARD, action: "sweep" },
      { ...CARD, action: "approve" },
      { ...CARD, action: "confirm", revision: "0" },
      { ...CARD, action: "confirm", revision: "two" },
      { message_id: "", revision: "1", action: "confirm" },
      { ...CARD, action: "edit", correction: "x".repeat(MAX_CORRECTION_CHARS + 1) },
    ]) {
      assert.equal(parseDecisionForm(form(fields)).ok, false, JSON.stringify(fields).slice(0, 80));
    }
  });
});

describe("describeAnswer", () => {
  it("tells the owner what happened, in words", () => {
    assert.equal(describeAnswer({ status: "queued" }).tone, "ok");
    assert.match(describeAnswer({ status: "stale", current_revision: 3 }).message, /changed/);
    assert.match(describeAnswer({ status: "not_found" }).message, /no longer waiting/);
    assert.equal(describeAnswer({ status: "invalid", detail: "no edits left" }).message, "no edits left");
  });

  it("never shows a raw server error", () => {
    const answer = describeAnswer({ status: "error" });
    assert.equal(answer.tone, "error");
    assert.doesNotMatch(answer.message, /stack|Traceback|psycopg/);
  });
});
