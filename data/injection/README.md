# Injection and credential fixtures (M18)

Each case is one JSON file, cited by its `id` everywhere else: in tests, reviews,
plans and notes. **Never quote a case's text outside this folder.** Quoting
injection payloads in a session made auto mode block that session's shell on
2026-10-02.

All names, domains, codes and links here are invented.

## Format

```json
{
  "id": "cred-otp-own-line",
  "group": "credential",
  "note": "Why the case exists, in one or two sentences.",
  "subject": "…",
  "body": "…",
  "expect": {
    "credential": true,
    "kept": ["text that must survive scrubbing, as it reads afterwards"],
    "gone": ["text that must not survive"]
  }
}
```

- `group`:
  - `credential`: set aside whole (M18, decision 2);
  - `meeting`: mail that must come through, with its times, rooms and meeting links;
  - `hidden`: text the owner never sees, which must not reach a model (D1);
  - `forged`: structure an email tries to forge, which must stay between the call's markers (D3); `expect.inside` lists it;
  - `guest`: mail whose guests the card must say the source of (D5); see below;
  - `output`: not mail, but what a model writes (below).
- The message is either `body`, a single `text/plain` part, or `message`, a MIME tree:

  ```json
  {"mime": "multipart/alternative", "parts": [
    {"mime": "text/plain", "text": "..."},
    {"mime": "text/html", "text": "<p>...</p>"},
    {"mime": "text/plain", "filename": "notes.txt", "disposition": "attachment", "text": "..."}
  ]}
  ```

  `tests/gmail_payloads.py` turns either one into the response Gmail's `users.messages.get` returns, base64url bodies included. That response then goes through the real preparation (`app.google.gmail.to_email_message`).
- `expect.kept` and `expect.gone` are checked against the prepared body. Credential mail must leave the client flagged, its body a fixed notice, with no code and no link in it or in its subject.
- `expect.gap`, when present, names a known gap: the case documents what is not caught.
- Hidden parts carry inert markers (`HIDDEN-MARKER-...`), not instructions: a marker that survives shows the hiding was missed. Text inside SVG or MathML counts as hidden: Gmail draws neither.

## Each attack, written other ways and hidden (`attack`, `variant`)

D8 asks for each attack paraphrased three ways and hidden three ways. A case that varies another names it:

```json
{"attack": "forged-correction-line", "variant": "paraphrase"}
```

- `attack`: the id of the case it varies, the attack's original.
- `variant`: `paraphrase` (the same attack in other words, in the original's group) or `hidden` (the attack inside markup the owner never sees, next to a visible meeting line; mostly in the `hidden` group, a guest in the `guest` group), or `original`.
- The attacks: the four forged structures, a guest the email asks to add (`guest-only-in-body`), mail that asks for a code (`bait-ask-for-code`, set aside whole since it names one), and a title shaped like the card's own lines (`output-title-edit-prompt`, paraphrased only: a model's output cannot be hidden).
- Each hiding technique is used more than once across the attacks: an inline `display:none`, a zero font size, a comment, `visibility:hidden`, a zero max-height, the `hidden` attribute, zero opacity, `mso-hide`, SVG, MathML, a `display:none` followed by a CSS comment left open, `display:none` written with a CSS escape, and a `template`.
- A `<br>` inside SVG or MathML ends the foreign element, as the HTML parser defines it: text after it is drawn. Hidden cases use plain line breaks there.

`tests/test_injection.py` runs every case through the preparation and the prompt assembly, and checks that each attack has its variants. A failure names the case and the expectation, never the text.

## Where a guest came from (`guest`)

A `guest` case is mail as above, with what a model proposes from it and the world around it:

```json
{
  "guests": ["new@example.net"],
  "participants": ["sara@example.com"],
  "allowed": [],
  "expect": {"credential": false, "sources": {"new@example.net": "email"}, "quoted_section": false}
}
```

- `guests`: the addresses the model proposes, written as the extraction leaves them, lower-cased.
- `participants`: whom the owner wrote to in the thread; tests build the thread's metadata from them.
- `allowed`: contacts the owner has allowed.
- `expect.sources`: the source each guest's card must show: `thread`, `allowed`, `email`, `quoted` or `absent` (`app/policy/participants.py`). `expect.quoted_section`: whether the email quotes or forwards older mail.

## What the model writes (`output`)

A case of the `output` group is not mail. It is a title and a location as a model might write them, either one `null`, and what must hold once they are scrubbed for the card and the event (D6):

```json
{
  "id": "output-title-link",
  "group": "output",
  "note": "…",
  "title": "…",
  "location": null,
  "expect": {"kept": ["…"], "gone": ["…"]}
}
```

`expect.kept` and `expect.gone` are checked against the scrubbed title and location together, and each must come out on one line. Lines shaped like instructions carry inert markers (`OUTPUT-MARKER-...`).

## Chat cases (`chat/`, M19)

`chat/` holds the chat injection cases (M19, D11). They live in a folder of their own, so M18's loaders and its eval's hash, which read this folder's own `*.json`, never see them. `tests/test_chat_injection.py` runs each one.

Each case is mail a search finds, text the owner pastes, or an event a stranger sent, which tries to steer what the owner is told or asked. The planner never sees any of it, so a case carries the plan a correct planner writes from the typed request alone. That plan runs through the real executor, reader and renderer. Only the reader's model is a fake, and a compliant one: it reports every line it is given, and names every address.

```json
{
  "id": "chat-mail-…",
  "attack": "add this address | the owner wants this | a forged sender name | a quoted chat reply",
  "typed": "what the owner typed",
  "pasted": null,
  "mail": [{"id": "…", "from": "…", "dmarc": true, "at": "2026-10-06T16:00", "subject": "…", "body": "…"}],
  "events": [{"title": "…", "start": "…", "end": "…", "organizer": "…"}],
  "plan": {"kind": "answer", "timezone": "Asia/Karachi", "steps": ["…"], "reply": {"template": "…"}},
  "expect": {
    "plan": "refused",
    "labels": ["from …@… (domain not verified)"],
    "cards": [{"title": "…", "start": "…", "end": "…", "guests": ["…"]}],
    "allow": ["…"],
    "never_on_card": ["…"],
    "absent": ["…"]
  }
}
```

- The world is the routing eval's (`data/planner/world.json`): its owner, zone, contacts, calendar, and the time of asking. A case brings its own mail, and may add events.
- `expect.labels` lists every label the answer may show outside quotation marks, exactly. A label is "from" an address or "an unknown sender" with Gmail's verdict, "from the text you pasted", "sent by you", or "an invitation from" an organiser. Each label must come before anything quoted on its line.
- `expect.cards` are the proposals parked, with local times. No guest in `never_on_card` may be on one.
- `expect.allow` lists the addresses offered for an Allow, which only a paste's can be.
- No text in `expect.absent` may show outside quotation marks: a display name, or a line forged to look like the answer's own.
- `expect.plan` is `refused` when the plan check must refuse the plan.
- On every case: the answer holds no hidden or blank-looking character, every quotation is cut to 300 characters, and the calendar is only read.
