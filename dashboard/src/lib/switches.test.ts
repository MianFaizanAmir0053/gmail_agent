import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { banners, heldNote, NO_SWITCHES, readSwitches } from "./switches.ts";

describe("readSwitches", () => {
  it("reads the control row", () => {
    assert.deepEqual(readSwitches({ paused: true, budget_state: "warning" }), {
      paused: true,
      budgetState: "warning",
    });
  });

  it("says nothing for a row it cannot read", () => {
    assert.deepEqual(readSwitches(undefined), NO_SWITCHES);
    assert.deepEqual(readSwitches({ paused: "yes", budget_state: "broke" }), NO_SWITCHES);
  });
});

describe("the header's banners (M17, D5 and D6)", () => {
  it("shows none while all is well", () => {
    assert.deepEqual(banners(NO_SWITCHES), []);
  });

  it("says the agent is paused, and that Withdraw still works", () => {
    const [banner] = banners({ paused: true, budgetState: "ok" });
    assert.equal(banner?.tone, "warn");
    assert.match(banner?.text ?? "", /^Paused/);
    assert.match(banner?.text ?? "", /Withdraw still works/);
  });

  it("says the cap is reached, the most pressing after a pause", () => {
    const shown = banners({ paused: true, budgetState: "exhausted" });
    assert.equal(shown.length, 2);
    assert.equal(shown[1]?.tone, "error");
    assert.match(shown[1]?.text ?? "", /^Spending cap reached/);
  });

  it("warns at 80%, with no amount", () => {
    const [banner] = banners({ paused: false, budgetState: "warning" });
    assert.equal(banner?.text, "Model spending is at 80% of this month's cap.");
    assert.doesNotMatch(banner?.text ?? "", /\$/);
  });
});

describe("why a queued decision waits", () => {
  it("names a pause for any decision", () => {
    for (const action of ["confirm", "edit", "cancel"]) {
      assert.match(heldNote(action, { paused: true, budgetState: "ok" }) ?? "", /^Paused/);
    }
  });

  it("holds only an Edit at the cap: a Confirm or a Cancel calls no model", () => {
    const capped = { paused: false, budgetState: "exhausted" } as const;
    assert.match(heldNote("edit", capped) ?? "", /spending cap/);
    assert.equal(heldNote("confirm", capped), null);
    assert.equal(heldNote("cancel", capped), null);
  });

  it("says nothing when nothing holds it", () => {
    assert.equal(heldNote("edit", NO_SWITCHES), null);
    assert.equal(heldNote(null, { paused: true, budgetState: "ok" }), null);
  });
});
