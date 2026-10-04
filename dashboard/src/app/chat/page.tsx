import Link from "next/link";

import { ChatAllow } from "@/components/ChatAllow";
import { ChatBox } from "@/components/ChatBox";
import { Refresher } from "@/components/Refresher";
import {
  chatRefreshEvery,
  openTurn,
  readAnswer,
  statusWords,
  type TurnStatus,
} from "@/lib/chat";
import { query } from "@/lib/db";
import { when } from "@/lib/format";
import { ownerZone } from "@/lib/timeline";

export const dynamic = "force-dynamic";

/** What `web_reader` may read of a turn (M19, D12): never the paste, nor the plan. */
type TurnRow = {
  id: string;
  status: TurnStatus;
  asked_at: Date;
  typed: string | null;
  answer: unknown;
  reason: string | null;
};

const RECENT = 20;

/**
 * Ask in your own words (M19, D1). Each request is a turn: Fly stores it, a
 * job answers it, and this page re-reads until it has. Answers are plain
 * text, written by code (D7): React shows them as text, and nothing in them
 * becomes markup or a link. A content policy lets the page load no image
 * from anywhere else (`next.config.ts`).
 */
export default async function ChatPage() {
  const zone = ownerZone(process.env.OWNER_TIMEZONE);
  const rows = await query<TurnRow>(
    `SELECT id, status, asked_at, typed, answer, reason
       FROM chat_turns ORDER BY asked_at DESC, id DESC LIMIT ${RECENT}`,
  );
  const turns = rows.toReversed();
  const open = openTurn(turns);
  const every = chatRefreshEvery(open ? new Date(open.asked_at) : null, new Date());

  return (
    <main className="chat">
      <h1>Ask</h1>
      <p className="muted">
        Questions about your mail and calendar, and events to propose. Text you paste is read,
        never acted on.
      </p>
      <Refresher every={every} />
      <ol className="turns">
        {turns.map((turn) => {
          const answer = readAnswer(turn.answer);
          const waiting = statusWords(turn.status);
          return (
            <li key={turn.id} className="turn">
              <p className="asked">
                <span className="muted">{when(turn.asked_at, zone)}</span>{" "}
                {turn.typed ?? <span className="muted">(cleared)</span>}
              </p>
              {waiting && <p className="muted">{waiting}</p>}
              {answer && <p className="answer">{answer.text}</p>}
              {turn.reason && <p className="answer reason">{turn.reason}</p>}
              {answer?.allow.map((address) => <ChatAllow key={address} address={address} />)}
              {answer && answer.cards.length > 0 && (
                <p>
                  <Link href="/" prefetch={false}>
                    The card is on the timeline
                  </Link>
                </p>
              )}
            </li>
          );
        })}
      </ol>
      <ChatBox locked={open !== null} />
    </main>
  );
}
