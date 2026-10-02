import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { activityLine, KINDS } from "./activity.ts";

const BARE = { tool: null, dry_run: null, reason: null };

describe("the Activity page (M17, D7)", () => {
  it("knows every kind the audit log may hold", () => {
    // `app/policy/audit.py`'s `Kind`: a kind added there must be given words here.
    const audit = readFileSync(new URL("../../../app/policy/audit.py", import.meta.url), "utf8");
    const block = audit.match(/Kind = Literal\[([\s\S]*?)\]/)?.[1] ?? "";
    const kinds = [...block.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]);

    assert.ok(kinds.length > 0);
    assert.deepEqual([...KINDS].sort(), kinds.sort());
  });

  it("renders every kind in words of its own", () => {
    const lines = KINDS.map((kind) => activityLine({ ...BARE, kind, tool: "calendar.create_hold" }).what);

    for (const [index, what] of lines.entries()) assert.notEqual(what, KINDS[index]);
    assert.equal(new Set(lines).size, KINDS.length);
  });

  it("shows a kind it does not yet know as it is, rather than failing", () => {
    assert.equal(activityLine({ ...BARE, kind: "something_new" }).what, "something_new");
  });

  it("names the tool and gives the fixed reason", () => {
    const line = activityLine({
      kind: "action_refused",
      tool: "calendar.create_invite",
      dry_run: null,
      reason: "guests outside the thread",
    });
    assert.equal(line.what, "Refused a calendar invite: guests outside the thread");
    assert.equal(line.tone, "warn");
  });

  it("says when an action ran as a dry run", () => {
    const line = activityLine({ ...BARE, kind: "action_executed", tool: "calendar.create_hold", dry_run: true });
    assert.equal(line.what, "Ran a calendar hold as a dry run");
  });

  it("falls back to words for a tool it does not know", () => {
    assert.equal(activityLine({ ...BARE, kind: "action_approved", tool: "mail.send" }).what, "Approved an action");
  });
});
