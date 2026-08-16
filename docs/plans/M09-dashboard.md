# M09 · Next.js dashboard

**Est.** 3 days · **Depends on** M08 · **Blocks** nothing

## Goal

Make the M08 data visible. This is the full-stack evidence — a real UI over real production data, not a toy CRUD app.

## Deliverables — exactly five views, then stop

1. **Runs list** — table, status filter, date range, cost per run
2. **Trace detail** — per-node timeline with latency, tokens, cost; redacted input/output
3. **Cost chart** — spend per day, split by model
4. **Eval history** — accuracy over time from `results/eval-*.json`, annotated with what changed
5. **Failures** — failed runs with error detail and a re-run link

## Scope control

**Do not build a user system.** A single shared token in an env var, checked by middleware, is correct here. Auth teaches you nothing in this project and will cost you three days.

Scope creep on dashboards is near-universal. Five views. Ship it.

## Stack

Next.js App Router, server components querying Postgres directly (no API layer needed for a single-user tool), Tailwind, Recharts or similar. Deploy on Vercel — free tier, and it can reach a Neon/Supabase database fine.

## The eval history view is the one that matters

The other four are competent instrumentation. This one is the story: a line going from your frozen M03 baseline up to wherever you finish, with annotations for *what you changed at each step*.

That chart is the visual version of the sentence you want to say in an interview. Make it screenshot-able for the README.

## Exit criterion

Deployed, and you can diagnose a real production failure end-to-end from the dashboard — without opening a SQL client.

## Running notes

**Two bugs found by running it, neither visible in the code.**

`npm audit` flagged **CVE-2025-66478** in Next 15.5.4 on install, plus transitive `postcss` and `sharp` advisories. Neither transitive is reachable here — our CSS is ours and there is no `next/image` — but the fix was Next 16, which audits clean, so the major upgrade was worth taking rather than shipping a documented-vulnerable version in a portfolio repo.

The session cookie keyed `secure` off `NODE_ENV`. Under `npm start` that means a `Secure` cookie served over `http://localhost`, which the browser accepts and then refuses to send back, so sign-in silently never completes. Deriving it from `request.nextUrl.protocol` is correct both locally and behind HTTPS.

**Turbopack caught a design flaw, not just a build warning.** Reading `../results/*.json` escapes the project, and it warned that this traces the entire repository into the deployment. The deeper problem was worse: a dashboard deployed separately from the agent has no repository to read at all. Eval history now lives in Postgres, published by `app.eval.publish` — deliberately a separate command, so `app.eval.run` stays runnable with no database anywhere. The JSON files remain the committed evidence.

**No API layer.** Server components query Postgres directly. One reader, tables the agent already owns; an HTTP hop would add serialisation and a second place for types to drift.

**No charting library.** Two charts, both structurally a list of rectangles. Hand-rolled SVG renders inside the HTML with no client bundle and no hydration boundary, and nothing breaks when a dependency changes its API.

**`unpriced` is rendered as `unpriced`, not `$0.0000`.** The storage layer is careful to distinguish "free" from "not priced"; formatting it as zero on screen would throw that away at the last step.

**Auth fails closed.** An unset `DASHBOARD_TOKEN` returns 503 rather than admitting everyone — the same mistake as the webhook secret, avoided deliberately this time.

### Verified

```
npm audit        0 vulnerabilities
tsc --noEmit     clean
next build       6 routes, no warnings
```

Live against the real database:

```
no-token   -> 401
?token=…   -> 307 + cookie
/          200   6 message IDs, all success
/costs     200   classify / fetch / skip, $0.000751 total, $0.0250 per 100 emails
/evals     200   latest 92.9%, floor 35.7%
/failures  200   empty (no failed runs yet)
```

**The failures view has never been exercised with real data** — nothing has failed yet. It is the one view whose usefulness is still unproven.
