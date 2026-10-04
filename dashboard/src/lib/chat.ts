/**
 * The chat page's rules (M19, D1, D5 and D7). No framework imports, so
 * `node --test` runs them directly.
 *
 * The browser knows what was pasted: a mark beside each character of the box
 * says whether it arrived by a paste or a drop. Fly is sent the text in parts,
 * each typed or pasted, so typed and pasted are never guessed from the words,
 * and no character index has to agree between JavaScript and Python.
 */

/** A run of the box's text, typed or pasted. Fly's `ChatPart`. */
export type ChatPart = { text: string; pasted: boolean };

import { isAddress } from "./guests.ts";

export const MAX_TYPED_CHARS = 2_000;
export const MAX_PASTED_CHARS = 16_000;
export const MAX_PARTS = 50;

/**
 * The marks after one change to the box: the characters before and after the
 * change keep theirs, and those it put in get the change's. A change is found
 * by what the old and new text share at each end; where it could be placed
 * more than one way among identical characters, the typed and the pasted text
 * come out the same either way.
 */
export function remark(
  before: string,
  marks: readonly boolean[],
  after: string,
  pasted: boolean,
): boolean[] {
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) {
    start += 1;
  }
  let endBefore = before.length;
  let endAfter = after.length;
  while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) {
    endBefore -= 1;
    endAfter -= 1;
  }
  const kept = marks.slice(0, before.length);
  return [
    ...kept.slice(0, start),
    ...new Array<boolean>(endAfter - start).fill(pasted),
    ...kept.slice(endBefore),
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

export type ChatAnswer = "queued" | "zone_mismatch" | "invalid" | "error";

type Notice = { tone: "ok" | "warn" | "error"; message: string };

export function describeChat(answer: ChatAnswer): Notice {
  switch (answer) {
    case "queued":
      return { tone: "ok", message: "Asked. The answer appears here." };
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

export function describeChatAllow(answer: "allowed" | "invalid" | "error", address: string): Notice {
  switch (answer) {
    case "allowed":
      return { tone: "ok", message: `Allowed ${address}. Ask again, typing or naming them.` };
    case "invalid":
      return { tone: "warn", message: "That is not an email address." };
    default:
      return { tone: "error", message: "It could not be allowed. Try again in a moment." };
  }
}
