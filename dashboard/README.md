# mailagent dashboard

Five views over the trace tables the agent writes: runs, a per-run trace,
costs, evaluation history, and failures.

## Running it

```bash
cd dashboard
npm install
cp .env.example .env.local     # fill in the database URL, Google client, AUTH_SECRET and OWNER_EMAIL
npm run dev
```

Then open `http://localhost:3000` and sign in with the Google account named in
`OWNER_EMAIL`. `/me` shows who is signed in.

Checks, which need Node 24:

```bash
npm run typecheck && npm test && npm run build
```

## Choices worth knowing

**No API layer.** Server components query Postgres directly. This is a
single-user tool reading tables the agent already owns; an HTTP hop would add
serialisation and a second place for types to drift.

**No charting library.** Two charts, both structurally a list of rectangles.
Hand-rolled SVG renders as part of the HTML with no client bundle and no
hydration boundary, and nothing breaks when a dependency changes its API.

**One user, signed in with Google.** Auth.js admits a single verified address,
`OWNER_EMAIL`. A blank value admits nobody. The rule lives in
`src/lib/access.ts` and is tested with `node --test`, so there is no test
framework to keep up to date. It is checked twice:
- in the proxy (`src/proxy.ts`), which lets only the manifest, the service worker, the icons and Auth.js's own routes through without a session;
- on every database read (`src/lib/db.ts`), because Next documents ways a request can skip the proxy.

Sessions are JWT cookies with no database behind them. Revoking a device means
rotating `AUTH_SECRET`, which signs out every device.

**Unpriced is not free.** A span whose model has no rate stores `NULL`, and the
UI renders `unpriced` rather than `$0.0000` — the same distinction the storage
layer makes, carried through to the screen instead of quietly lost in
formatting.

**Eval history reads committed JSON files**, not the database. The evidence
should be reviewable in the repository rather than only inside a running
service.
