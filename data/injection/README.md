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

- `group`: `credential` (set aside whole: M18 decision 2) or `meeting` (mail that must come through, with its times, rooms and meeting links).
- `body` is the text the body preparation hands the scrubber. Later cases add the raw Gmail parts that the body preparation reads (M18, D1).
- `expect.kept` and `expect.gone` are checked against the scrubbed body of meeting mail. Credential mail is never scrubbed: its body is replaced whole.
