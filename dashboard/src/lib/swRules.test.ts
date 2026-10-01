import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import vm from "node:vm";

/**
 * The service worker's caching rule, run exactly as the worker runs it: the
 * worker's own file is evaluated against a stand-in for its global scope.
 * Nothing in it touches the scope until an event arrives.
 */
type Notification = { title: string; options: { body: string; tag: string } };

const source = readFileSync(new URL("../../public/sw.js", import.meta.url), "utf8");
const scope: {
  isCacheable?: (url: URL, origin: string) => boolean;
  notificationFor?: (data: unknown) => Notification;
  addEventListener: () => void;
} = { addEventListener: () => {} };
vm.runInNewContext(source, { self: scope, URL });
const isCacheable = scope.isCacheable!;
const notificationFor = scope.notificationFor!;

const ORIGIN = "https://mailagent.vercel.app";
const at = (path: string) => new URL(path, ORIGIN);

describe("the service worker's cache", () => {
  it("keeps the app's own static build files and icons", () => {
    assert.equal(isCacheable(at("/_next/static/chunks/app-1a2b.js"), ORIGIN), true);
    assert.equal(isCacheable(at("/_next/static/css/9f8e.css"), ORIGIN), true);
    assert.equal(isCacheable(at("/icons/icon-192.png"), ORIGIN), true);
  });

  it("never keeps a page, an API answer or a server-component payload", () => {
    // Signed-in pages and API responses always come from the network (D5):
    // a cached timeline would show proposals that are already decided.
    for (const path of [
      "/",
      "/analytics",
      "/me",
      "/api/push-subscription",
      "/api/auth/session",
      "/?_rsc=abc123",
      "/_next/static/chunks/app.js?_rsc=abc123",
      "/manifest.webmanifest",
      "/sw.js",
    ]) {
      assert.equal(isCacheable(at(path), ORIGIN), false, path);
    }
  });

  it("never keeps anything from another origin", () => {
    assert.equal(
      isCacheable(new URL("https://cdn.example.com/_next/static/x.js"), ORIGIN),
      false,
    );
  });
});

describe("the service worker's notifications", () => {
  it("keeps a sign-in alert apart from proposal pushes", () => {
    // One shared tag let the next proposal replace an unread alert, and Fly
    // never sends the same alert twice.
    const alert = notificationFor({ title: "mailagent", body: "Google sign-in needs attention.", tag: "alert" });
    const proposal = notificationFor({ title: "mailagent", body: "A proposal needs you.", tag: "proposal" });
    assert.notEqual(alert.options.tag, proposal.options.tag);
    assert.equal(alert.options.body, "Google sign-in needs attention.");
  });

  it("gives each mail alert a tag of its own, apart from the sign-in alert", () => {
    // Each mail alert is sent once a day: one must not replace the other, nor
    // an unread sign-in alert, nor be replaced by the next proposal.
    const tags = ["alert", "mail-sync", "mail-feed", "proposal"].map(
      (tag) => notificationFor({ title: "mailagent", body: "x", tag }).options.tag,
    );
    assert.equal(new Set(tags).size, 4);
  });

  it("takes no tag from a push beyond the ones it knows", () => {
    for (const tag of ["anything", "__proto__", "constructor", 42]) {
      assert.equal(notificationFor({ tag }).options.tag, notificationFor({}).options.tag);
    }
  });

  it("falls back to generic words for a push it cannot read", () => {
    const note = notificationFor(null);
    assert.equal(note.title, "mailagent");
    assert.equal(note.options.body, "Something needs you.");
  });
});
