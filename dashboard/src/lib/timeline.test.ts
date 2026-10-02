import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  cardToken,
  cardView,
  footerKey,
  formatWindow,
  layoutKey,
  outsideGuestKeys,
  HELD_REFRESH_EVERY_MS,
  ownerZone,
  REFRESH_EVERY_MS,
  refreshEvery,
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

describe("refreshEvery", () => {
  const live = { paused: false, budgetState: "ok" } as const;

  it("re-reads only while a decision is open", () => {
    assert.equal(refreshEvery([{ status: "pending" }, { status: "decided" }], live), null);
    assert.equal(refreshEvery([], live), null);
    assert.equal(
      refreshEvery([{ status: "pending" }, { status: "deciding", decision_action: "confirm" }], live),
      REFRESH_EVERY_MS,
    );
  });

  it("slows down while every open decision is held (M17, D6)", () => {
    const paused = { paused: true, budgetState: "ok" } as const;
    assert.equal(
      refreshEvery([{ status: "deciding", decision_action: "confirm" }], paused),
      HELD_REFRESH_EVERY_MS,
    );
    const capped = { paused: false, budgetState: "exhausted" } as const;
    assert.equal(refreshEvery([{ status: "deciding", decision_action: "edit" }], capped), HELD_REFRESH_EVERY_MS);
    assert.equal(
      refreshEvery(
        [
          { status: "deciding", decision_action: "edit" },
          { status: "deciding", decision_action: "confirm" },
        ],
        capped,
      ),
      REFRESH_EVERY_MS,
    );
  });

  it("keeps up while a withdraw waits for its answer, held or not", () => {
    const paused = { paused: true, budgetState: "ok" } as const;
    assert.equal(
      refreshEvery([{ status: "deciding", decision_action: "confirm", withdraw_requested: true }], paused),
      REFRESH_EVERY_MS,
    );
  });
});


describe("layoutKey and outside guests", () => {
  it("changes when an Allow hides a guest, so the moved buttons are held", () => {
    assert.notEqual(layoutKey([{ ...ROW, outside: 2 }]), layoutKey([{ ...ROW, outside: 1 }]));
  });
});

describe("guests outside the thread (M17, D4)", () => {
  const row: ProposalRow = {
    ...ROW,
    payload: { ...ROW.payload, outside_guests: ["Sara.Khan@googlemail.com", "new@example.net"] },
  };

  it("lists each one the owner has not allowed", () => {
    assert.deepEqual(cardView(row, "UTC").outsideGuests, [
      "Sara.Khan@googlemail.com",
      "new@example.net",
    ]);
  });

  it("hides one already allowed, however the card spells it", () => {
    const view = cardView(row, "UTC", new Set(["sarakhan@gmail.com"]));
    assert.deepEqual(view.outsideGuests, ["new@example.net"]);
  });

  it("shows none for a card parked before the rule", () => {
    assert.deepEqual(cardView(ROW, "UTC").outsideGuests, []);
  });

  it("looks each guest up once, by the key Fly stores", () => {
    assert.deepEqual(outsideGuestKeys([row, row, ROW]), ["new@example.net", "sarakhan@gmail.com"]);
  });
});

describe("a queued decision and Withdraw (M17, D6)", () => {
  const queued: ProposalRow = {
    ...ROW,
    status: "deciding",
    decision_id: "41",
    decision_action: "confirm",
    withdraw_requested: false,
    withdraw_declined: false,
  };

  it("offers Withdraw on a queued decision nothing has been asked about", () => {
    const view = cardView(queued, "UTC");
    assert.equal(view.decisionId, "41");
    assert.equal(view.canWithdraw, true);
    assert.equal(view.withdrawing, false);
  });

  it("offers nothing to withdraw on an operator's sweep", () => {
    const view = cardView({ ...queued, decision_action: "sweep" }, "UTC");
    assert.equal(view.canWithdraw, false);
  });

  it("offers nothing to withdraw on a card waiting for the owner", () => {
    const view = cardView({ ...queued, status: "pending" }, "UTC");
    assert.equal(view.decisionId, null);
    assert.equal(view.canWithdraw, false);
  });

  it("shows a request in flight, and offers it once only", () => {
    const view = cardView({ ...queued, withdraw_requested: true }, "UTC");
    assert.equal(view.withdrawing, true);
    assert.equal(view.canWithdraw, false);
  });

  it("says when the worker declined it", () => {
    const view = cardView({ ...queued, withdraw_declined: true }, "UTC");
    assert.equal(view.withdrawDeclined, true);
    assert.equal(view.canWithdraw, false);
  });

  it("says why a decision waits: paused, or an Edit at the cap", () => {
    const paused = { paused: true, budgetState: "ok" } as const;
    const capped = { paused: false, budgetState: "exhausted" } as const;
    assert.match(cardView(queued, "UTC", new Set(), paused).held ?? "", /^Paused/);
    assert.equal(cardView(queued, "UTC", new Set(), capped).held, null);
    const edit = { ...queued, decision_action: "edit" };
    assert.match(cardView(edit, "UTC", new Set(), capped).held ?? "", /spending cap/);
  });

  it("moves the layout key when the footer changes", () => {
    const before = layoutKey([{ ...queued, footer: footerKey(cardView(queued, "UTC")) }]);
    const asked = { ...queued, withdraw_requested: true };
    const after = layoutKey([{ ...asked, footer: footerKey(cardView(asked, "UTC")) }]);
    assert.notEqual(before, after);
  });
});
