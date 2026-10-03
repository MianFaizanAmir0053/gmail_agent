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
  - `hidden`: text the owner never sees, which must not reach a model (D1).
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
