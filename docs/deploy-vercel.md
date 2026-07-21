# Deploying ObligationAggregator on Vercel

The whole platform runs as one Vercel project: the FastAPI app (UI + API) as a Python
function, Vercel Cron for ingestion, Postgres for the system of record, and Vercel Blob
for snapshots/attestations.

## 1. Provision

- **Database**: create a Postgres database (Vercel Postgres / Neon). Note the
  connection string.
- **Blob store**: create a Blob store in the Vercel dashboard and link it to the
  project (this injects `BLOB_READ_WRITE_TOKEN`).
- **Signing key** (optional but recommended): run `oblag keygen` locally, then copy
  the PEM contents.

## 2. Environment variables (Project → Settings → Environment Variables)

| Variable | Value |
|---|---|
| `OBLAG_DATABASE_URL` | `postgresql+psycopg://…` (from step 1) |
| `OBLAG_STORAGE_BACKEND` | `vercel-blob` |
| `OBLAG_CRON_SECRET` and `CRON_SECRET` | the same random string — Vercel sends `CRON_SECRET` as the cron Authorization bearer; the app checks `OBLAG_CRON_SECRET` |
| `OBLAG_SIGNING_KEY_PEM` | contents of `signing.pem` (enables attestations) |
| `OBLAG_BASE_URL` | your deployment URL (used in notifications/RSS links) |
| `OBLAG_REGSGOV_API_KEY` … | source credentials as desired (regulations.gov, LegiScan, OEIL procedures, HYS topics) |
| `OBLAG_BROWSER_CDP_URL` | optional: a remote Chromium CDP websocket (e.g. Browserless) — enables the EBA browser adapter serverlessly |
| `OBLAG_SMTP_*` | email delivery — also required for magic-link login below |
| `OBLAG_AUTH` | `disabled` (default, single-org) or `magic-link` (multi-org public app, spec 07) |
| `OBLAG_INSTANCE_ADMINS` | csv of emails granted instance-admin operations (only with `magic-link`) |

### Single-org vs multi-org

By default (`OBLAG_AUTH` unset or `disabled`) the instance is single-org: no login,
watchlists open to anyone who can reach it — the current behavior. Set
`OBLAG_AUTH=magic-link` (plus working `OBLAG_SMTP_*` for the sign-in emails and
`OBLAG_BASE_URL` for the link host) to run it as a public multi-org app: orgs sign
up by email, watchlists and notifications are scoped per org, and the change
feed / obligations / deadlines stay public. See `docs/specs/07-multi-tenancy.md`.

**Programmatic access (multi-org):** each org mints API keys under **Settings** and
calls the JSON API with `Authorization: Bearer oblag_…`, scoped to that org and
rate-limited (`OBLAG_API_RATE_LIMIT_PER_MIN`, default 600/min). Webhook watchlists get
a per-watchlist HMAC secret; each delivery carries `X-Oblag-Signature: sha256=<hmac>`
over the raw body for authenticity, and targets are SSRF-validated (no private /
loopback / metadata hosts; redirects disabled). Org admins invite teammates by email
under Settings; invitees join on first sign-in.

## 3. Deploy

```bash
vercel deploy   # repo root; vercel.json routes everything to api/index.py
```

Then seed the catalog once (any of):

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://<app>/api/internal/run-group/daily
# or locally against the prod DB:
OBLAG_DATABASE_URL=… uv run oblag seed
```

## 4. Scheduling

`vercel.json` defines two crons (the Hobby plan allows at most 2, daily frequency
minimum): the daily source group (05:10 UTC) and the state-machine tick (00:15).
The daily run-group endpoint automatically includes the weekly adapter group on
Mondays (UTC), and every ingestion endpoint dispatches pending notifications when
it finishes, so no separate weekly or dispatch cron is needed.
The endpoints are 404 unless `OBLAG_CRON_SECRET` is set and 401 without the bearer.

## Platform notes / limits

- **Function duration**: daily incremental runs fit comfortably in the 300 s
  configured `maxDuration`. Historical **backfills do not** — run
  `oblag backfill … ` from a laptop/CI pointed at the production
  `OBLAG_DATABASE_URL` (windowed queries make this restartable).
- **Browser tier**: Vercel functions cannot host Chromium. With
  `OBLAG_BROWSER_CDP_URL` set, browser adapters (EBA) connect to a remote Chromium
  over CDP; without it they self-disable cleanly and everything else still runs.
- **BYOL** stays a local/CLI workflow by design — licensed documents must not be
  uploaded to shared storage (spec 06).
- **APScheduler is not used on Vercel** — do not run `oblag serve --with-scheduler`
  there; Vercel Cron replaces it. Self-hosting keeps working unchanged.
