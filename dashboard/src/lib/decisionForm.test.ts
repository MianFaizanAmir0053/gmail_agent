import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  MAX_CORRECTION_CHARS,
  describeAllow,
  describeAnswer,
  describeSwitch,
  describeWithdraw,
  parseAllowForm,
  parseDecisionForm,
  parseSwitchForm,
  parseWithdrawForm,
} from "./decisionForm.ts";

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return data;
}

const CARD = { message_id: "18c0f2a3b4c5d6e7", revision: "2" };
const TOKEN = "3f9a1c07be42-dry-1";

describe("parseDecisionForm", () => {
  it("reads a confirm with the card's token, or a cancel, on the card's revision", () => {
    assert.deepEqual(parseDecisionForm(form({ ...CARD, action: "confirm", token: TOKEN })), {
      ok: true,
      value: {
        message_id: CARD.message_id,
        revision: 2,
        action: "confirm",
        correction: "",
        token: TOKEN,
      },
    });
    assert.equal(parseDecisionForm(form({ ...CARD, action: "cancel" })).ok, true);
  });

  it("refuses a confirm that carries no token, or a mangled one", () => {
    // It would bind nothing (M17, D2): Fly refuses it, so this spares the round trip.
    for (const token of [undefined, "", "3f9a1c07be42-dry", "3F9A1C07BE42-dry-1", "x-live-1"]) {
      const fields: Record<string, string> = { ...CARD, action: "confirm" };
      if (token !== undefined) fields.token = token;
      assert.equal(parseDecisionForm(form(fields)).ok, false, String(token));
    }
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
    assert.match(describeAnswer({ status: "not_ready" }).message, /being prepared/);
  });

  it("never shows a raw server error", () => {
    const answer = describeAnswer({ status: "error" });
    assert.equal(answer.tone, "error");
    assert.doesNotMatch(answer.message, /stack|Traceback|psycopg/);
  });
});


describe("an Allow (M17, D4)", () => {
  const form = (fields: Record<string, string>) => {
    const data = new FormData();
    for (const [name, value] of Object.entries(fields)) data.set(name, value);
    return data;
  };

  it("carries one address and the proposal it came from", () => {
    assert.deepEqual(parseAllowForm(form({ address: " sara@example.com ", message_id: "m1" })), {
      ok: true,
      value: { address: "sara@example.com", message_id: "m1" },
    });
  });

  it("refuses anything that is not one address", () => {
    const parsed = parseAllowForm(form({ address: "a@b.com, c@d.com", message_id: "m1" }));
    assert.equal(parsed.ok, false);
  });

  it("says what happened in words", () => {
    assert.equal(describeAllow("allowed", "sara@example.com").tone, "ok");
    assert.equal(describeAllow("error", "sara@example.com").tone, "error");
  });

  it("explains Fly's refusal of a Confirm with a guest outside", () => {
    assert.match(describeAnswer({ status: "outside" }).message, /not in this email thread/);
  });
});

describe("Withdraw, and the switches (M17, D6)", () => {
  it("names a decision by its id", () => {
    assert.deepEqual(parseWithdrawForm(form({ decision_id: "41" })), {
      ok: true,
      value: { decision_id: 41 },
    });
  });

  it("refuses anything that is not a decision's id", () => {
    for (const id of ["", "0", "-4", "4.5", "41; drop", "1".repeat(16)]) {
      assert.equal(parseWithdrawForm(form({ decision_id: id })).ok, false, id);
    }
  });

  it("says what became of a withdraw, in words", () => {
    assert.equal(describeWithdraw("requested").message, "Withdrawing…");
    // Settled can mean applied, withdrawn, returned or expired: the words
    // claim none of them.
    assert.equal(describeWithdraw("settled").message, "That decision is no longer queued.");
    assert.equal(describeWithdraw("error").tone, "error");
  });

  it("knows only Pause and Resume", () => {
    assert.deepEqual(parseSwitchForm(form({ kind: "pause" })), { ok: true, value: "pause" });
    assert.deepEqual(parseSwitchForm(form({ kind: "resume" })), { ok: true, value: "resume" });
    assert.equal(parseSwitchForm(form({ kind: "delete" })).ok, false);
  });

  it("says when a switch could not be reached", () => {
    assert.equal(describeSwitch("pause", "done").tone, "ok");
    assert.match(describeSwitch("resume", "error").message, /could not be resumed/);
  });
});
