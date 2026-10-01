import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { applicationServerKey, pushSetupState, subscriptionBody } from "./push.ts";

describe("applicationServerKey", () => {
  it("decodes the base64url public key into the 65 bytes of a P-256 point", () => {
    // What `.\tasks.ps1 vapid` prints: 65 bytes, the first one 0x04.
    const key = "BA" + "A".repeat(85);
    const bytes = applicationServerKey(key);
    assert.equal(bytes.length, 65);
    assert.equal(bytes[0], 0x04);
  });
});

describe("pushSetupState", () => {
  const IPHONE_SAFARI =
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";
  const ANDROID_CHROME =
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36";

  it("asks an iPhone in a Safari tab to install the app first", () => {
    // iOS offers push only to a web app added to the Home Screen.
    assert.equal(
      pushSetupState({ userAgent: IPHONE_SAFARI, standalone: false, hasPush: false, permission: "default", subscribed: false }),
      "install-first",
    );
  });

  it("offers notifications once installed, or on Android", () => {
    assert.equal(
      pushSetupState({ userAgent: IPHONE_SAFARI, standalone: true, hasPush: true, permission: "default", subscribed: false }),
      "off",
    );
    assert.equal(
      pushSetupState({ userAgent: ANDROID_CHROME, standalone: false, hasPush: true, permission: "default", subscribed: false }),
      "off",
    );
  });

  it("knows when notifications are on, or refused", () => {
    const base = { userAgent: ANDROID_CHROME, standalone: false, hasPush: true };
    assert.equal(pushSetupState({ ...base, permission: "granted", subscribed: true }), "on");
    assert.equal(pushSetupState({ ...base, permission: "denied", subscribed: false }), "denied");
  });

  it("says nothing on a browser without push", () => {
    assert.equal(
      pushSetupState({ userAgent: "Mozilla/5.0 (X11; Linux) Firefox/130.0", standalone: false, hasPush: false, permission: "default", subscribed: false }),
      "unsupported",
    );
  });
});

describe("subscriptionBody", () => {
  it("passes on the endpoint and keys, and nothing else", () => {
    const body = subscriptionBody({
      endpoint: "https://fcm.googleapis.com/fcm/send/abc",
      expirationTime: null,
      keys: { p256dh: "p", auth: "a" },
      extra: "ignored",
    });
    assert.deepEqual(body, {
      endpoint: "https://fcm.googleapis.com/fcm/send/abc",
      keys: { p256dh: "p", auth: "a" },
    });
  });

  it("refuses something that is not a subscription", () => {
    assert.equal(subscriptionBody({ endpoint: 42 }), null);
    assert.equal(subscriptionBody({ endpoint: "https://x", keys: { p256dh: "p" } }), null);
    assert.equal(subscriptionBody(null), null);
  });
});
