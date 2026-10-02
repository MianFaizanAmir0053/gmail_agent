"use server";

import { revalidatePath } from "next/cache";

import { auth } from "@/auth";
import { isOwnerSession } from "@/lib/access";
import {
  describeAllow,
  describeAnswer,
  describeSwitch,
  describeWithdraw,
  parseAllowForm,
  parseDecisionForm,
  parseSwitchForm,
  parseWithdrawForm,
  type Notice,
} from "@/lib/decisionForm";
import { postContact, postDecision, postSwitch, postWithdraw } from "@/lib/fly";

/**
 * A tap on a card: Confirm, Edit or Cancel.
 *
 * The owner is checked here, first. A Server Function is a POST to the page's
 * route, and Next documents that the proxy can miss one, so this check is the
 * one that holds. Fly then decides whether the decision stands; the answer
 * comes back as words, never a raw error.
 */
export async function decideFromCard(_previous: Notice | null, data: FormData): Promise<Notice> {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    return { tone: "error", message: "Sign in as the owner first." };
  }

  const parsed = parseDecisionForm(data);
  if (!parsed.ok) {
    return { tone: "warn", message: parsed.error };
  }

  const notice = describeAnswer(await postDecision(parsed.value));
  // Whatever the answer, the card should show what is true now: "Applying…",
  // or the latest version of a proposal that moved.
  revalidatePath("/");
  return notice;
}


/**
 * Allow on a card: a guest outside the email's thread may now be invited
 * (M17, D4). The owner is checked first, as for a decision; Fly records the
 * contact, and the card re-reads so the guest is no longer marked.
 */
export async function allowFromCard(_previous: Notice | null, data: FormData): Promise<Notice> {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    return { tone: "error", message: "Sign in as the owner first." };
  }

  const parsed = parseAllowForm(data);
  if (!parsed.ok) {
    return { tone: "warn", message: parsed.error };
  }

  const notice = describeAllow(await postContact(parsed.value), parsed.value.address);
  revalidatePath("/");
  return notice;
}

/**
 * Withdraw on a queued card (M17, D6). Fly records the request; the worker
 * carries it out, even while paused, or declines it if the decision is
 * already being applied. The card re-reads, and says which.
 */
export async function withdrawFromCard(_previous: Notice | null, data: FormData): Promise<Notice> {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    return { tone: "error", message: "Sign in as the owner first." };
  }

  const parsed = parseWithdrawForm(data);
  if (!parsed.ok) {
    return { tone: "warn", message: parsed.error };
  }

  const notice = describeWithdraw(await postWithdraw(parsed.value.decision_id));
  revalidatePath("/");
  return notice;
}

/**
 * Pause or Resume, from the header (M17, D6). Every page shows the banner,
 * so every page re-reads.
 */
export async function switchFromHeader(_previous: Notice | null, data: FormData): Promise<Notice> {
  const session = await auth();
  if (!isOwnerSession(session?.user?.email, process.env.OWNER_EMAIL)) {
    return { tone: "error", message: "Sign in as the owner first." };
  }

  const parsed = parseSwitchForm(data);
  if (!parsed.ok) {
    return { tone: "warn", message: parsed.error };
  }

  const notice = describeSwitch(parsed.value, await postSwitch(parsed.value));
  revalidatePath("/", "layout");
  return notice;
}
