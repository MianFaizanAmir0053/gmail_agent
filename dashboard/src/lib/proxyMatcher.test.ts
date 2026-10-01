import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { isExemptPath } from "./access.ts";

/**
 * The proxy's matcher, read from `src/proxy.ts` itself. Next requires it to be
 * a literal in that file, so it cannot be imported from a shared module.
 * Paths it skips never reach the sign-in gate at all.
 */
const source = readFileSync(new URL("../proxy.ts", import.meta.url), "utf8");
const literal = /matcher:\s*\[\s*("(?:[^"\\]|\\.)*")\s*,?\s*\]/.exec(source)?.[1];
assert.ok(literal, "src/proxy.ts has one matcher, written as a string literal");
const matcher = new RegExp(`^${JSON.parse(literal)}$`);
const gated = (path: string) => matcher.test(path);

describe("the proxy's matcher", () => {
  it("gates every page, action and API route", () => {
    for (const path of [
      "/",
      "/analytics",
      "/me",
      "/runs/abc123",
      "/api/push-subscription",
      "/icons",
      "/swajs",
      "/sw.js.map",
    ]) {
      assert.equal(gated(path), true, path);
    }
  });

  it("skips only build output and the files a phone fetches without a session", () => {
    for (const path of [
      "/_next/static/chunks/app-1a2b.js",
      "/favicon.ico",
      "/sw.js",
      "/manifest.webmanifest",
      "/icons/icon-192.png",
    ]) {
      assert.equal(gated(path), false, path);
    }
  });

  it("skips no file the sign-in rule would refuse", () => {
    // Skipping only spares the proxy a run on files anyone may fetch. The rule
    // in access.ts would let them through anyway.
    for (const path of ["/sw.js", "/manifest.webmanifest", "/icons/icon-192.png"]) {
      assert.equal(isExemptPath(path), true, path);
    }
  });
});
