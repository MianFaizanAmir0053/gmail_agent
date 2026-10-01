import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  admitsSignIn,
  admitsSignInAttempt,
  isExemptPath,
  isOwnerSession,
  isPairingCode,
  isPairingEnabled,
  PAIRING_PROVIDER_ID,
} from "./access.ts";

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

describe("admitsSignInAttempt", () => {
  const verified = { email: OWNER, email_verified: true };

  it("keeps Google's rule exactly, reading only Google's profile", () => {
    for (const pairing of [false, true]) {
      assert.equal(admitsSignInAttempt({ provider: "google", profile: verified }, OWNER, pairing), true);
      assert.equal(
        admitsSignInAttempt({ provider: "google", profile: { email: OWNER, email_verified: false } }, OWNER, pairing),
        false,
      );
      // The address on the user record is not Google's word for anything.
      assert.equal(admitsSignInAttempt({ provider: "google", email: OWNER }, OWNER, pairing), false);
      assert.equal(admitsSignInAttempt({ provider: "google", profile: verified }, "", pairing), false);
    }
  });

  it("admits a pairing sign-in for the owner's address while pairing is on", () => {
    assert.equal(admitsSignInAttempt({ provider: PAIRING_PROVIDER_ID, email: OWNER }, OWNER, true), true);
  });

  it("refuses a pairing sign-in while pairing is off", () => {
    assert.equal(admitsSignInAttempt({ provider: PAIRING_PROVIDER_ID, email: OWNER }, OWNER, false), false);
  });

  it("refuses a pairing sign-in for any other address, or while OWNER_EMAIL is blank", () => {
    assert.equal(
      admitsSignInAttempt({ provider: PAIRING_PROVIDER_ID, email: "someone@example.com" }, OWNER, true),
      false,
    );
    assert.equal(admitsSignInAttempt({ provider: PAIRING_PROVIDER_ID }, OWNER, true), false);
    for (const owner of ["", "   ", undefined]) {
      assert.equal(admitsSignInAttempt({ provider: PAIRING_PROVIDER_ID, email: "" }, owner, true), false);
    }
  });

  it("refuses a provider it does not know, whatever the profile says", () => {
    for (const provider of ["github", "credentials", "", undefined]) {
      assert.equal(
        admitsSignInAttempt({ provider, profile: verified, email: OWNER }, OWNER, true),
        false,
        String(provider),
      );
    }
  });
});

describe("isPairingCode", () => {
  it("accepts six ASCII digits, leading zeros included", () => {
    for (const code of ["123456", "000042", "000000", "999999"]) {
      assert.equal(isPairingCode(code), true, code);
    }
  });

  it("refuses anything else", () => {
    for (const code of [
      "",
      "12345",
      "1234567",
      "12345a",
      " 123456",
      "123456 ",
      "123456\n",
      "12 456",
      "-12345",
      "1e5000",
      // Digits from other scripts: Arabic-Indic and fullwidth 1 to 6.
      String.fromCodePoint(...[1, 2, 3, 4, 5, 6].map((n) => 0x0660 + n)),
      String.fromCodePoint(...[1, 2, 3, 4, 5, 6].map((n) => 0xff10 + n)),
    ]) {
      assert.equal(isPairingCode(code), false, JSON.stringify(code));
    }
  });

  it("refuses what is not a string", () => {
    for (const value of [123456, null, undefined, ["123456"], { code: "123456" }]) {
      assert.equal(isPairingCode(value), false, JSON.stringify(value));
    }
  });
});

describe("isPairingEnabled", () => {
  it("is on only when PAIRING_ENABLED is exactly true", () => {
    assert.equal(isPairingEnabled("true"), true);
  });

  it("is off when unset, blank or anything else", () => {
    for (const value of [undefined, "", "false", "TRUE", "1", "yes", " true"]) {
      assert.equal(isPairingEnabled(value), false, String(value));
    }
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
