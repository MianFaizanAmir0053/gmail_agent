# M02 · Eval harness & golden dataset

**Est.** 1 day · **Depends on** M00 (parallel with M01) · **Blocks** M03

## Goal

A scorer that can measure any extractor, built **before** the extractor exists. This is the module that makes "accuracy went from X% to Y%" an honest sentence rather than a retroactive guess.

## Why this moved earlier

The original plan put evals in Phase 3, after the agent was already deployed. That makes every prompt change in Phases 1–2 unmeasured, and makes the headline improvement number unprovable — you cannot produce a *before* baseline after you've already improved things. The harness has to exist before the first extraction run.

## Deliverables

- `data/raw_emails/` — real emails, **gitignored**
- `scripts/anonymize.py` — consistent pseudonymisation of names, addresses, companies, phone numbers
- `data/fixtures/*.json` — scrubbed, committed, safe for a public repo
- `data/fixtures/labels.json` — hand-labeled expected `ExtractionResult` per fixture
- `app/eval/scorer.py` — field-level scoring
- `app/eval/run.py` + `make eval` — prints a table, writes `results/eval-<timestamp>.json`

## Scoring

| Field | Metric |
|---|---|
| `is_meeting` | precision / recall / F1 |
| `start_utc`, `end_utc` | exact match to the minute, **compared in UTC** |
| `attendees` | set F1 (email addresses normalised lowercase) |
| `title` | fuzzy ratio, ≥0.8 counts as correct |
| `timezone` | exact IANA string match |

Report per-field and one headline aggregate. The headline number is what goes in the README.

## Dataset coverage — deliberately include

30–40 emails spanning:

- Relative dates: "next Tuesday", "tomorrow at 3", "end of week"
- **Timezone-crossing invites** (sender in a different zone to you)
- All-day events and multi-day events
- Cancellations and reschedules ("moving our 2pm to 4pm")
- Forwarded threads where the meeting details are quoted several replies down
- **Non-meeting emails** — newsletters, receipts, notifications, marketing

That last category matters most. False positives (the agent proposing an event for a newsletter) are the failure mode a user actually notices and the one that makes them turn the bot off.

## Privacy

Real emails contain other people's names, addresses, and business details. They cannot go in a public repo. The anonymiser must be **consistent** — the same real name maps to the same fake name across every fixture, or threading and attendee-matching test cases stop making sense.

Anonymisation is part of this module's definition of done, not a cleanup task for later.

## Exit criterion

`make eval` runs against a stub extractor that returns all-nulls and correctly reports ~0%. The plumbing works before there's anything real to measure.

## Running notes

**The "~0%" exit criterion in this plan was wrong.** An always-says-no extractor is genuinely *correct* on every non-meeting, so it scores 35.7% here, not 0%. That is not a bug in the harness — it is the majority-class floor, and it is the number any real extractor must beat. Quoting a headline without comparing against it is how a model that has learned nothing gets reported as good.

Both baselines are committed under `results/` as the floor:

```
always_no    is_meeting acc 35.7%   headline exact-match 35.7%
always_yes   is_meeting acc 64.3%   headline exact-match  0.0%
             (recall 100%, precision 64.3% -- the corpus base rate)
```

**Fixtures carry their own `now_utc`.** Relative dates only have a correct answer relative to an instant. Without pinning it per fixture, the suite silently changes meaning every day it runs and "next Tuesday" cases rot. The extractor signature is therefore `(email, *, now_utc, user_timezone)`, not `(email)`.

**Grounded on Monday 2026-08-17, not Sunday.** "Next Tuesday" said on a Sunday genuinely has two defensible readings; no label would have been correct. Ambiguity deserves its own fixture *plus* a written policy, not a coin flip baked into a golden set — deliberately left as a gap.

**One file per fixture, not `fixtures/*.json` + `labels.json` as planned.** The split needed an ID join, and an email drifting from its label is a silent failure that caps the achievable score while looking like a model bug.

**Title scoring needed two metrics.** Pure character similarity scores "Design review" against "Design Review meeting" at 0.76 and fails it — but that is a *good* extraction, and failing it makes the headline pessimistic and noisy. Now matches on character ratio ≥ 0.8 **or** token overlap ≥ 0.6, which still rejects a bare "Sync" for "Vendor sync" (0.5) where the identifying word was actually lost.

**Base rate is deliberately wrong.** 64% of fixtures are meetings against maybe 5% in a real inbox. Balanced sampling is right for measuring the event fields, but it means `is_meeting` precision here flatters reality — noted in `data/README.md` so the number never gets quoted as production performance.

### Verified

```
ruff / format / mypy --strict   pass
pytest                          179 tests
make eval equivalent            .\tasks.ps1 eval --extractor always_no
```

`tests/test_fixtures.py` parametrises over the golden set itself, so a mislabelled fixture (meeting in the past, owner listed as attendee, offset instead of IANA zone, tag disagreeing with the label) fails the build rather than quietly capping the score.

**Still synthetic.** `app.eval.fetch` and `app.eval.anonymize` exist for pulling real mail through, but no real fixtures have been added yet. The live inbox is nearly all job alerts, so meeting cases need deliberate archive search.
