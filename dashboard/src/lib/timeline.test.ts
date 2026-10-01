import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  cardView,
  formatWindow,
  ownerZone,
  shouldRefresh,
  type ProposalRow,
} from "./timeline.ts";

const ROW: ProposalRow = {
  message_id: "18c0f2a3b4c5d6e7",
  revision: 1,
  status: "pending",
  final_status: null,
  action_type: "calendar_invite",
  dry_run: true,
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
