import assert from "node:assert/strict";
import { describe, it } from "node:test";

import pathMatch from "next/dist/shared/lib/router/utils/path-match.js";

import config, { IMAGE_POLICY } from "../../next.config.ts";

/** Whether a header rule's `source` covers a path, by Next's own matcher. */
const covers = (source: string, path: string) =>
  pathMatch.getPathMatch(source, { strict: true, removeUnnamedParams: true })(path) !== false;

describe("the content policy", () => {
  it("is sent on every page but sign-in, so it holds however the owner reaches Ask", async () => {
    // A policy comes with a document. A client navigation, and a Back after
    // a reload, keep the first page's document: so every page must send it.
    const rules = (await config.headers?.()) ?? [];
    const policed = rules.filter((rule) =>
      rule.headers.some((h) => h.key === "Content-Security-Policy" && h.value === IMAGE_POLICY),
    );
    const sent = (path: string) => policed.some((rule) => covers(rule.source, path));
    for (const path of ["/", "/chat", "/costs", "/runs/abc", "/activity", "/me", "/pair"]) {
      assert.ok(sent(path), path);
    }
    // Sign-in shows no mail, and its provider logos come from authjs.dev.
    assert.equal(sent("/api/auth/signin"), false);
  });

  it("loads images from this app alone", () => {
    assert.equal(IMAGE_POLICY, "img-src 'self'");
  });
});
