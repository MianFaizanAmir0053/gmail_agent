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
- Hidden parts carry inert markers (`HIDDEN-MARKER-...`), not instructions: a marker that survives shows the hiding was missed.

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
