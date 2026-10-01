import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  cardToken,
  cardView,
  formatWindow,
  layoutKey,
  ownerZone,
  shouldRefresh,
  type CardPayload,
  type ProposalRow,
} from "./timeline.ts";

const ROW: ProposalRow = {
  message_id: "18c0f2a3b4c5d6e7",
  revision: 1,
  status: "pending",
  final_status: null,
  action_type: "calendar_invite",
  dry_run: true,
  args_hash: "3f9a1c07be42" + "0".repeat(52),
  generation: 1,
  payload: {
    title: "Design review",
    start_utc: "2026-10-02T11:00:00Z",
    end_utc: "2026-10-02T12:00:00Z",
    timezone: "Europe/London",
    attendees: ["sara@example.com"],
    location: "Room 4B",
    conflicts: ["Overlaps Standup"],
    review_issues: ["the stated zone was ignored"],
  },
  parked_at: "2026-10-01T09:00:00Z",
};

describe("formatWindow", () => {
  it("shows the time in the owner's zone, never UTC", () => {
    // Approving 11:00 when you meant 16:00 is the failure this card exists to stop.
    assert.equal(
      formatWindow("2026-10-02T11:00:00Z", "2026-10-02T12:00:00Z", "Asia/Karachi"),
      "Fri 02 Oct, 16:00 – 17:00",
    );
  });

  it("names the end's day when the event crosses midnight", () => {
    assert.equal(
      formatWindow("2026-10-02T18:30:00Z", "2026-10-02T19:30:00Z", "Asia/Karachi"),
      "Fri 02 Oct, 23:30 – Sat 03 Oct, 00:30",
    );
  });

  it("says so when the time is unknown", () => {
    assert.equal(formatWindow(null, null, "UTC"), "time unknown");
  });

  it("treats a time it cannot read as unknown, rather than failing the page", () => {
    assert.equal(formatWindow("next Tuesday", null, "UTC"), "time unknown");
    assert.equal(formatWindow("2026-10-02T11:00:00Z", "later", "UTC"), "Fri 02 Oct, 11:00");
  });
});

describe("cardView", () => {
  it("lets the owner decide and edit a pending proposal", () => {
    const view = cardView(ROW, "Asia/Karachi");
    assert.equal(view.canDecide, true);
    assert.equal(view.canEdit, true);
    assert.equal(view.applying, false);
    assert.equal(view.when, "Fri 02 Oct, 16:00 – 17:00");
  });

  it("hides Edit at revision 3, where the graph would refuse a third edit", () => {
    const view = cardView({ ...ROW, revision: 3 }, "UTC");
    assert.equal(view.canDecide, true);
    assert.equal(view.canEdit, false);
  });

  it("shows an open decision as applying, with nothing to tap", () => {
    const view = cardView({ ...ROW, status: "deciding" }, "UTC");
    assert.equal(view.applying, true);
    assert.equal(view.canDecide, false);
    assert.equal(view.canEdit, false);
  });

  it("carries the dry run, the guests, the conflicts and the reviewer's issues", () => {
    const view = cardView(ROW, "UTC");
    assert.equal(view.dryRun, true);
    assert.equal(view.invite, true);
    assert.deepEqual(view.attendees, ["sara@example.com"]);
    assert.deepEqual(view.conflicts, ["Overlaps Standup"]);
    assert.deepEqual(view.reviewIssues, ["the stated zone was ignored"]);
    assert.equal(view.location, "Room 4B");
  });

  it("survives a payload cleared by retention", () => {
    const view = cardView({ ...ROW, status: "decided", payload: null }, "UTC");
    assert.equal(view.title, "(cleared)");
    assert.equal(view.when, "time unknown");
    assert.deepEqual(view.attendees, []);
  });

  it("survives a missing title", () => {
    const view = cardView({ ...ROW, payload: { ...ROW.payload, title: null } }, "UTC");
    assert.equal(view.title, "(untitled)");
  });

  it("keeps only the words from lists that are not lists of words", () => {
    // One odd row must not take the whole timeline down with it.
    const payload = {
      ...ROW.payload,
      attendees: "sara@example.com",
      conflicts: [42, "Overlaps Standup"],
      review_issues: { issue: "x" },
      title: 7,
    } as unknown as CardPayload;
    const view = cardView({ ...ROW, payload }, "UTC");
    assert.deepEqual(view.attendees, []);
    assert.deepEqual(view.conflicts, ["Overlaps Standup"]);
    assert.deepEqual(view.reviewIssues, []);
    assert.equal(view.title, "(untitled)");
  });

  it("offers no decision on a proposal whose content is gone", () => {
    // The owner would be approving something they cannot see.
    const view = cardView({ ...ROW, payload: null }, "UTC");
    assert.equal(view.canDecide, false);
    assert.equal(view.canEdit, false);
  });
});

describe("cardToken", () => {
  it("is the token app/channel/decide.py checks: hash prefix, mode, generation", () => {
    // The same example is pinned in tests/test_decide.py.
    assert.equal(cardToken("3f9a1c07be42" + "0".repeat(52), false, 2), "3f9a1c07be42-live-2");
    assert.equal(cardToken("a".repeat(64), true, 1), "aaaaaaaaaaaa-dry-1");
  });
});

describe("cardView's binding", () => {
  it("carries the token, and offers Confirm only with one", () => {
    const view = cardView(ROW, "UTC");
    assert.equal(view.token, "3f9a1c07be42-dry-1");
    assert.equal(view.canConfirm, true);

    const unbound = cardView({ ...ROW, args_hash: null }, "UTC");
    assert.equal(unbound.token, null);
    assert.equal(unbound.canConfirm, false);
    assert.equal(unbound.canDecide, true); // Edit and Cancel still work
  });

  it("names the mode the proposal runs under", () => {
    assert.equal(cardView(ROW, "UTC").live, false);
    assert.equal(cardView({ ...ROW, dry_run: false }, "UTC").live, true);
  });
});

describe("layoutKey", () => {
  const A: ProposalRow = { ...ROW, message_id: "a" };
  const B: ProposalRow = { ...ROW, message_id: "b" };

  it("stays the same while no card on the list moves", () => {
    assert.equal(layoutKey([A, B]), layoutKey([{ ...A }, { ...B }]));
  });

  it("changes when a card appears, leaves, moves, gains a revision or starts applying", () => {
    // Each of these can move a button under the owner's thumb.
    const before = layoutKey([A, B]);
    const after: ProposalRow[][] = [
      [A],
      [{ ...ROW, message_id: "c" }, A, B],
      [B, A],
      [{ ...A, revision: 2 }, B],
      [{ ...A, status: "deciding" }, B],
    ];
    for (const rows of after) {
      assert.notEqual(layoutKey(rows), before);
    }
  });
});

describe("ownerZone", () => {
  it("uses the configured zone, and UTC when it is missing or not a zone", () => {
    // A typo in OWNER_TIMEZONE must not take the timeline down with a RangeError.
    assert.equal(ownerZone("Asia/Karachi"), "Asia/Karachi");
    assert.equal(ownerZone(undefined), "UTC");
    assert.equal(ownerZone(""), "UTC");
    assert.equal(ownerZone("Mars/Olympus_Mons"), "UTC");
  });
});

describe("shouldRefresh", () => {
  it("re-reads only while a decision is open", () => {
    assert.equal(shouldRefresh([{ status: "pending" }, { status: "decided" }]), false);
    assert.equal(shouldRefresh([{ status: "pending" }, { status: "deciding" }]), true);
    assert.equal(shouldRefresh([]), false);
  });
});
