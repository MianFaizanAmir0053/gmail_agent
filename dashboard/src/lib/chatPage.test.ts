import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

/**
 * The answers are text from mail, written into the page by code (M19, D7).
 * Nothing on the chat page may turn text into markup.
 */
const FILES = [
  "src/app/chat/page.tsx",
  "src/components/ChatBox.tsx",
  "src/components/ChatAllow.tsx",
];

describe("the chat page shows text as text", () => {
  for (const file of FILES) {
    it(`${file} makes no markup from text`, () => {
      const source = readFileSync(file, "utf8");
      assert.doesNotMatch(source, /dangerouslySetInnerHTML|innerHTML|\beval\(/);
    });
  }
});
