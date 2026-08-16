# mailagent dashboard

Five views over the trace tables the agent writes: runs, a per-run trace,
costs, evaluation history, and failures.

## Running it

```bash
cd dashboard
npm install
cp .env.example .env.local     # fill in DATABASE_URL and DASHBOARD_TOKEN
npm run dev
```

Then open `http://localhost:3000/?token=<DASHBOARD_TOKEN>` once. The token is
exchanged for a cookie and stripped from the URL, so it does not linger in
browser history or leak through a referrer header.

## Choices worth knowing

**No API layer.** Server components query Postgres directly. This is a
single-user tool reading tables the agent already owns; an HTTP hop would add
serialisation and a second place for types to drift.

**No charting library.** Two charts, both structurally a list of rectangles.
Hand-rolled SVG renders as part of the HTML with no client bundle and no
hydration boundary, and nothing breaks when a dependency changes its API.

**No user system.** One shared token checked in middleware. Accounts, sessions,
and password resets would cost days and demonstrate nothing the rest of the
project does not already show. The middleware fails closed: an unset
`DASHBOARD_TOKEN` returns 503 rather than allowing everyone.

**Unpriced is not free.** A span whose model has no rate stores `NULL`, and the
UI renders `unpriced` rather than `$0.0000` — the same distinction the storage
layer makes, carried through to the screen instead of quietly lost in
formatting.

**Eval history reads committed JSON files**, not the database. The evidence
should be reviewable in the repository rather than only inside a running
service.
