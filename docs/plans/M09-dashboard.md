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

_(record what surprised you here)_
