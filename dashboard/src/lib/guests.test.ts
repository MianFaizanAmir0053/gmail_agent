import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { cardSource, guestKey, isAddress } from "./guests.ts";

describe("cardSource", () => {
  // The same cases as tests/test_participants.py: both cards must name the
  // same source for the same payload (M18, D5).
  const stored = (where: string, { inThread = false, outsideNow = false } = {}) => ({
    guest_sources: { "sara@example.com": where },
    thread_guests: inThread ? ["sara@example.com"] : [],
    outside_guests: outsideNow ? ["sara@example.com"] : [],
  });
  const none = new Set<string>();
  const sara = new Set(["sara@example.com"]);

  it("knows no source for a payload parked before M18", () => {
    assert.equal(cardSource("sara@example.com", { outside_guests: ["sara@example.com"] }, none), null);
    assert.equal(cardSource("sara@example.com", { guest_sources: { "sara@example.com": "trusted" } }, none), null);
    assert.equal(cardSource("sara@example.com", { guest_sources: ["sara@example.com"] }, none), null);
    assert.equal(cardSource("constructor", { guest_sources: {} }, none), null);
  });

  it("is not in the thread once a check has found them outside it", () => {
    assert.equal(cardSource("sara@example.com", stored("email", { inThread: true, outsideNow: true }), none), "email");
  });

  it("stays allowed once a check no longer marks them", () => {
    assert.equal(cardSource("sara@example.com", stored("quoted"), sara), "allowed");
  });

  it("shows the first source that holds", () => {
    assert.equal(cardSource("sara@example.com", stored("absent", { inThread: true }), sara), "thread");
    assert.equal(cardSource("sara@example.com", stored("absent", { outsideNow: true }), sara), "allowed");
    assert.equal(cardSource("sara@example.com", stored("absent", { outsideNow: true }), none), "absent");
  });

  it("compares guests the way Gmail reads them", () => {
    const card = stored("email", { inThread: true });
    assert.equal(cardSource("sara@example.com", { ...card, thread_guests: ["Sara@Example.com"] }, none), "thread");
  });
});

describe("guestKey", () => {
  // The same cases as tests/test_participants.py: the card hides a guest
  // already allowed only if both sides spell the mailbox the same way.
  it("reads a Gmail address the way Gmail does", () => {
    assert.equal(guestKey("Sara.Khan+work@gmail.com"), guestKey("sarakhan@gmail.com"));
    assert.equal(guestKey("sarakhan@googlemail.com"), guestKey("sara.khan@gmail.com"));
    assert.equal(guestKey("Ali@Example.com"), guestKey("ali@example.com"));
  });

  it("trims what Python trims: ASCII space and a byte-order mark", () => {
    assert.equal(guestKey("\ufeffSara@Example.com "), "sara@example.com");
  });

  it("compares anything else exactly, a +tag or a dot included", () => {
    assert.notEqual(guestKey("ali+sales@example.com"), guestKey("ali@example.com"));
    assert.notEqual(guestKey("a.li@example.com"), guestKey("ali@example.com"));
  });
});

describe("isAddress", () => {
  it("takes one address", () => {
    assert.equal(isAddress("sara@example.com"), true);
  });

  it("refuses anything else", () => {
    for (const value of ["", "sara", "sara@", "@example.com", "sara@example", "a b@example.com", "a@b.com, c@d.com"]) {
      assert.equal(isAddress(value), false, value);
    }
    assert.equal(isAddress(`${"a".repeat(320)}@example.com`), false);
    // As `_checked` in app/policy/contacts.py: printable ASCII only.
    for (const value of ["s\u00e1ra@example.com", "\u0085a@b.com", "sara@exam\u00a0ple.com"]) {
      assert.equal(isAddress(value), false, value);
    }
  });
});
