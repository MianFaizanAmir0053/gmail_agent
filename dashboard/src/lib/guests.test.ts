import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { guestKey, isAddress } from "./guests.ts";

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
