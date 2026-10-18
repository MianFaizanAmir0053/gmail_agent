/**
 * The chat page's rules (M19, D1, D5 and D7). No framework imports, so
 * `node --test` runs them directly.
 *
 * The browser knows what was typed: a mark beside each character of the box
 * says whether it arrived as typing, by the input event's own type. Fly is
 * sent the text in parts,
 * each typed or pasted, so typed and pasted are never guessed from the words,
 * and no character index has to agree between JavaScript and Python.
 */

/** A run of the box's text, typed or pasted. Fly's `ChatPart`. */
export type ChatPart = { text: string; pasted: boolean };

import { isAddress } from "./guests.ts";

export const MAX_TYPED_CHARS = 2_000;
export const MAX_PASTED_CHARS = 16_000;
export const MAX_PARTS = 50;

/** One change to the box: it removed `before` from `start` to `endBefore`,
 * and put in `after` from `start` to `endAfter`. */
export type Change = { start: number; endBefore: number; endAfter: number };

/**
 * Where one change to the box lies. The caret after it, when the browser
 * gives one that fits, ends what was put in: among identical characters it
 * is the only way to know which ones the change touched. A caret that would
 * make the change put in other than `inserting` characters, when the event
 * says how many it put in, is not trusted: it would stretch the change over
 * text the event never touched. Otherwise the change is found by what the
 * old and new text share at each end.
 */
export function located(
  before: string,
  after: string,
  caret?: number | null,
  inserting?: number,
): Change {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) {
    start += 1;
  }
  if (caret != null && caret >= 0 && caret <= after.length) {
    const endBefore = before.length - (after.length - caret);
    if (endBefore >= 0 && before.slice(endBefore) === after.slice(caret)) {
      const placed = { start: Math.min(start, endBefore, caret), endBefore, endAfter: caret };
      if (inserting === undefined || placed.endAfter - placed.start === inserting) return placed;
    }
  }
  let endBefore = before.length;
  let endAfter = after.length;
  while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) {
    endBefore -= 1;
    endAfter -= 1;
  }
  return { start, endBefore, endAfter };
}

/** Keys: one character at a time, or a line break. */
const KEYS: ReadonlySet<string> = new Set(["insertText", "insertLineBreak", "insertParagraph"]);

/** An input method composing, and (in Safari) the composition it finished. */
const COMPOSING: ReadonlySet<string> = new Set(["insertCompositionText", "insertFromComposition"]);

/** A spelling correction: it replaces a word. */
const CORRECTION = "insertReplacementText";

/**
 * Whether one input event's own insertion counts as typed (D5), before the
 * word it lands in is weighed (`remarkInput`). Anything that is not typing
 * counts as pasted: a paste, a drop, an undo or a redo (which can bring back
 * what was pasted), a yank, or a kind the browser does not name.
 *
 * Typing counts as pasted too when it puts in more than a word at once that
 * no input method composed. A keyboard's clipboard, an extension or a text
 * expander puts text in as typing, and keys put in one character at a time.
 * A word a keyboard predicts, with its space, is typing; so is what voice
 * typing or an input method composes.
 *
 * None of this decides whose an address is: on a card from chat, any address
 * that is not a confirmed contact waits for the owner's Allow (M19, D8, as
 * amended on 2026-10-18). The marks decide what the planner reads.
 */
export function typedInput(inputType: string, inserted: string): boolean {
  const composing = COMPOSING.has(inputType);
  if (!KEYS.has(inputType) && !composing && inputType !== CORRECTION) return false;
  if ([...inserted].length <= 1) return true;
  return composing || !/\s/.test(inserted.replace(/ $/, ""));
}

/** The input type of an event the page can trust: one the browser made.
 * One a script made (an extension, say) has none, and so counts as pasted. */
export function inputKind(event: { isTrusted: boolean; inputType?: string }): string {
  return event.isTrusted ? (event.inputType ?? "") : "";
}

/**
 * The marks after one input event. What it put in is pasted unless it was
 * typing (`typedInput`), judged by the event's own text when it gives one.
 *
 * Typing whose own text does not account for the change found in the box is
 * pasted: text appeared there with no event of its own, as when a reload
 * restores a form, or a script sets the box's value.
 *
 * A spelling correction takes on the word it corrects: pasted if any of the
 * word was pasted. Then `settled`.
 */
export function remarkInput(
  before: string,
  marks: readonly boolean[],
  after: string,
  inputType: string,
  caret?: number | null,
  data?: string | null,
): boolean[] {
  const own = inputType === "insertText" && typeof data === "string" ? data : undefined;
  const inserting = own !== undefined ? own.length : inputType.startsWith("delete") ? 0 : undefined;
  const change = located(before, after, caret, inserting);
  const inserted = after.slice(change.start, change.endAfter);
  const judged = own ?? inserted;
  let pasted = inserted.length > 0 && (!typedInput(inputType, judged) || unaccounted(own, inserted));
  if (!pasted && inserted.length > 0 && inputType === CORRECTION) {
    const [from, to] = wordAt(before, change.start, change.endBefore);
    pasted = marks.slice(from, to).some(Boolean);
  }
  return settled(after, marked(marks, before.length, change, pasted));
}

/** Whether an event's own text leaves part of the change unexplained. */
function unaccounted(own: string | undefined, inserted: string): boolean {
  return own !== undefined && own !== inserted;
}

/** The word around `from`..`to`: the characters either side up to whitespace. */
function wordAt(text: string, from: number, to: number): [number, number] {
  let first = from;
  let last = to;
  while (first > 0 && !/\s/.test(text[first - 1] ?? " ")) first -= 1;
  while (last < text.length && !/\s/.test(text[last] ?? " ")) last += 1;
  return [first, last];
}

/**
 * The marks made whole: a character made of two code units (an emoji) is
 * never split between typed and pasted, which would send half a character.
 * Pasted wins.
 */
function settled(text: string, marks: readonly boolean[]): boolean[] {
  const out = marks.slice();
  for (let index = 0; index + 1 < text.length; index += 1) {
    const high = text.charCodeAt(index);
    const low = text.charCodeAt(index + 1);
    const pair = high >= 0xd800 && high <= 0xdbff && low >= 0xdc00 && low <= 0xdfff;
    if (pair && out[index] !== out[index + 1]) {
      out[index] = true;
      out[index + 1] = true;
    }
  }
  return out;
}

function marked(
  marks: readonly boolean[],
  length: number,
  change: Change,
  pasted: boolean,
): boolean[] {
  const kept = marks.slice(0, length);
  return [
    ...kept.slice(0, change.start),
    ...new Array<boolean>(change.endAfter - change.start).fill(pasted),
    ...kept.slice(change.endBefore),
  ];
}

/** The box's text as runs of typed and pasted characters. */
export function chatParts(text: string, marks: readonly boolean[]): ChatPart[] {
  const parts: ChatPart[] = [];
  for (let index = 0; index < text.length; index += 1) {
    const pasted = marks[index] === true;
    const last = parts.at(-1);
    if (last && last.pasted === pasted) last.text += text[index];
    else parts.push({ text: text[index] ?? "", pasted });
  }
  return parts;
}

export type ChatRequest = { key: string; zone: string; parts: ChatPart[] };

type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

const KEY = /^[A-Za-z0-9_-]{16,64}$/;

/** The page's form, checked as Fly will check it, so a refusal is said here. */
export function parseChatForm(data: FormData, zone: string): Parsed<ChatRequest> {
  const key = String(data.get("key") ?? "");
  if (!KEY.test(key)) return { ok: false, error: "Reload the page and ask again." };
  let raw: unknown;
  try {
    raw = JSON.parse(String(data.get("parts") ?? ""));
  } catch {
    return { ok: false, error: "Type what you want done." };
  }
  if (!Array.isArray(raw) || raw.length > MAX_PARTS) {
    return { ok: false, error: "Ask with fewer pasted pieces." };
  }
  const parts: ChatPart[] = [];
  for (const part of raw) {
    if (typeof part?.text !== "string" || typeof part?.pasted !== "boolean") {
      return { ok: false, error: "Type what you want done." };
    }
    parts.push({ text: part.text, pasted: part.pasted });
  }
  const typed = parts
    .filter((part) => !part.pasted)
    .map((part) => part.text)
    .join("")
    .trim();
  const pasted = parts
    .filter((part) => part.pasted)
    .map((part) => part.text)
    .join("\n")
    .trim();
  if (!typed) return { ok: false, error: "Type what you want done; pasted text is only read." };
  if (typed.length > MAX_TYPED_CHARS) {
    return { ok: false, error: `Keep what you type under ${MAX_TYPED_CHARS} characters.` };
  }
  if (pasted.length > MAX_PASTED_CHARS) {
    return { ok: false, error: `Paste at most ${MAX_PASTED_CHARS} characters.` };
  }
  return { ok: true, value: { key, zone, parts } };
}

export type ChatAnswer =
  | "queued"
  | "zone_mismatch"
  | "key_reused"
  | "already_ended"
  | "invalid"
  | "error";

const CONFLICTS: ReadonlySet<string> = new Set(["zone_mismatch", "key_reused", "already_ended"]);

/** Which of Fly's refusals a 409 is: another zone, a key already used for
 * other words, or a key whose turn has ended. A body that cannot be read is
 * an error, which is safe to send again. */
export function chatConflict(body: unknown): "zone_mismatch" | "key_reused" | "already_ended" | "error" {
  const status = typeof body === "object" && body !== null ? (body as { status?: unknown }).status : null;
  return typeof status === "string" && CONFLICTS.has(status)
    ? (status as "zone_mismatch" | "key_reused" | "already_ended")
    : "error";
}

/** A notice for the chat box. `newKey`: the next send is a new request,
 * with a key of its own. */
export type ChatNotice = { tone: "ok" | "warn" | "error"; message: string; newKey?: true };

/** The key last sent, and the parts it was sent with; null before the first
 * send, and once a notice asks for a new key. */
export type Sent = { key: string; parts: string } | null;

/**
 * The key a send uses (D1). A resend of the words the key was first sent
 * with keeps it, so a send that timed out finds its turn already stored,
 * whatever was typed and deleted in between. Other words, typed or pasted,
 * are another request, and get a key of their own.
 */
export function keyToSend(
  sent: Sent,
  parts: string,
  fresh: () => string,
): { key: string; sent: NonNullable<Sent> } {
  if (sent !== null && sent.parts === parts) return { key: sent.key, sent };
  const key = fresh();
  return { key, sent: { key, parts } };
}

/** What the page remembers it sent, after Fly's answer: nothing once the
 * answer asks for a new key; after a timeout, the key, so a resend finds
 * its turn. */
export function afterNotice(sent: Sent, notice: ChatNotice | null): Sent {
  return notice?.newKey ? null : sent;
}

export function describeChat(answer: ChatAnswer): ChatNotice {
  switch (answer) {
    case "queued":
      return { tone: "ok", message: "Asked. The answer appears here.", newKey: true };
    case "key_reused":
      // The words changed under the key, or retention has cleared the words
      // first sent with it: either way, a new key asks afresh.
      return {
        tone: "warn",
        message: "That send could not be matched to the first one. Please send it again.",
        newKey: true,
      };
    case "already_ended":
      // A send that timed out was stored after all, and its turn has ended.
      return {
        tone: "warn",
        message: "That was asked already, and its answer is above. To ask again, send it again.",
        newKey: true,
      };
    case "zone_mismatch":
      return {
        tone: "error",
        message: "The web app's zone is not the agent's: set OWNER_TIMEZONE to its USER_TIMEZONE.",
      };
    case "invalid":
      return { tone: "warn", message: "That could not be asked. Type it again." };
    default:
      return {
        tone: "error",
        message: "The agent could not be reached. Send it again: it will not be asked twice.",
      };
  }
}

/** A turn the page is waiting on: re-read every two seconds, then every ten
 *  after thirty (D1). Idle when nothing is open. */
export function chatRefreshEvery(openSince: Date | null, now: Date): number | null {
  if (openSince === null) return null;
  return now.getTime() - openSince.getTime() < 30_000 ? 2_000 : 10_000;
}

export type TurnStatus = "queued" | "running" | "answered" | "stopped" | "failed";

export const OPEN: readonly TurnStatus[] = ["queued", "running"];

/** The turn the page waits on, if any: the box is locked until it ends (D1). */
export function openTurn<T extends { status: TurnStatus }>(turns: readonly T[]): T | null {
  return turns.find((turn) => OPEN.includes(turn.status)) ?? null;
}

/** A stored answer, read defensively: Fly writes it, but it is JSON. */
export type StoredAnswer = { text: string; allow: string[]; cards: string[] };

export function readAnswer(value: unknown): StoredAnswer | null {
  if (typeof value !== "object" || value === null) return null;
  const answer = value as Record<string, unknown>;
  if (typeof answer.text !== "string") return null;
  const strings = (list: unknown): string[] =>
    Array.isArray(list) ? list.filter((item): item is string => typeof item === "string") : [];
  return { text: answer.text, allow: strings(answer.allow), cards: strings(answer.cards) };
}

/** What a turn without an answer shows. */
export function statusWords(status: TurnStatus): string | null {
  switch (status) {
    case "queued":
      return "Waiting to start…";
    case "running":
      return "Working on it…";
    default:
      return null;
  }
}

/** An address from a paste, for its Allow: no proposal is named. */
export function parseAllowAddress(data: FormData): Parsed<{ address: string }> {
  const address = String(data.get("address") ?? "").trim();
  if (!isAddress(address)) return { ok: false, error: "That is not an email address." };
  return { ok: true, value: { address } };
}

export function describeChatAllow(
  answer: "allowed" | "invalid" | "error",
  address: string,
): ChatNotice {
  switch (answer) {
    case "allowed":
      return { tone: "ok", message: `Allowed ${address}. Ask again, typing or naming them.` };
    case "invalid":
      return { tone: "warn", message: "That is not an email address." };
    default:
      return { tone: "error", message: "It could not be allowed. Try again in a moment." };
  }
}
