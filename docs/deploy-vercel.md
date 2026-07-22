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

**Org depth (Phase 3):** each org's BYOL licensed documents are strictly isolated —
org-partitioned storage, every query org-scoped, so one tenant can never read
another's copies (uploaded + diffed under **Documents**). Orgs set a notification
From-name and Reply-To under Settings, applied to their email watchlists. Instance
admins (`OBLAG_INSTANCE_ADMINS`) can add curated dates to items from the UI. Optional
per-org quotas: `OBLAG_QUOTA_WATCHLISTS`, `OBLAG_QUOTA_API_KEYS`, `OBLAG_QUOTA_BYOL_DOCS`,
`OBLAG_QUOTA_INVITES` (0 = unlimited, the default).

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

## Reliability & operations (v0.7.x)

### Environment separation (recommended)

Preview deployments boot the same code as production. To keep a branch's boot code
(catalog sync, repairs, milestone seeding) from ever touching production data,
give previews their own database:

1. Create a Neon **branch** of the production database (near-instant, copy-on-write).
2. In Vercel → Project → Settings → Environment Variables, set
   `OBLAG_DATABASE_URL` for the **Preview** environment to the branch's connection
   string, and set `OBLAG_ALLOW_PREVIEW_BOOT_WRITES=true` for Preview only.

Until that is set, previews are safe by default: a deployment where
`VERCEL_ENV=preview` **skips all mutating boot steps** unless
`OBLAG_ALLOW_PREVIEW_BOOT_WRITES=true`.

### Operator hardening (public single-org deployments)

With `OBLAG_AUTH` unset (single-org), the UI has no login. The shared-data write
(curated date assertions) is gated behind an operator token: visit `/admin/unlock`,
enter it once (sets a 12-hour httponly cookie), and the admin form appears.

The gate token is **`OBLAG_ADMIN_TOKEN`** if set, otherwise it falls back to
**`OBLAG_CRON_SECRET`** — so any deployment that runs scheduled fetches (which requires
a cron secret) is locked automatically with no extra configuration; unlock with the
cron secret you already have. The write is fully open only when *neither* is set (fine
for local/private use). Set `OBLAG_ADMIN_TOKEN` when you want a distinct operator
secret separate from the cron secret. For full multi-user isolation, set
`OBLAG_AUTH=magic-link` instead (requires SMTP).

### Failure alerts

Set `OBLAG_SMTP_*` and `OBLAG_OPS_ALERT_EMAILS` (csv; falls back to
`OBLAG_INSTANCE_ADMINS`, then `OBLAG_SMTP_FROM`). After each cron group, any data
source stuck failing (`consecutive_failures ≥ 2`) triggers one email per source
per day. Source health is always visible at `/health`.

### Backup & restore drill

Snapshots live in Vercel Blob and all structured data in Neon Postgres. Neon keeps
point-in-time recovery automatically. **Exercise the restore path before you need
it**: create a Neon branch from a PITR timestamp, point a preview at it
(`OBLAG_DATABASE_URL`), and confirm `GET /api/v1/items` returns the expected count.
A restore you have never run is not a backup.

### Deployment verification

The `deploy-verify` GitHub Action polls production `/openapi.json` for the merged
`__version__` after each push to `main` and fails loudly if it never appears —
catching a merge whose production deploy silently never started. Override the URL
with a `PROD_URL` repository variable if it changes.
