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
| `OBLAG_SMTP_*` | optional email delivery |

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

`vercel.json` defines the crons: daily source group (05:10 UTC), weekly group
(Mon 08:10), the state-machine tick (00:15), and notification dispatch (every 6 h).
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
