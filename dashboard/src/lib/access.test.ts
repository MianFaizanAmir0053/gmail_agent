import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { admitsSignIn, isExemptPath, isOwnerSession } from "./access.ts";

const OWNER = "owner@example.com";

describe("admitsSignIn", () => {
  it("admits the owner's verified address", () => {
    assert.equal(admitsSignIn({ email: OWNER, email_verified: true }, OWNER), true);
  });

  it("ignores case and surrounding space in either address", () => {
    assert.equal(admitsSignIn({ email: "Owner@Example.com", email_verified: true }, ` ${OWNER} `), true);
  });

  it("refuses another address", () => {
    assert.equal(admitsSignIn({ email: "someone@example.com", email_verified: true }, OWNER), false);
  });

  it("refuses the owner's address when Google has not verified it", () => {
    assert.equal(admitsSignIn({ email: OWNER, email_verified: false }, OWNER), false);
    assert.equal(admitsSignIn({ email: OWNER }, OWNER), false);
  });

  it("admits nobody while OWNER_EMAIL is blank or unset", () => {
    for (const owner of ["", "   ", undefined]) {
      assert.equal(admitsSignIn({ email: OWNER, email_verified: true }, owner), false);
      assert.equal(admitsSignIn({ email: "", email_verified: true }, owner), false);
    }
  });

  it("refuses a profile with no address", () => {
    assert.equal(admitsSignIn({ email_verified: true }, OWNER), false);
    assert.equal(admitsSignIn(undefined, OWNER), false);
  });
});

describe("isOwnerSession", () => {
  it("accepts a session for the owner", () => {
    assert.equal(isOwnerSession(OWNER, OWNER), true);
  });

  it("refuses a session once OWNER_EMAIL names someone else", () => {
    assert.equal(isOwnerSession(OWNER, "new-owner@example.com"), false);
  });

  it("refuses no session, and every session while OWNER_EMAIL is blank", () => {
    assert.equal(isOwnerSession(undefined, OWNER), false);
    assert.equal(isOwnerSession(null, OWNER), false);
    assert.equal(isOwnerSession(OWNER, ""), false);
    assert.equal(isOwnerSession("", ""), false);
  });
});

describe("isExemptPath", () => {
  it("exempts exactly the manifest, the service worker, icons and Auth.js routes", () => {
    for (const path of [
      "/manifest.webmanifest",
      "/sw.js",
      "/icons/icon-192.png",
      "/icons/apple-touch-icon.png",
      "/api/auth/signin",
      "/api/auth/callback/google",
      "/api/auth/session",
    ]) {
      assert.equal(isExemptPath(path), true, path);
    }
  });

  it("gates everything else, including near misses", () => {
    for (const path of [
      "/",
      "/me",
      "/costs",
      "/analytics",
      "/api/decisions",
      "/api/auth",
      "/api/authx/signin",
      "/icons",
      "/iconsx/icon.png",
      "/sw.js/",
      "/sw.jsx",
      "/manifest.webmanifest.bak",
      "/public/sw.js",
    ]) {
      assert.equal(isExemptPath(path), false, path);
    }
  });
});
