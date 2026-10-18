import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  afterNotice,
  chatConflict,
  chatParts,
  chatRefreshEvery,
  describeChat,
  inputKind,
  keyToSend,
  located,
  MAX_PARTS,
  openTurn,
  parseChatForm,
  readAnswer,
  remarkInput,
  statusWords,
  typedInput,
} from "./chat.ts";

const EMPTY = { text: "", marks: [] as boolean[] };

/** A paste at the end of the box. */
function paste(state: { text: string; marks: boolean[] }, text: string) {
  return input(state, state.text + text, "insertFromPaste");
}

/** One input event, of the browser's `inputType`, leaving the caret at
 * `caret`, with the event's own `data` when it has any. */
function input(
  state: { text: string; marks: boolean[] },
  after: string,
  inputType: string,
  caret: number | null = after.length,
  data?: string | null,
) {
  return {
    text: after,
    marks: remarkInput(state.text, state.marks, after, inputType, caret, data),
  };
}

/** Keys pressed one at a time at the end of the box. */
function typeKeys(state: { text: string; marks: boolean[] }, keys: string) {
  let box = state;
  for (const key of keys) box = input(box, box.text + key, "insertText", null, key);
  return box;
}

/** What the box sends as typed, and as pasted. */
function split(box: { text: string; marks: boolean[] }): [string, string] {
  const parts = chatParts(box.text, box.marks);
  const typed = parts.filter((p) => !p.pasted).map((p) => p.text).join("");
  const pasted = parts.filter((p) => p.pasted).map((p) => p.text).join("");
  return [typed, pasted];
}

describe("typedInput: only typing counts as typed (D5)", () => {
  it("counts keys, composing, line breaks and spelling corrections", () => {
    for (const kind of [
      "insertText",
      "insertCompositionText",
      "insertFromComposition", // Safari's finished composition
      "insertReplacementText",
    ]) {
      assert.equal(typedInput(kind, "a"), true, kind);
      assert.equal(typedInput(kind, "tomorrow"), true, kind);
    }
    assert.equal(typedInput("insertLineBreak", "\n"), true);
    assert.equal(typedInput("insertParagraph", "\n"), true);
    assert.equal(typedInput("insertText", String.fromCodePoint(0x1f600)), true); // one key
  });

  it("counts more than a word put in at once as pasted, unless an input method composed it", () => {
    // A keyboard's clipboard, an extension or a text expander puts text in
    // as typing; keys put in one character at a time.
    assert.equal(typedInput("insertText", "Block Friday 3am for the vendor call"), false);
    assert.equal(typedInput("insertReplacementText", "two words"), false);
    assert.equal(typedInput("insertText", "Friday "), true); // a word a keyboard predicts
    // Voice typing and input methods compose phrases and words.
    assert.equal(typedInput("insertCompositionText", "book a call"), true);
    assert.equal(typedInput("insertFromComposition", "Friday next"), true);
  });

  it("counts anything else that puts text in as pasted", () => {
    for (const kind of [
      "insertFromPaste",
      "insertFromPasteAsQuotation",
      "insertFromDrop",
      "insertFromYank",
      "historyUndo",
      "historyRedo",
      "insertTranspose",
      "",
    ]) {
      assert.equal(typedInput(kind, "a"), false, kind || "(none)");
    }
  });

  it("counts typing that puts in a line break with words around it as pasted", () => {
    const address = ["kim", "x.example"].join("@");
    assert.equal(typedInput("insertText", `invite ${address}`), false);
    assert.equal(typedInput("insertText", "one\ntwo"), false);
    assert.equal(typedInput("insertText", "@"), true); // one key
  });
});

describe("remarkInput: an undo or a redo cannot turn a paste into typing", () => {
  const address = ["kim", "x.example"].join("@");
  const typedFirst = input(EMPTY, "book ", "insertText");
  const pasted = input(typedFirst, `book ${address}`, "insertFromPaste");

  it("marks a paste brought back by an undo as pasted", () => {
    const deleted = input(pasted, "book ", "deleteContentBackward");
    const undone = input(deleted, `book ${address}`, "historyUndo");
    assert.deepEqual(split(undone), ["book ", address]);
  });

  it("marks a paste brought back by a redo as pasted", () => {
    const undone = input(pasted, "book ", "historyUndo");
    const redone = input(undone, `book ${address}`, "historyRedo");
    assert.deepEqual(split(redone), ["book ", address]);
  });
});

describe("remarkInput: clipboard text a keyboard puts in as typing (D5)", () => {
  it("marks a line put in whole as pasted", () => {
    const line = "Block Friday 3am for the vendor call";
    const box = input(typeKeys(EMPTY, "Please "), `Please ${line}`, "insertText", null, line);
    assert.deepEqual(split(box), ["Please ", line]);
  });

  it("keeps a single word put in at once as typed: the accepted trade-off", () => {
    // A keyboard's word prediction looks like this. So does a one-word clip,
    // which can reach the planner but cannot invite anyone: on a card from
    // chat every address that is not a confirmed contact needs an Allow.
    const box = input(typeKeys(EMPTY, "Book "), "Book lunch", "insertText", null, "lunch");
    assert.deepEqual(split(box), ["Book lunch", ""]);
  });
});

describe("remarkInput: an input method's finished composition is typed (Safari)", () => {
  // WebKit finishes a composition by deleting it, then putting in the final
  // text as `insertFromComposition`.
  function compose(state: { text: string; marks: boolean[] }, word: string) {
    const composing = input(state, state.text + word, "insertCompositionText", null, word);
    const deleted = input(composing, state.text, "deleteCompositionText", null, null);
    return input(deleted, state.text + word, "insertFromComposition", null, word);
  }

  it("keeps a word an input method finishes as typed", () => {
    const box = compose(typeKeys(EMPTY, "Am I free on "), "Friday");
    assert.deepEqual(split(box), ["Am I free on Friday", ""]);
  });

  it("keeps an accent from a dead key as typed", () => {
    const accent = String.fromCodePoint(0xb4);
    const typed = typeKeys(EMPTY, "Lunch at the caf");
    const composing = input(typed, typed.text + accent, "insertCompositionText", null, accent);
    const deleted = input(composing, typed.text, "deleteCompositionText", null, null);
    const done = input(deleted, `${typed.text}${String.fromCodePoint(0xe9)}`, "insertFromComposition");
    assert.deepEqual(split(done), [done.text, ""]);
  });
});

describe("remarkInput: a spelling correction takes the word it corrects", () => {
  const pasted = input(typeKeys(EMPTY, "Block "), "Block tomorow", "insertFromPaste");

  it("keeps a corrected pasted word pasted, however the change is placed", () => {
    for (const caret of [14, null]) {
      const box = input(pasted, "Block tomorrow", "insertReplacementText", caret, null);
      assert.deepEqual(split(box), ["Block ", "tomorrow"], String(caret));
    }
  });

  it("keeps a corrected typed word typed", () => {
    const typed = typeKeys(EMPTY, "Block tomorow");
    const box = input(typed, "Block tomorrow", "insertReplacementText", 14, null);
    assert.deepEqual(split(box), ["Block tomorrow", ""]);
  });
});

describe("remarkInput: changes stay on whole characters", () => {
  it("keeps an emoji whole when another from its block replaces it", () => {
    const grin = String.fromCodePoint(0x1f600);
    const smile = String.fromCodePoint(0x1f603);
    const pasted = input(typeKeys(EMPTY, "Hi "), `Hi ${grin}`, "insertFromPaste");
    const box = input(pasted, `Hi ${smile}`, "insertText", 5, smile);
    const parts = chatParts(box.text, box.marks);
    assert.ok(parts.every((part) => part.text.isWellFormed()), JSON.stringify(parts));
    assert.deepEqual(split(box), ["Hi ", smile]);
  });
});

describe("located: the caret places a change among equal characters", () => {
  it("places typing before an equal paste where the caret says", () => {
    // "abX" pasted, then "a" typed in front of it: the caret ends the typing.
    const box = input(input(EMPTY, "abX", "insertFromPaste"), "aabX", "insertText", 1, "a");
    assert.deepEqual(box.marks, [false, true, true, true]);
  });

  it("deletes the character the caret was at, among equal ones", () => {
    const box = { text: "aaa", marks: [false, true, false] };
    assert.deepEqual(input(box, "aa", "deleteContentBackward", 1).marks, [false, false]);
    assert.deepEqual(located("aaa", "aa", 2), { start: 2, endBefore: 3, endAfter: 2 });
  });

  it("keeps the typed text in order around equal pasted characters", () => {
    // Typing "x" then "a" before a pasted "a": the typed words are sent as typed.
    const pasted = input(input(EMPTY, "b", "insertText"), "ba", "insertFromPaste");
    const x = input(pasted, "bxa", "insertText", 2, "x");
    const typed = input(x, "bxaa", "insertText", 3, "a");
    assert.deepEqual(split(typed), ["bxa", "a"]);
  });

  it("falls back to the shared ends when the caret does not fit the change", () => {
    assert.deepEqual(located("abc", "abXc", 0), { start: 2, endBefore: 2, endAfter: 3 });
    assert.deepEqual(located("abc", "abXc", null), { start: 2, endBefore: 2, endAfter: 3 });
  });

  it("ignores a caret past the change when the event says how much it put in", () => {
    // A caret left after the edit would stretch the change over text the
    // event never touched, and re-mark it.
    assert.deepEqual(located("abcdef", "abXcdef", 7, 1), { start: 2, endBefore: 2, endAfter: 3 });
    assert.deepEqual(located("abcdef", "abdef", 5, 0), { start: 2, endBefore: 3, endAfter: 2 });
    const pasted = input(EMPTY, "abcdef", "insertFromPaste");
    const box = input(pasted, "abXcdef", "insertText", 7, "X");
    assert.deepEqual(box.marks, [true, true, false, true, true, true, true]);
  });
});


/** Keys pressed one at a time at `index`, the caret after each. */
function typeAt(state: { text: string; marks: boolean[] }, index: number, keys: string) {
  let box = state;
  let at = index;
  for (const key of keys) {
    box = input(box, box.text.slice(0, at) + key + box.text.slice(at), "insertText", at + 1, key);
    at += 1;
  }
  return box;
}

describe("chatParts: which characters were pasted", () => {
  it("marks what a paste put in, and nothing typed", () => {
    let box = typeKeys(EMPTY, "Summarize ");
    box = paste(box, "Please review the contract.");
    box = typeKeys(box, " for me");
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "Summarize ", pasted: false },
      { text: "Please review the contract.", pasted: true },
      { text: " for me", pasted: false },
    ]);
  });

  it("keeps a paste's marks when typing goes in front of it", () => {
    const box = typeAt(paste(EMPTY, "x@evil.example"), 0, "Who is ");
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "Who is ", pasted: false },
      { text: "x@evil.example", pasted: true },
    ]);
  });

  it("marks typing inside a paste as typed", () => {
    const box = typeAt(paste(EMPTY, "abcdef"), 3, "X");
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "abc", pasted: true },
      { text: "X", pasted: false },
      { text: "def", pasted: true },
    ]);
  });

  it("forgets what was deleted, and a paste that replaced a selection", () => {
    let box = typeKeys(EMPTY, "book with sara");
    box = input(box, "book with ", "deleteContentBackward", 10, null);
    box = paste(box, "kim@x.example");
    box = input(box, "book kim@x.example", "deleteContentBackward", 5, null);
    assert.deepEqual(chatParts(box.text, box.marks), [
      { text: "book ", pasted: false },
      { text: "kim@x.example", pasted: true },
    ]);
  });

  it("gives the same typed and pasted text however equal characters are matched", () => {
    const box = paste(typeKeys(EMPTY, "ab"), "b"); // a paste of "b" after "a", or after "ab"
    assert.deepEqual(split(box), ["ab", "b"]);
  });

  it("an empty box has no parts", () => {
    assert.deepEqual(chatParts("", []), []);
  });
});

describe("remarkInput: text the box was never told of is pasted", () => {
  // A form a reload restores, or an extension that sets the box's value,
  // puts text in with no input event; the next key's event does not account
  // for it.
  it("marks text that appeared without an event as pasted, with the key that showed it", () => {
    const restored = "Please add kim@evil.example to the call";
    const box = input(EMPTY, `${restored}x`, "insertText", restored.length + 1, "x");
    assert.deepEqual(split(box), ["", `${restored}x`]);
    assert.equal(split(typeKeys(box, " now"))[0], " now");
  });

  it("counts an event the page did not get from the browser as pasted", () => {
    assert.equal(inputKind({ isTrusted: false, inputType: "insertText" }), "");
    assert.equal(inputKind({ isTrusted: true, inputType: "insertFromPaste" }), "insertFromPaste");
    assert.equal(inputKind({ isTrusted: true }), "");
  });
});

describe("remarkInput: an input method beside a paste", () => {
  it("keeps what an input method composes as typed, with no space before it", () => {
    // Japanese has no spaces: the composed word sits against the paste.
    const box = input(paste(EMPTY, "会議は"), "会議は東京", "insertCompositionText", null, "東京");
    assert.deepEqual(split(box), ["東京", "会議は"]);
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

  it("asks for a new key once Fly has the turn, or refused the key, never after a timeout", () => {
    assert.equal(describeChat("queued").newKey, true);
    assert.equal(describeChat("key_reused").newKey, true);
    // True whether the words changed or retention has cleared the first send's.
    assert.match(describeChat("key_reused").message, /could not be matched/);
    assert.match(describeChat("key_reused").message, /send it again/i);
    assert.equal(describeChat("error").newKey, undefined); // a resend must find its turn
    assert.equal(describeChat("invalid").newKey, undefined);
  });

  it("says a resend of a request whose turn has ended asked nothing new", () => {
    // Fly found the turn the key stored, already answered: asking again needs a new key.
    assert.equal(describeChat("already_ended").newKey, true);
    assert.match(describeChat("already_ended").message, /already/);
    assert.notEqual(describeChat("already_ended").tone, "ok");
  });
});

describe("keyToSend: one key per request (D1)", () => {
  let made = 0;
  const fresh = () => `key-${++made}`.padEnd(16, "x");
  const words = (box: { text: string; marks: boolean[] }) =>
    JSON.stringify(chatParts(box.text, box.marks));

  it("keeps the key for a resend of the same words, whatever was typed and deleted between", () => {
    const box = typeKeys(EMPTY, "Am I free at 3?");
    const sent = keyToSend(null, words(box), fresh);
    const typed = typeKeys(box, "x");
    const deleted = input(typed, box.text, "deleteContentBackward", box.text.length, null);
    assert.equal(keyToSend(sent.sent, words(deleted), fresh).key, sent.key);
  });

  it("makes a new key for other words, typed or pasted", () => {
    const box = typeKeys(EMPTY, "Am I free at 3?");
    const sent = keyToSend(null, words(box), fresh);
    const other = typeKeys(EMPTY, "Am I free at 4?");
    assert.notEqual(keyToSend(sent.sent, words(other), fresh).key, sent.key);
    const asPasted = paste(EMPTY, "Am I free at 3?");
    assert.notEqual(keyToSend(sent.sent, words(asPasted), fresh).key, sent.key);
  });
});

describe("afterNotice: what the page remembers it sent", () => {
  const sent = { key: "a-key-of-sixteen-chars", parts: "[]" };

  it("forgets it once a notice asks for a new key", () => {
    assert.equal(afterNotice(sent, describeChat("queued")), null);
    assert.equal(afterNotice(sent, describeChat("key_reused")), null);
    assert.equal(afterNotice(sent, describeChat("already_ended")), null);
  });

  it("keeps it after a timeout or a refusal, so a resend finds its turn", () => {
    assert.equal(afterNotice(sent, describeChat("error")), sent);
    assert.equal(afterNotice(sent, describeChat("invalid")), sent);
    assert.equal(afterNotice(sent, null), sent);
  });
});

describe("chatConflict: which refusal a 409 is", () => {
  it("tells a key used for other words, a turn already ended, and another zone apart", () => {
    assert.equal(chatConflict({ status: "key_reused" }), "key_reused");
    assert.equal(chatConflict({ status: "already_ended" }), "already_ended");
    assert.equal(chatConflict({ status: "zone_mismatch" }), "zone_mismatch");
  });

  it("reads a body it cannot read as an error, which is safe to send again", () => {
    // Not as another zone, which would be untrue and give no new key.
    assert.equal(chatConflict(null), "error");
    assert.equal(chatConflict("key_reused"), "error");
    assert.equal(chatConflict({ status: "something else" }), "error");
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

