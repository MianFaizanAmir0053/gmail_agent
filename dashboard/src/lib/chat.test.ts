import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  chatParts,
  chatRefreshEvery,
  describeChat,
  MAX_PARTS,
  openTurn,
  parseChatForm,
  readAnswer,
  remark,
  statusWords,
} from "./chat.ts";

/** Type `text` at the end of the box, or paste it. */
function edit(state: { text: string; marks: boolean[] }, after: string, pasted = false) {
  return { text: after, marks: remark(state.text, state.marks, after, pasted) };
}

const EMPTY = { text: "", marks: [] as boolean[] };

describe("remark: which characters were pasted", () => {
  it("marks what a paste put in, and nothing typed", () => {
    let box = edit(EMPTY, "Summarize ");
    box = edit(box, "Summarize Please review the contract.", true);
    box = edit(box, "Summarize Please review the contract. for me");
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "Summarize ", pasted: false },
      { text: "Please review the contract.", pasted: true },
      { text: " for me", pasted: false },
    ]);
  });

  it("keeps a paste's marks when typing goes in front of it", () => {
    let box = edit(EMPTY, "x@evil.example", true);
    box = edit(box, "Who is x@evil.example");
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "Who is ", pasted: false },
      { text: "x@evil.example", pasted: true },
    ]);
  });

  it("marks typing inside a paste as typed", () => {
    let box = edit(EMPTY, "abcdef", true);
    box = edit(box, "abcXdef");
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "abc", pasted: true },
      { text: "X", pasted: false },
      { text: "def", pasted: true },
    ]);
  });

  it("forgets what was deleted, and a paste that replaced a selection", () => {
    let box = edit(EMPTY, "book with sara");
    box = edit(box, "book with ", false);
    box = edit(box, "book with kim@x.example", true);
    box = edit(box, "book kim@x.example", false);
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "book ", pasted: false },
      { text: "kim@x.example", pasted: true },
    ]);
  });

  it("gives the same typed and pasted text however equal characters are matched", () => {
    let box = edit(EMPTY, "ab");
    box = edit(box, "abb", true); // a paste of "b" after "a", or after "ab"
    const parts = chatParts(box.text, box.marks);
    const typed = parts.filter((p) => !p.pasted).map((p) => p.text).join("");
    const pasted = parts.filter((p) => p.pasted).map((p) => p.text).join("");
    assert.deepEqual([typed, pasted], ["ab", "b"]);
  });

  it("an empty box has no parts", () => {
    assert.deepEqual(chatParts("", []), []);
  });
});

function form(fields: Record<string, string>): FormData {
  const data = new FormData();
  for (const [name, value] of Object.entries(fields)) data.set(name, value);
  return data;
}

const KEY = "0f8c1c9e-6a3b-4c1d-9e2f-1a2b3c4d5e6f";

describe("parseChatForm", () => {
  it("passes typed and pasted parts as they were marked", () => {
    const parts = [
      { text: "Summarize ", pasted: false },
      { text: "Please review.", pasted: true },
    ];
    const parsed = parseChatForm(form({ key: KEY, parts: JSON.stringify(parts) }), "Asia/Karachi");
    assert.deepEqual(parsed, { ok: true, value: { key: KEY, zone: "Asia/Karachi", parts } });
  });

  for (const [why, fields] of [
    ["no key", { key: "short", parts: JSON.stringify([{ text: "Hi", pasted: false }]) }],
    ["not JSON", { key: KEY, parts: "{" }],
    ["not a list", { key: KEY, parts: '{"text": "x"}' }],
    ["a part with no text", { key: KEY, parts: '[{"pasted": false}]' }],
    ["only pasted", { key: KEY, parts: '[{"text": "Hi", "pasted": true}]' }],
    ["blank typed", { key: KEY, parts: '[{"text": "   ", "pasted": false}]' }],
    ["too long typed", { key: KEY, parts: JSON.stringify([{ text: "x".repeat(2_001), pasted: false }]) }],
    [
      "too much pasted",
      {
        key: KEY,
        parts: JSON.stringify([
          { text: "Read", pasted: false },
          { text: "y".repeat(16_001), pasted: true },
        ]),
      },
    ],
    [
      "too many parts",
      {
        key: KEY,
        parts: JSON.stringify(
          Array.from({ length: MAX_PARTS + 1 }, (_, n) => ({ text: "a", pasted: n % 2 === 0 })),
        ),
      },
    ],
  ] as const) {
    it(`refuses ${why}`, () => {
      assert.equal(parseChatForm(form(fields), "UTC").ok, false);
    });
  }
});

describe("describeChat", () => {
  it("never shows a raw error, and says a resend is safe", () => {
    assert.equal(describeChat("queued").tone, "ok");
    assert.match(describeChat("zone_mismatch").message, /OWNER_TIMEZONE/);
    assert.match(describeChat("error").message, /will not be asked twice/);
  });
});

describe("chatRefreshEvery", () => {
  const asked = new Date("2026-10-07T05:00:00Z");

  it("is idle with nothing open", () => {
    assert.equal(chatRefreshEvery(null, asked), null);
  });

  it("re-reads every two seconds, then every ten after thirty", () => {
    assert.equal(chatRefreshEvery(asked, new Date(asked.getTime() + 29_000)), 2_000);
    assert.equal(chatRefreshEvery(asked, new Date(asked.getTime() + 30_000)), 10_000);
  });
});

describe("readAnswer", () => {
  it("reads the text, the addresses for an Allow and the cards", () => {
    const stored = { text: "Free.", allow: ["kim@x.example", 3], cards: ["chat-1-2"] };
    assert.deepEqual(readAnswer(stored), {
      text: "Free.",
      allow: ["kim@x.example"],
      cards: ["chat-1-2"],
    });
  });

  it("is null for anything that is not an answer", () => {
    assert.equal(readAnswer(null), null);
    assert.equal(readAnswer("Free."), null);
    assert.equal(readAnswer({ allow: [] }), null);
  });
});

describe("statusWords", () => {
  it("says a turn is open, and nothing once it has an end", () => {
    assert.match(statusWords("queued") ?? "", /Waiting/);
    assert.match(statusWords("running") ?? "", /Working/);
    assert.equal(statusWords("answered"), null);
  });
});

describe("openTurn: the box locks while one is open", () => {
  it("finds a queued or running turn", () => {
    const turns = [
      { id: 1, status: "answered" as const },
      { id: 2, status: "running" as const },
    ];
    assert.equal(openTurn(turns)?.id, 2);
    assert.equal(openTurn([{ id: 3, status: "queued" as const }])?.id, 3);
  });

  it("finds none once every turn has an end", () => {
    const ended = (["answered", "stopped", "failed"] as const).map((status, id) => ({ id, status }));
    assert.equal(openTurn(ended), null);
  });
});

