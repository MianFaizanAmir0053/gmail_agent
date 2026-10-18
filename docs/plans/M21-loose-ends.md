# M21 · Loose ends

**Est.** 7 days · **Depends on** M16, M18, M20, and M19's reader · **Blocks** M23 (connectors feed loose ends), M24 (earned autonomy), M26 (money)

Plan and decisions: [`ASSISTANT-PLAN.md`](../../ASSISTANT-PLAN.md), sections
"Decisions" (the first proactive job), "Key assumptions to validate" and
"Security model". Evidence: [`../research/assistant-landscape-2026-09.md`](../research/assistant-landscape-2026-09.md),
"Unmet needs" (closure, not briefs) and "Proactivity".

**Status: approved by the owner on 2026-10-18, after five rounds of decisions
and two fresh-context reviews whose findings are folded in, with the owner's
decisions 24 to 26 (see Review). The tasks are 21.1–21.28 in
[`tasks/todo.md`](../../tasks/todo.md); a review of them settled the
clarifications in the running notes.**
- **The build begins only after M15's mail measurement says go** (M15, B4: at least 5 loose ends a week, counted cautiously). A no-go re-plans M21.
- **Its model runs need a paid-tier Gemini key,** as M18's and M19's do. The plan never sends real mail through a free-tier key. M21 also changes files in M18's and M19's hashed sets (Deliverables). Those committed runs need the same key; one made before M21's changes is made again after them, and its CI gate fails until it is.

## Goal

Every working morning, the owner sees what they owe and what they promised,
and each item closes by itself when it is done:
- **replies owed:** someone asked something of the owner by email, and the owner has not answered in 48 hours;
- **promises made:** the owner wrote, in mail they sent, that they would do something for someone;
- **tracked to closure:** the owner's reply closes a reply owed, and the owner's later message that delivers a promise closes it, with a Reopen for a wrong close;
- **with receipts:** each item links to its email, and the page shows how accurate each kind has been.

**What M21 cannot promise.**
- It sees only this Gmail account. An ask answered by phone or WhatsApp stays open until the owner taps Done, and the digest says what it does not cover.
- A "reply owed" rests on the sender's words, which they control. Nothing in M21 acts on them: the list only reminds.
- A close judged by a model can be wrong. A Reopen undoes it, and counts against the accuracy the page shows.
- Telling the owner's words from quoted text is done by structure and by matching. A mail client whose quoting it does not recognise can still slip quoted text in; D4's matching guard and the injection suite bound that.

## Why this comes next

The code as of `3ec4772`, mapped on 2026-10-18 and checked by both reviews.

**The mailbox is already seen** (M20). `gmail_messages` keeps every Primary, Updates, Forums and sent message as metadata: the thread, Gmail's own time (`internal_at`), the labels, the direction, the category, From, To, Cc and the bulk signals.
- It never keeps a subject, a snippet, a body, `Reply-To` or `Authentication-Results`.
- `to_self` is true when the owner sent the message and *any* To address is theirs.
- A SCHEDULED message counts as outbound.
- Freshness:
  - the cursor's `caught_up_at` says a pass reached the end of history;
  - `gap_from` marks a gap still being worked off after a catch-up;
  - `backfill_until` says how far back the backfill has reached;
  - `gmail_fetch_queue` holds messages that failed to fetch, with no thread id.
- The feed's row rule (`feed.ROW`) admits mailing lists that Gmail files as personal; M15's rule does not.

**The loose-end rule exists** (M15). `app/jobs/measure.py`'s flagged thread is one whose first qualifying ask got no reply from the owner within 48 hours. Its qualifying ask is inbound, labelled `CATEGORY_PERSONAL`, and not automated. Automated means:
- a List-Unsubscribe header;
- Auto-Submitted other than `no`;
- Precedence `bulk`, `list` or `junk`;
- a no-reply or calendar sender, an excluded sender, or a calendar Subject.

Its labelling keeps counts only, and its module keeps every id to itself.

**A reader exists** (M19). `app/planner/reader.py` reads through M18's preparation and holds no tool. But:
- its questions are keyed by `Ask`, the planner's own schema, which is hashed into the routing baseline;
- its block includes From, To and the Subject;
- its question rides in the system instruction;
- `EmailMessage` keeps no headers and no HTML;
- `prepare` sets mail aside as credential mail on its Subject or anywhere in its body.

**Gmail's verdict on a sender's domain** is read from `Authentication-Results` (`participants.dmarc_passed`), as M19's search does.

**Outages are named:** `gmail.is_outage` covers 5xx, 429, network and refresh errors, apart from the model's `UnavailableError`.

**Spending is metered per message** (M17), with a monthly cap, a per-message ceiling, and chat's daily cap as the pattern for another. `CLASSIFY_MODEL` may be set to an evaluation-only model, which cannot answer in a schema.

**A confirmed meeting** is a proposal whose `final_status` is `created`. Its `action_type` is `calendar_hold` or `calendar_invite`, and its guests are in its payload until the purge clears it.

**Pushes carry fixed text** (M16):
- Each tag only decides which notification replaces which.
- A tap always opens `/` (`dashboard/public/sw.js`).
- Every alert built from `ALERT_PUSH` shares the tag `alert`.

**Eval results are published** from `results/` into Postgres (`app/eval/publish.py`), keyed by file name.

**There is no record of loose ends,** and no page or digest for them.

## Decisions (18 Oct 2026)

The owner took the recommended choice for each, over five rounds. The fifth followed the first review; the sixth question, 26, followed the second.

1. **Kinds:** replies owed, and promises the owner made. Promises owed to the owner, deadlines and money (M26) wait.
2. **Order:** this spec now; the build after M15's mail measurement. *(Its second half, the labelling also starting M21's test set, is replaced by 24.)*
3. **Surface:** a Loose ends page, and one digest at 9:00 on working days (`WORKING_HOURS`). It is skipped when nothing is new, and it ends with what it does not cover.
4. **No other pushes.**
5. **An ask from someone else** joins the list by itself as "they asked", with the sender's address and a quote. A dismissal counts toward accuracy. A promise is "you promised": the owner's own words.
6. **Chat does not answer about loose ends** in M21.
7. **A reply is owed 48 hours after the ask**, unanswered: M15's clock.
8. **Candidates come from M15's rule;** the reader confirms each once, at 48 hours, and quotes the ask.
9. **A reply owed closes** on the owner's reply in the thread, or their Done or Dismiss.
10. **Promises:** a model reads the owner's own words in each email they send, never quoted or forwarded text.
11. **The first run** looks back 14 days.
12. **Actions:** Done, Dismiss, Snooze until a date, and Mute sender.
13. **The digest** holds up to 10 items, then "and N more". *(What comes first is set by 26.)*
14. **A promise closes by itself** when a later message of the owner's in the same thread delivers it: "closed: … (your message on <date>)", with a one-tap Reopen. A promise kept in another thread waits for Done.
15. **Due dates:** a stated one is worked out in `USER_TIMEZONE` from when the email was sent. "Next week" is that week's last working day. None is 7 days after sending.
16. **A promise** is anything the owner said they would do for someone. Pleasantries ("talk soon", "let's catch up sometime") are not.
17. **Stored text:** a quote of up to 200 characters, scrubbed as M18 scrubs, with the sender's address. It is kept while the item is open and cleared a week after it closes. `web_reader` may read that column.
18. **Spending** is mail's, with a daily cap for M21 of $0.25. *(The first run's budget and the models are set by 25.)*
19. **The test set** gates at 90% precision for each kind, on what the list shows as open and on its automatic closes, with 95% intervals; recall is reported. *(Its make-up is replaced by 24.)*
20. **Exit:** two weeks of digests on the owner's real mail, with dismissals and Reopens under 20% of the items shown, and the test-set bar.
21. **The page shows each kind's accuracy:** the test set's last score, and the last 30 days' dismiss and reopen rates.
22. **A confirmed meeting closes the thread's reply owed** once the invite is created: not in dry run, and not on a Cancel.
23. **One item per thread** for replies owed. A new ask after the owner's reply reopens it: "they asked again".
24. **M21's own labelling, by its own rule** (replaces 2's second half and 19's make-up). It runs after the rules are built and before any model feature:
    - replies owed: 40 candidates, and 20 threads it would close on the owner's reply ("did that message answer them?");
    - promises: sent emails, sampled until 30 promises turn up.

    The 90% bar is judged only with at least 30 open items and 15 closes per kind; fewer means label more. M15's measurement stays exactly as M15 specifies, and is the go/no-go.
25. **The models and the first run** (sets 18's remainder):
    - the "does it ask you something?" check runs on the cheap model, reading at most 6,000 characters; promises and their closes run on `PLANNER_MODEL`;
    - the daily cap stays $0.25;
    - asks from people the owner has written to before are read first, and at most 3 reads a sender a day;
    - a fixed-text alert fires when anything waits more than a day;
    - the first run has a one-off $1.50 budget, so it finishes in its first day.
26. **What the digest shows first** (sets 13's order): new items first, marked "new". Then the most overdue of the rest fill the ten. The push fires only for new items, due days reached and automatic closes, never for the owner's own taps.

**Defaults set with this spec, open at the owner's word:**
- Mail the owner sends only to themselves is never a promise, and never closes a reply owed.
- A snoozed item returns in the digest on its day.
- Muted addresses are listed on the page, each with an Unmute. Unmute only stops leaving that sender out: what Mute closed stays closed, with a Reopen while its quote is kept.
- **A Dismiss is for one ask, a Mute for a sender.** A later ask in the thread reopens the item as "they asked again", unless its sender is muted.
- **The digest's push says only "Your loose ends are ready."** Like every push here (M16), it carries no email text. It opens the page, whose top is the digest.
- **The day's digest is judged once,** at the first run at or after 9:00 on a working day. A 9:00 missed because the laptop slept is judged at the first run after it that day, before the end of `WORKING_HOURS`. Never two in a day.
- **An ask whose email the owner deleted** closes as "the email was deleted", shown among the closes. It does not count against accuracy.
- An injection suite gates CI (D12).

## Scope

**In:**
- the records: items, their events, the messages read and checked, the muted senders, the digests, the state, the published scores;
- replies owed: candidates from M20's records by M15's rule, confirmed and quoted by the reader, closed by the owner's reply to the asker, a created invite to the asker, Done or Dismiss;
- promises: found in the owner's own words in sent mail, dated, closed by a later delivering message in the thread;
- the job that finds and closes, and the job that sends the digest;
- the page, with its actions and accuracy, and Fly's API for the actions;
- spending: the models, a daily cap, the first run's budget, the order of reads, and an alert;
- retention for quotes and addresses;
- the test sets: the owner's real mail, labelled locally after the rules exist; and invented fixtures in CI, with an injection suite.

**Out:**
- promises owed to the owner, deadlines, and money (M26);
- drafting or sending replies, and any write to Gmail (labels, archive);
- chat questions about loose ends (decision 6);
- other sources: other accounts, Slack, the connectors (M22, M23);
- a promise kept in another thread (Done for now);
- pushes beyond the digest and the "behind" alert (decisions 4 and 25);
- autonomy of any kind (M24).

## Design

### D1. The records

Migration `014_loose_ends.sql`:
- **`loose_ends`**, one row per item:
  - `kind`: `reply_owed` or `promise`;
  - `thread_id`;
  - `message_id`: the current ask, which moves on "asked again" (D3), or the email the promise is in;
  - `position`: a promise's place in its email, from 0; always 0 for a reply owed;
  - `counterpart`: the asker's address, or the promise email's first To recipient, as `guest_key` spells it;
  - `reply_to`: the ask's Reply-To address, read with the ask (D2);
  - `sender_verified`: Gmail's verdict on the asker's domain (D2); null for a promise;
  - `status`: `open`, `snoozed` or `closed`; `snooze_until`, a day;
  - `due_on`: a promise's due day, in `USER_TIMEZONE`;
  - `ask_at`: Gmail's time for the ask, or for the promise's email;
  - `closed_by`: `reply`, `meeting`, `delivered`, `done`, `dismissed`, `muted` or `gone`; with `closed_at`, `closing_message_id` and `closing_at`;
  - `reopened_at`: the last Reopen. No message at or before it closes the item again (D3, D5);
  - `quote`, `cleared_at` (when the quote, counterpart and `reply_to` were cleared), `opened_at` and `updated_at`.
  - Unique on `thread_id` among replies owed (decision 23), and on `(message_id, position)` among promises.
- **`loose_end_events`**, append-only: the item (null for Unmute), the event, its `closed_by` where it is a close, when, and by what (`job`, `web`, `cli`). No text, no address. The events are `opened`, `asked_again`, `closed`, `reopened`, `done`, `dismissed`, `snoozed`, `unsnoozed`, `muted` and `unmuted`.
- **`loose_end_reads`**, keyed by message and question (`reply_owed` or `promises`): the verdict, the strikes, the model, and when. No text. The verdicts are `ask`, `no_ask`, `promises`, `none`, `credential`, `unreadable` and `skipped`.
- **`loose_end_checks`**, keyed by promise item and later message: D5's verdict (`delivered`, `not_delivered`, `unreadable`), the strikes, and when.
- **`muted_senders`**: the address as `guest_key` spells it, and when.
- **`loose_end_digests`**, keyed by the owner's day: when it was judged, its outcome (`sent`, `nothing_new` or `push_failed`), and the ids of the items it showed, marked new or not, and of the closes. Ids only.
- **`loose_end_state`**, one row: `started_at`, the instant M21 first ran; the first run's spend; and `first_run_ended_at`.
- **`loose_end_scores`**: the published test-set scores (D11). Each row holds the kind, the measure, yes and of, the interval, the models, the shaping hash, the commit and the results file.
- **Grants:** `web_reader` may read `loose_ends` (the quote, the counterpart and `reply_to` included: decision 17), the events, the muted senders, the digests and the scores. It may not read the reads, the checks or the state.

### D2. Replies owed: found

**When anything is read.** No ask is read, and nothing is closed or sent, unless the mailbox is current:
- the sync reached the end of history within 30 minutes;
- no gap is being worked off (`gap_from` is null);
- no fetch is queued: no row of `gmail_fetch_queue` with status `queued`, which has no thread to narrow it to. An `unreadable` fetch does not block; M20 reports it;
- until the first run ends, the backfill has reached 14 days before `started_at` (`backfill_until`).

A reply the owner sent from the phone during an outage is stored before any ask it answered is read.

**A candidate is an inbound message that meets M15's rule** on the stored row:
- labelled `CATEGORY_PERSONAL`;
- not to self, not gone, not in spam or trash;
- no List-Unsubscribe; Precedence not `bulk`, `list` or `junk`; Auto-Submitted absent or `no`;
- not from a no-reply or calendar sender, nor from a muted sender (the muted senders are M15's excluded senders here);
- addressed to the owner: To or Cc holds the profile address or an `OWNER_ALIASES` entry, normalised as M15's `normalise_address` does. Every other comparison of addresses in M21 (the asker, a Reply-To, recipients, mutes) uses `guest_key`.

And it must also be:
- **unanswered:** no SENT message of the owner's later in the thread holds its sender, or its Reply-To, in To or Cc;
- **48 hours old** (decision 7);
- **no earlier than 14 days before `started_at`** (decision 11);
- **not yet read** for this question.

M20 stores no Subject, so M15's calendar-Subject check cannot run. The reader's instruction says that invitations are not asks.

**The order within a thread** (decision 23 and the defaults):
- While the thread's item is open or snoozed, nothing else in it is read. Follow-ups and other asks wait.
- Once it is closed (by anything but `gone`), the thread's remaining candidates are read in order, each once. The first read as an ask reopens the item as "they asked again" (D3). A second person's ask that the owner's reply to the first did not answer is one of these.
- A thread with no item reads its candidates in order. A "Thanks!" read as no ask leaves the next as the candidate.

**The read.** M21 has its own reader entry point and questions (`app/loose_ends/reader.py`), not M19's `Ask`, so the planner's schema and its routing baseline are untouched.
- **A fetch of its own** (in `app/google/gmail.py`) keeps the HTML part's structure and the two headers M21 needs, `Reply-To` and `Authentication-Results`.
- **What is read:** the ask's own body only, through M18's preparation. That means no Subject and no headers, the own part (D4's extraction) between fresh markers, and at most 6,000 characters. The credential check runs on that part alone, so a Subject that mentions a code does not set aside every reply in its thread.
- **The model** is `LOOSE_ENDS_CHECK_MODEL`, defaulting to `gemini-3.5-flash-lite`, the cheap model of decision 25. An evaluation-only model is refused at startup.
- **The instruction is fixed.** The only thing in it from code is the owner's own addresses, so the reader knows which recipient is the owner. It asks: "Does this message ask the owner for something they have not given: an answer, a decision, a document, an action? A calendar invitation, an RSVP, a receipt, a notification and thanks are not asks. Put the ask in points as one short sentence; leave points empty if it asks the owner nothing."

**What the read makes:**
- **An ask** makes the item `open`, or reopens it (D3), with:
  - the sentence as its quote, cut to 200 characters by the renderer's rules;
  - `reply_to`, and `sender_verified` from the message's own `Authentication-Results`, as `participants.dmarc_passed` reads it;
  - the asker's address, checked as one before it is stored.
- **No ask** records `no_ask`, and makes nothing.

**The quote** is shown in quotation marks under the sender's address and Gmail's verdict ("domain verified by Gmail" or "not verified"), never a display name (M19, D7).

### D3. Replies owed: closed

Every run, in code. Every change is conditional on the item's state as read, so an update that finds it changed does nothing.
- **The owner's reply** closes it, `reply`. A reply is a message in the thread that:
  - is after the ask and after any Reopen;
  - carries the SENT label (so not SCHEDULED, and not a draft);
  - is not only to the owner: some To or Cc address is not theirs;
  - holds the asker, or the ask's Reply-To, in To or Cc.
  
  A forward to someone else, or a note to self, closes nothing.
- **A confirmed meeting** (decision 22) closes it, `meeting`. That is a mail proposal with `action_type` `calendar_invite` and `final_status` `created`, parked from one of these:
  - the ask itself;
  - a later message in the thread whose invite's guests include the asker, read from the proposal's payload before the purge clears it.
  
  A hold, a dry run, a Cancel, a Withdraw, an expiry, and an invite without the asker close nothing.
- **The ask gone** closes it, `gone`: its row carries the SPAM or TRASH label, or `gone_at` is set. A row missing because M20's purge deleted it is never `gone` (D10). It is shown among the closes ("the email was deleted"), and does not count against accuracy.
- **The owner's Done, Dismiss or Mute** close it (D8).
- **Asked again** (decision 23): a later ask read as such (D2) reopens the item as `asked_again`. One update:
  - moves `message_id`, `counterpart`, `reply_to`, `sender_verified`, `ask_at`, `quote` and `opened_at` to the new ask;
  - resets the `closed_*` fields and `cleared_at`.
  
  A group thread then names who asked this time.
- **A Reopen** (D8) sets `reopened_at`. No message at or before it closes the item again.

### D4. Promises: found

**Which messages:** every message the sync stores with direction `out` and the SENT label (not SCHEDULED, not a draft) whose To or Cc holds an address that is not the owner's, no earlier than 14 days before `started_at`. Each is read once, through the same fetch as D2's.

**What is read: only the owner's own words, found in three steps.**
1. **Structure.** From the HTML part, the quote containers of the clients the owner uses are removed before flattening:
   - Gmail's `gmail_quote` and `gmail_attr`;
   - Apple Mail's `blockquote type="cite"` and the attribution block beside it;
   - Outlook's reply header block and what follows its separator;
   - any `blockquote`.
2. **Text.** The flattened text, or the text part where there is no HTML, is cut at the first quote marker, attribution line, "Original Message" line, or From/Sent/To/Subject header block. The attribution line is matched with no length limit, in the languages the cleaner knows.
3. **Matching.** Any line of what is left that also appears in the body of an earlier inbound message in the thread is dropped: it was quoted, whatever the client. A reply of which half or more is dropped this way is not read for promises, and is recorded `unreadable`.

The Subject and the headers are never read. The credential check runs on what is read. Credential mail the owner sends (a code they share) is set aside unread, recorded `credential`.

**The read** runs on `PLANNER_MODEL` (decision 25), with its own schema: up to 5 promises, each one short sentence, and its due day as `YYYY-MM-DD`, or empty. The fixed instruction gives:
- the day the email was sent, the owner's time zone and their working days;
- decision 15's rule: "next week" is that week's last working day;
- decision 16's: pleasantries are not promises.

**Due days are checked in code.** A day between the day sent and a year later is used. Anything else, or none, becomes 7 days after the day sent.

**Each promise is an item:** `open`, with its sentence as the quote (cut to 200 characters, and scrubbed), the first To or Cc recipient that is not the owner's as the counterpart (or none), the email's time as `ask_at`, and its order as `position`.

### D5. Promises: closed

**The check.** When the owner sends again in the thread (SENT, after the promise's email, and after any Reopen), the job asks once about that message, for all the promises still open in the thread together: "For each promise below, does the message deliver it?"
- Each promise and the message's own part (read as D4 reads) go in the user turn, each between fresh markers of its own, as M18's `proposal_block` does for an Edit.
- The instruction carries no text from mail.
- The answer is typed: yes or no for each promise, by its number. A number that was not asked about is ignored.
- Only promises made in earlier messages are asked about. A credential message is not checked.

**The close:**
- **Yes** closes that promise, `delivered`, with the message's id and time. The next digest shows "closed: «the promise» (your message on <date>)", with a Reopen.
- **A Reopen** sets `reopened_at`, so neither that message nor any before it closes the promise again.

**Overdue** is open with `due_on` before the owner's today. **Due today** is `due_on` equal to it.

### D6. The jobs

**Both jobs run only with `LOOSE_ENDS_ENABLED`** (default off, as `INGEST_ENABLED` is): a deploy before the build is done reads no mail, and `started_at` is set by the first run with it on.

**`loose_ends`, every 15 minutes**, one run at a time under an advisory lock, a command-line run included. Each run:
1. **Waits for a current mailbox** (D2): nothing is closed or read otherwise. The run records in `job_runs` whether it began on one.
2. **Closes what closed, in code** (D3, and D5's code part), and ends snoozes whose day has come, recording `unsnoozed`.
3. **Reads, within the caps,** at most 10 reads a run, in this order:
   1. asks from people the owner has written to before (an outbound message to them in M20's records);
   2. promise reads and delivery checks;
   3. other asks.
   
   Oldest first within each. At most 3 ask reads a sender a day; the limit is for inbound asks only.
4. **Checks before each read:** Pause, new work and the monthly cap (M17), and the daily cap (D9). A refusal ends the run, and the next run tries again.

**A failed read** charges a strike, unless the failure is an outage: the model's (`UnavailableError`) or Gmail's (`is_outage`). Three strikes record `unreadable`. `python -m app.loose_ends --retry-unreadable` clears them, as M20's `--retry` does.

**The first run** reads the 14-day backlog with a one-off budget of $1.50, in place of the daily cap (decision 25):
- **Order:** threads newest first, each thread's candidates in order.
- **Limit:** at most 200 reads. Candidates beyond them are recorded `skipped` and never read.
- **Its end:** when the $1.50 is spent, or no backlog candidate is left that it can read. Candidates waiting behind an open item, or held by the day's per-sender limit, do not count. It may span days, and the budget does not reset at midnight. After it, the daily cap applies.

**The "behind" alert** is its own alert code, `loose_ends_behind`, sent once a day with the fixed text "Loose ends are behind on reading mail". It fires when a candidate has waited more than a day for its read, counted from the later of when it qualified (an ask 48 hours after it arrived, a sent email when it was sent) and the first run's end. It is judged on every run, even when the gate stops reads, and sent at most once a day (`alerts_sent`). It has its own tag and Telegram words, so it never replaces another alert, such as the sign-in one.

**`loose_ends_digest`, every 15 minutes:**
- **The day is judged once,** at the first run at or after 9:00 on a working day, in `USER_TIMEZONE`, before the end of `WORKING_HOURS`, and after a `loose_ends` run that began on a current mailbox. A row is written whatever the outcome, so the day is never judged twice.
- **New since the last digest that went out** (decision 26), read from the events by their sequence ids, so that nothing committed while a digest is made is lost:
  - an item opened or asked again;
  - a promise falling due today, or turning overdue since;
  - a snooze that ended;
  - an automatic close (`reply`, `meeting`, `delivered`, `gone`).
  
  The owner's own taps (Done, Dismiss, Mute, Snooze, Reopen) are never new.
- **Nothing new:** the row says `nothing_new`, and no push goes out.
- **Something new:** the row is written with its ids first, then the push goes out, its own alert code (`loose_ends_digest`), tag and Telegram words. The row is `sent` when at least one channel delivered it. When none did, it is `push_failed`: the push is not retried that day, and its news carries over to the next digest.
- **The service worker** maps each tag to a page (`loose-ends` and `loose-ends-behind` both open `/loose-ends`). It never takes a URL from a push.

### D7. The digest

Composed by code from the records, never by a model, and stored by item id on its day's row:
- **Up to 10 open items** (decisions 13 and 26). New ones come first, marked "new", most overdue first among them. Then the most overdue of the rest. A promise's lateness is the days past its due day; a reply owed's is the time past its 48 hours. Then "and N more".
- **The automatic closes since the last digest,** up to 5, then "and N more closed". Each `delivered` close has a Reopen beside it, and a `gone` close says "the email was deleted".
- **The coverage line:** "This covers this Gmail account only: not WhatsApp, calls or other inboxes."

### D8. The page and the API

**`/loose-ends`**, "Loose ends" in the menu:
- **Today's digest:** its stored ids, rendered now.
- **Every open item,** grouped: late, due, replies owed, snoozed. Each shows:
  - "they asked" or "you promised";
  - the quote under the sender's address and Gmail's verdict;
  - the ask's age (from `ask_at`) or the due day;
  - a link to the thread in Gmail.

**Actions:**
- **Done** and **Dismiss** close the item. A Dismiss is for this ask: a later ask in the thread reopens it, unless its sender is muted.
- **Snooze:** tomorrow, next week (the next week's first working day), or a date.
- **Mute sender,** on a "they asked". It closes that sender's open replies owed, `muted`, counts as a dismissal for accuracy, and leaves their mail out from then on. **Unmute** stops leaving them out. What it closed stays closed, with a Reopen while its quote is kept.
- **Reopen,** on a closed item, while its quote is kept (7 days). The page lists what closed in the last 7 days, each with a Reopen.

**Every action:**
- is owner-only: the server action checks the owner, then calls Fly's API with `WEB_API_SECRET`;
- is conditional on the item's state, so Done on a closed item changes nothing, and between the job's close and the owner's Dismiss on one item, the first wins;
- records an event.

**Accuracy** (decision 21), for each kind:
- the last published test-set score (`loose_end_scores`), with its models;
- **the dismiss rate:** items dismissed or muted, over items opened, in the last 30 days;
- **the reopen rate:** Reopens of automatic closes, over the automatic closes, in the last 30 days. `gone` is left out of both, and a Reopen of the owner's own close never counts.

Under 10 in a denominator, only the counts show.

**Muted senders,** each with Unmute.

**Plain text:** quotes are React text, under the content policy every page sends.

### D9. Spending

- **Metering:** each ask or promise read is metered under `loose-<message_id>`, and each delivery check under `loose-<the later message's id>`, through M17's metered path. It counts toward the monthly cap and against the mail reserve, as mail does.
- **The ask check** runs on `LOOSE_ENDS_CHECK_MODEL` (`gemini-3.5-flash-lite`: $0.30 and $2.50 per million tokens, no 2027 rise), with at most 6,000 characters of body: about $0.001 a read.
- **Promises and delivery checks** run on `PLANNER_MODEL` (`gemini-3.6-flash`: $1.50 and $7.50 from 1 Jan 2027): about $0.005 to $0.01 a call at 2027 rates. One check covers every open promise in the thread.
- **`LOOSE_ENDS_DAILY_CAP_USD`,** default `0.25`, summed over `loose-` ids in the owner's day. Reaching it stops reads until the next day; the code rules go on.
- **`LOOSE_ENDS_FIRST_RUN_USD`,** default `1.50`: the first run's budget, in place of the daily cap.
- **The estimate:** after the first run, about $0.10 to $0.17 a day at a few dozen reads. D11 measures it on the owner's mail.
- **An unpriced model** is never called. `LOOSE_ENDS_CHECK_MODEL` counts among the models in use (`models.in_use()`), so an unpriced one stops new work, as any model does.
- **The eval** meters its calls under a prefix of its own, never `loose-`, so it never spends production's daily cap.

### D10. Retention

- **A week after an item closes,** the hourly purge clears its quote, counterpart and `reply_to`, and sets `cleared_at`. The item keeps its kind, status, times and `closed_by`.
- **A Reopen** is offered only while the quote is kept.
- **Kept, with no text and no address:** the events, the reads, the checks, the digests' ids and the scores.
- **A muted sender** is kept while muted; Unmute deletes the row.
- **M20's purge** deletes mail rows at 180 days, or a week after they are gone. An item keeps its own `thread_id`, `ask_at` and `closing_at`, so the page needs none of those rows. An item still open when its ask's row is purged stays open until the owner acts.

### D11. The test sets

**The order** (decision 24, and the plan's golden-set-first rule):
1. Build and test the rules (D2, D3, which messages D4 reads and D4's extraction, D6) in code, with no model.
2. The owner labels.
3. Then the reads are built and scored against the labels.

**The labeller,** `.\tasks.ps1 loose-ends label`, runs in the owner's terminal only (`require_terminal`), and only on a current mailbox. It paces its own Gmail reads, at most 30 a minute, since it shares the per-user quota with the sync. Its window is the last 90 days, which M20's backfill holds, using D2's and D4's rules with that window instead of `started_at`'s 14 days. It can be stopped, resumed and extended, and never labels a message twice. It shows each message's own part, as D2 and D4 read it, and asks:
- **Replies owed:**
  - for 40 candidates drawn by D2's rule (seeded): "Did it ask you for something? Was it still unanswered 48 hours later?";
  - for 20 threads where D3's rule closes an ask the owner answered more than 48 hours after it came, found in code with no model: "Did that message answer them?".
- **Promises:** for sent emails sampled (seeded) until 30 promises turn up, 15 of them kept later in their thread, or the window ends: "Does it promise something? How many? When is each due? Was each kept later in this thread?".

**What is kept** goes to `data/loose_ends/labels.jsonl`, which is gitignored and dockerignored:
- message ids, and each labelled thread's messages up to the labelled point, so that the eval sees the thread as the owner labelled it;
- the owner's answers.

It never holds email text. It is deleted when the test set is retired (DEPLOY §15).

**`loose-ends-eval`** runs D2 to D5 on the labelled snapshots, fetching the messages from Gmail at the same pace. It refuses to run:
- unless `GEMINI_PAID_TIER=true`, so real mail never goes through a free-tier key;
- unless the mailbox is current.

**What it scores:**
- **Replies owed:**
  - the precision of what it shows as open, over the candidates it confirms;
  - the precision of its automatic closes (correct closes over closes made), over the labelled threads, meeting closes included where there are any;
  - recall.
- **Promises:**
  - the precision of "it lists a promise", per email;
  - its count of promises against the owner's, reported;
  - due days, exact;
  - the precision of its closes (correct `delivered` verdicts over those made), and their recall over the promises the owner says were kept.

**The bar** (decision 24) is a point estimate of at least 90%, for each kind on open items and on closes. It is judged only with at least 30 open items and 15 closes per kind; fewer is "not enough evidence", and the owner labels more. Wilson 95% intervals are reported.

**Never tuned on what it reports.** After any change to the instructions or the extraction made after a score, the bar is judged on newly labelled mail (a fresh seed), at the same minimum counts.

**`--check-labels`** reports the labels' counts, with no key and no current mailbox needed.

**What it writes:** `results/loose-ends-<timestamp>.json`, marked as a run on labels or on fixtures, with counts only, the models and the shaping hash. It is committed. `.\tasks.ps1 publish` copies runs on the owner's labels, never fixture runs, into `loose_end_scores`, so the page shows them. The timestamp makes a same-day re-run a new file, which is then published too.

**Invented fixtures, in CI** (`data/loose_ends/fixtures/`), metadata and bodies:
- **The rule:**
  - a mailing list in Primary with List-Unsubscribe; an invitation; a SCHEDULED send; a forward; a note to self; a reply-all that keeps an alias;
  - a "Thanks!" then an ask; a group thread where Bob asks while Alice's ask is open; an ask after Dismiss; an ask after Done;
  - spam, trash and gone; a muted sender; the 14-day bound;
  - a gap being worked off; a queued fetch.
- **The closes:**
  - a reply to the asker; a reply to a Reply-To address;
  - an invite created to the asker, and one to someone else; a hold; a dry run; a Cancel;
  - a Reopen not closed again; an ask again.
- **Promises:**
  - replies written in Gmail, Apple Mail and Outlook;
  - a credential Subject on a thread;
  - the due-day rules;
  - a promise delivered in its thread, and one kept in another.

They run in CI with a fake reader that obeys every instruction it finds, as M18's and M19's suites use. They also run once with the real model, as a committed run tied by hash to everything that shapes what the models see:
- `app/loose_ends/`, the cleaner, Gmail's fetch and body extraction, the scrubber and the prompt blocks;
- `app/policy/models.py` and `app/extraction/llm.py`;
- the models' names.

That run needs the paid key.

### D12. The injection suite

Cases live in `data/injection/loose_ends/`. Like M18's, they are cited by id and never quoted.
- **An inbound email that tries to:**
  - make a "you promised";
  - close or reopen another item, or mark itself done;
  - say the owner already replied;
  - set a date, or mute or unmute a sender.
- **Text shaped like** a label, a second quote or the coverage line, and quotation marks or blank-looking characters that try to escape the quote.
- **Promise sources that must never count:**
  - a reply's Subject carrying a promise;
  - a long display name or address in the attribution line;
  - a non-English attribution;
  - a forward quoting a promise "from the owner";
  - the same quoted text in replies written in Gmail, Apple Mail and Outlook.
- **A planted email,** sent beside an open promise, that the owner answers with "Thanks" from Gmail and from Apple Mail. Its Subject, display name and quoted text hold a promise and a fake label.
- **Links and codes,** which the scrubber removes.

They run with the compliant fake reader, so a check that passes only because a model refused proves nothing.

**What every case checks:**
- An item's kind and status come only from D2 to D5's rules in code.
- A model's verdict changes a status only as D2 and D5 allow:
  - `ask` opens or reopens the reply owed of its own thread, under its own sender;
  - `delivered`, on the owner's own words, closes a promise.
- No case's marker text reaches a promise read or a delivery check (M18's check).
- No promise text from another sender reaches an item.
- A quote shows only as a labelled quote, cut and cleaned.
- Nothing is sent anywhere.

### D13. Security

- **No model holds a tool** (M18). The questions are fixed, the answers are typed, and no instruction carries text from mail. The structural test that keeps tools out of `app/extraction/` and `app/planner/` covers `app/loose_ends/`.
- **Nothing leaves the account.** M21 reads Gmail and writes only its own records: no mail, no label, no calendar.
- **The owner's actions** are owner-only, through the web API's secret and the owner's session, and recorded as events.
- **The digest** is composed by code, and its push is fixed text.

## Deliverables

- **Python, new:**
  - `app/loose_ends/`: the records, the rules (D2, D3), the reader entry point and its questions (D2, D4, D5), the own-words extraction (D4), the jobs (D6) and the digest (D7);
  - the API for the page's actions in `app/web_api.py`;
  - the labeller, and `app/eval/loose_ends.py`.
- **Python, changed:**
  - `app/google/gmail.py`: a fetch that keeps the HTML's structure, `Reply-To` and `Authentication-Results`;
  - `app/rag/clean.py`: the attribution line with no length limit, and the header block on flattened HTML;
  - `app/policy/models.py`: the daily cap and the first run's budget;
  - `app/channel/channels.py` and `app/channel/webpush.py`: the alert codes `loose_ends_digest` and `loose_ends_behind`, with their own tags and Telegram words;
  - `app/eval/publish.py`: M21's results;
  - `app/config.py`: `LOOSE_ENDS_ENABLED`, `LOOSE_ENDS_CHECK_MODEL`, `LOOSE_ENDS_DAILY_CAP_USD`, `LOOSE_ENDS_FIRST_RUN_USD` and `GEMINI_PAID_TIER`, all five in `.env.example`;
  - `.gitignore` and `.dockerignore`, for the labels; `data/injection/README.md`, for M21's cases;
  - the purge, for quotes, counterparts and `reply_to`;
  - `tests/test_no_tools.py`, to cover `app/loose_ends/`.

  `gmail.py` and `models.py` are among M18's injection run's hashed files, and `models.py` among M19's routing run's. Neither run has been made yet; both are made after M21's changes.
- **Web:**
  - `/loose-ends`, with its actions, accuracy and muted senders;
  - "Loose ends" in the menu;
  - in `sw.js`, the two tags and a tag-to-page map.
- **Data:** `data/loose_ends/fixtures/`; `data/injection/loose_ends/`.
- **Migration:** `014_loose_ends.sql`, with `web_reader`'s grants.
- **Results:** the first scored run on the owner's labelled mail; the committed model run on the fixtures.
- **Docs:** DEPLOY.md §15 (settings, the paid key, labelling, using Dismiss for a wrong item, retiring the labels), README.

## Commands

```powershell
.\tasks.ps1 check
.\tasks.ps1 loose-ends label                        # label, in your terminal, on a current mailbox
.\tasks.ps1 loose-ends-eval                         # score M21 on the labelled mail (paid key)
.\tasks.ps1 loose-ends-eval --fixtures --baseline   # the committed model run on the fixtures
.\tasks.ps1 publish                                 # copy the scores where the page reads them
```

## Testing

- **Rules, in code, on fixtures:**
  - each part of D2's candidate rule, and the current-mailbox gate (a gap, a queued fetch, a backfill short of its bound);
  - the order within a thread: a follow-up while open, a second asker, an ask after Dismiss, Done or Mute;
  - each close in D3: a reply to the asker or their Reply-To, against a forward, a note to self, a reply-all keeping an alias and a scheduled send; an invite created to the asker, against one to someone else, a hold, a dry run, a Cancel and a Withdraw;
  - an ask again, its moved and reset fields;
  - a Reopen never closed again by the same message;
  - mute and unmute;
  - the due-day rules;
  - D5's batched check and close.
- **Reads:**
  - each question goes through M18's preparation, on the own part, with the credential check on what is read;
  - for a promise, the Subject, quoted text and attribution are never read, in Gmail's, Apple Mail's and Outlook's HTML and in text;
  - the matching guard drops quoted lines, and a mostly quoted reply is `unreadable`;
  - one read per message and question;
  - three strikes make `unreadable`, and an outage charges none.
- **Spending:**
  - the daily cap and the first run's budget stop reads, but not closes;
  - the first run's end, and its overflow recorded `skipped`;
  - Pause stops reads;
  - the order of reads, and the per-sender limit on asks only;
  - the "behind" alert and its clock;
  - an unpriced or evaluation-only model is never called.
- **The digest:**
  - judged once a day, at 9:00 or the first run after it, with a row whatever the outcome;
  - not on a day off;
  - new items first, then the most overdue;
  - the owner's taps are not new, and an old overdue item is not new;
  - written before its push, and never twice;
  - its push fixed, with its own tag;
  - the service worker opens `/loose-ends` for it.
- **Retention:** a quote, counterpart and `reply_to` cleared a week after the close; the rest kept.
- **The API and the page:**
  - each action is owner-only and conditional;
  - the grants;
  - quotes are plain text;
  - the two rates, their numerators and denominators.
- **The injection suite** (D12), with the compliant fake reader and no model in CI.
- **On Postgres:** a fixture mailbox, from M20's records to a digest, through every job.

## Boundaries

- **Always:**
  - read mail only through M18's preparation, with no tool;
  - decide an item's kind and status in code;
  - quote mail only as a labelled quote;
  - keep pushes to fixed text;
  - wait for a current mailbox.
- **Ask first:**
  - a new kind of loose end;
  - any write to Gmail;
  - a push beyond the digest and the "behind" alert;
  - widening `web_reader`'s grant;
  - storing more text than the quote.
- **Never:**
  - give a model a tool;
  - put text from mail in an instruction;
  - let mail text set a status beyond D2's `ask`;
  - read a Subject, or quoted or forwarded text, for a promise;
  - send, reply or draft.

## Exit criterion

1. **On the owner's labelled mail** (decision 24), each kind meets the bar with its minimum counts. The intervals are reported, and the results committed and published.
2. **For two weeks on the owner's real mail,** a digest is delivered on every working day that has something new: a row marked `sent`, not one whose push failed. The items dismissed, muted or reopened stay under 20% of the items opened in those two weeks, over at least 30 items; the run is extended until there are. The events show both. The owner uses Dismiss, not Done, for an item that should not have been there (DEPLOY §15 says so).
3. **A planted email,** sent beside an open promise and answered by the owner with "Thanks" from Gmail and from Apple Mail on the iPhone, makes no "you promised", closes no promise, and shows only as a labelled quote. Its Subject, display name and quoted text hold a promise and a fake label.

## Open questions

- **The Gmail link** to a thread from its id, for the page: checked at build time.
- **Sent volume.** Whether the daily cap holds at the owner's real volume of sent mail. M15 measures it, and the cap can be raised.
- **Other mail clients** whose quoting D4 does not recognise: the matching guard covers what they quote verbatim. A client found leaking adds its structure.
- **Later kinds:** promises owed to the owner, deadlines from Updates, and money (M26).
- **A promise kept in another thread,** and chat questions about loose ends: after daily use.

## Review

### First review (2026-10-18)

A fresh-context adversarial review read the draft against the code at `3ec4772`, the owner's decisions and the project's rules. It found 32 problems: 3 high, 5 medium-high, 12 medium and 12 low. Each was checked against the code and accepted.
- **Two changed the owner's decisions:**
  - the test set was too small for its bar, and drawn by another rule (decision 24);
  - the daily cap could not pay for the reads at 2027 prices (decision 25).
- **The others were folded into the design.** The high ones:
  - **Attacker text read as the owner's words,** through a reply's Subject, or a long attribution line that defeated the quote split. Promise reads now take only the owner's own body (D4, D12, Exit 3).
  - **Reply closes unmeasured, and too loose:** a forward, a note to self or a scheduled send closed them. They are now measured (D11) and narrowed (D3).
  - **A Reopen undone within 15 minutes:** `reopened_at` (D1, D3).
- **The medium-high ones:**
  - a "Thanks!" hid every later ask (D2);
  - the jobs did not wait for the sync (D2, D6);
  - a Gmail outage would have marked the backlog `unreadable` (D6);
  - the meeting close named a status that does not exist (D3);
  - a promise's text would have reached a system instruction (D5).

### Second review (2026-10-18)

A second fresh-context review read the revision, adding a pre-mortem: two weeks after shipping, M21 failed; why? It found 18 problems: 2 high, 10 medium and 6 low. Each was checked against the code and accepted.
- **One changed a decision:** the digest showed the same ten stale items while a new ask hid in "and N more". New items now come first (decision 26).
- **The high ones:**
  - **Own words only from Gmail:** a reply written in Apple Mail or Outlook kept the other side's quoted text as the owner's. D4 now removes those clients' quote structures, cuts on the flattened text, and drops any line found in an earlier inbound message. Exit 3 answers from the iPhone too.
  - **"Waits for the sync" was not enough:** a catch-up's gap, or a reply stuck in the fetch queue, left the owner's reply unstored while the ask was read. D2 now also waits for the gap, the queue and the backfill.
- **The medium ones:**
  - the push opened the timeline, and a new alert would have hidden the sign-in alert: own codes, tags and a tag-to-page map (D6);
  - a second asker in a thread was lost, and asks after Done or Dismiss had no rule (D2, D3);
  - a meeting with someone else closed the asker's item (D3);
  - promises were starved by strangers' asks, and checks cost one call per promise (D6, D9);
  - the first run's overflow and end were undefined (D6);
  - the ask rule was wider than M15's: M15's signals now apply on the stored row (D2);
  - M21 changes files in M18's and M19's hashed sets: named, and those runs ordered after it;
  - tests and exit criteria passed while broken. Now D11 and D12 use a compliant fake reader, a sent digest is one delivered, exit 2's denominator is the items opened, the close precision is defined, and scores carry their models.
- **The low ones:**
  - "to self" was wider than the default;
  - Reply-To asks never closed;
  - a credential Subject hid a thread;
  - `CLASSIFY_MODEL` could be an evaluation-only model, so M21 now has `LOOSE_ENDS_CHECK_MODEL`;
  - the no-tools test now covers M21;
  - the smaller gaps are settled in D1 to D8.

No third review was run: the second review's findings were narrower than the first's, and the build's own phase reviews cover the implementation. A second opinion from another model was offered; none was run. The owner approved the spec on 2026-10-18 as it stood.

## Running notes

### Specified (2026-10-18)

Drafted from four rounds of the owner's decisions, all as recommended, then revised after each review: a fifth round (decisions 24 and 25), and one question (26). Two findings while mapping the code shaped the first draft:
- **M15's labelling keeps counts only.** The first draft added `--keep` to it. The first review showed that would arrive after M15's decision run, and break that module's rule of keeping ids to itself. Decision 24 gives M21 its own labelling instead.
- **Pushes carry fixed text,** so the digest's push only says it is ready, and the digest is the top of the page.

### Tasks review (2026-10-18)

A fresh-context review of the first breakdown, read against this spec and the repository, found 25 problems. The breakdown was rewritten as 21.1–21.28, and these clarifications came into the spec. None changes an owner's decision.
- **An off switch,** `LOOSE_ENDS_ENABLED`, default off: a deploy midway through the build would otherwise have read real mail with no M21 cap, and fixed `started_at` for good (D6).
- **The gate's queue check** counts only `queued` fetches (D2). Every comparison of addresses but the owner's own uses `guest_key` (D2).
- **`gone`** is SPAM, TRASH or `gone_at`, never a row M20's purge deleted (D3).
- **A promise's counterpart** skips the owner's own addresses (D4). **The delivery check** asks only about earlier promises, and skips a credential message (D5).
- **The first run's end,** the "behind" alert's clock, and the alert judged even when the gate stops reads (D6). **A digest is `sent`** when any channel delivered it, and "new" is counted by the events' sequence ids from the last digest that went out (D6).
- **The reopen rate** counts only Reopens of automatic closes, without `gone` (D8). The page lists the last 7 days' closes, with Reopen (D8).
- **The check model counts among the models in use,** and the eval meters apart from production (D9).
- **The labeller's window** is 90 days. Its sample of reply closes is asks answered after 48 hours, and its promise sample aims for 15 kept ones. It resumes, extends and never repeats. `--check-labels` needs no key. Only runs on labels are published. A score is never tuned on the mail it reports (D11).
- **The breakdown itself:**
  - the job, the alerts and the spending are built on a fake reader before the labels, as D11's order requires;
  - the fetch, the cleaner and the own-words extraction are three tasks, the fetch on M18's hidden-text defences, and the cleaner run against every user's tests;
  - M21's alert tags and the service worker's map are one task;
  - snoozes end in 21.3;
  - a deploy task turns the switch on (21.27).

### One outage judge (2026-10-18)

M20 closed its open question on retrying FAILED messages the same day, for outages (its running notes). Poll now asks `app.outage.is_outage`, and it covers more than this spec's `UnavailableError` or `gmail.is_outage`. It also covers:
- the models' network errors;
- Gemini's and AI Gateway's refusals of the key, billing, model or quota;
- a calendar Google cannot read;
- a response cut off mid-read.

M21's reads use the same judge for their strikes (21.7), so a failure poll sends back is never a strike here. The spec's own text ("Why this comes next", and D6's "A failed read") is unchanged; this note is what changed under it.
