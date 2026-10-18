import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { perHundredEmails } from "./costs.ts";

// What the totals' SQL counts is tested on Postgres, from this file's own
// query: tests/test_dashboard_costs.py.
describe("the cost per hundred emails", () => {
  it("divides the mail runs' cost by the emails they ran for", () => {
    assert.equal(perHundredEmails(0.5, 50), 1);
    assert.equal(perHundredEmails(0, 0), null);
  });
});
