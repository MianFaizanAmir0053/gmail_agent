import assert from "node:assert/strict";
import { describe, it } from "node:test";

import config, { CHAT_POLICY } from "../../next.config.ts";

describe("the chat page's content policy", () => {
  it("is sent on /chat", async () => {
    const rules = (await config.headers?.()) ?? [];
    const chat = rules.find((rule) => rule.source === "/chat");
    assert.deepEqual(chat?.headers, [{ key: "Content-Security-Policy", value: CHAT_POLICY }]);
  });

  it("loads images from this app alone", () => {
    assert.equal(CHAT_POLICY, "img-src 'self'");
  });
});
