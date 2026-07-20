# Spec 07 — Multi-tenancy: ObligationAggregator as a public app

Status: **approved design, not yet implemented** (this document is the Phase-0
deliverable). Target: orgs sign up and use a hosted instance; self-hosting stays
first-class and single-org deployments keep working unchanged.

## 1. Design stance

The pipeline is **shared infrastructure**; tenancy is a **thin personalization
layer**. Regulatory changes, obligations, lifecycle states, key dates, events,
snapshots and attestations are identical for every org and are ingested once by
the instance's scheduler. Nothing in the core engine (specs 00–05) becomes
tenant-aware. This is the property that makes the SaaS shape cheap and keeps
the state machine's invariants intact.

Tenant-owned state is exactly:

| Resource | Today | Multi-tenant |
|---|---|---|
| Watchlists + notification targets | instance-global | owned by an org |
| RSS feed URLs | guessable (`/feeds/{id}.xml`) | unguessable per-watchlist token |
| BYOL private documents | instance-global private dir | **strictly org-isolated** (§6) |
| Curated date assertions | anyone (CLI) | instance admins only (shared data!) |
| Source credentials (LegiScan key, …) | instance env vars | stay instance-level (shared fetch) |
| SMTP | instance env vars | instance-level default; per-org From/reply-to later |

## 2. Tenancy & identity model

New tables (all additive — no changes to existing tables except the ownership
columns noted):

```
org            id, slug, name, created_at
user           id, email (unique, citext), display_name, created_at, last_login_at
org_member     org_id, user_id, role ('owner' | 'admin' | 'member'), UNIQUE(org_id, user_id)
login_token    id, user_id_or_email, token_hash, expires_at, consumed_at   -- magic links
session        id (random 256-bit), user_id, org_id (active org), expires_at, created_at
api_key        id, org_id, name, key_hash, created_at, last_used_at, revoked_at
watchlist      + org_id FK (nullable during migration, §8), + feed_token (random)
private_document + org_id FK
```

- A user can belong to multiple orgs; the session carries the **active org**.
- Roles: `owner` (billing/danger zone), `admin` (manage members, watchlists,
  BYOL), `member` (create/edit own watchlists, read everything).
- Instance operators get a separate `OBLAG_INSTANCE_ADMINS` (csv of emails)
  gate for cross-tenant operations: curated assertions, purge/relink/seed
  maintenance, adapter health beyond the public page.

## 3. Authentication: self-rolled magic links (decided)

No passwords, no third-party identity dependency; reuses the SMTP config the
instance needs for notifications anyway.

Flow:
1. `POST /auth/login {email}` → create `login_token` row storing **hash** of a
   256-bit random token, 15-minute expiry, single-use. Email the link:
   `https://…/auth/verify?token=…`.
2. `GET /auth/verify` → constant-time hash compare, not expired, not consumed →
   consume; create user on first login; create `session` row; set cookie
   `oblag_session` (HttpOnly, Secure, SameSite=Lax, 30-day rolling expiry).
3. First-login onboarding: create an org (name → slug) or accept a pending
   invite (invite = `org_member` row keyed by email, activated on first login).
4. `POST /auth/logout` deletes the session row.

Properties: tokens and API keys stored **hashed only** (SHA-256); rate-limit
`/auth/login` per email+IP (5/hour) to prevent mail-bombing; no account
enumeration (identical response whether or not the email exists). Enterprise
SSO (OIDC) can be added later as an *additional* login method without schema
changes — `user.email` remains the join point.

## 4. Authorization boundaries

- **Public (no auth):** change feed, item detail, obligations catalog,
  deadlines (+ .ics), activity, sources health, read-only JSON API. The shared
  regulatory data is the product's public face and marketing surface.
- **Authenticated, org-scoped:** watchlist CRUD (`WHERE org_id = session.org`),
  BYOL upload/diff, API keys, org settings, member management (admin), notification
  history for the org's watchlists.
- **Instance admin:** curated assert-date, maintenance endpoints (in addition
  to the existing `OBLAG_CRON_SECRET` machine gate), catalog editing.
- API requests authenticate with `Authorization: Bearer <api_key>` → resolves
  to an org; same scoping as sessions. Per-key rate limit (e.g. 600 req/min)
  via a simple fixed-window counter in Postgres (no Redis dependency).

## 5. Notifications under tenancy

- RSS: feed URL becomes `/feeds/{feed_token}.xml` (128-bit token) — the only
  authentication a feed reader can handle. Existing `/feeds/{id}.xml` returns
  410 after migration.
- Email: instance SMTP; `From` stays instance-level, per-org reply-to later.
  Delivery failures already retry (at-most-once log records successes only) —
  unchanged.
- Webhooks: add optional per-watchlist HMAC signing secret
  (`X-Oblag-Signature: sha256=…`) so org endpoints can verify authenticity.
  Outbound webhook targets must be validated against SSRF (no private IP
  ranges, no redirects) once orgs can point them anywhere.

## 6. BYOL isolation (legally load-bearing)

Private documents move under `private/{org_id}/…` (or org-prefixed keys in
blob storage). Every read path takes org from the session — never from the
request body. Spec 00 invariant 3 ("BYOL content never appears in shared
outputs") gains a sibling: **BYOL content never crosses an org boundary** —
enforced in `byol.py` queries and covered by a dedicated test that attempts a
cross-org read and must 404.

## 7. Public-exposure hardening (prerequisites to launch)

- Rate limiting on all public endpoints (per-IP fixed window; generous).
- Watchlist creation requires auth → removes the current open-write surface.
- CSRF: all state-changing HTML forms get a per-session CSRF token (magic-link
  cookie auth makes CSRF real; today's no-auth forms didn't need it).
- Security headers: CSP (self-only — the UI is already dependency-free), 
  X-Content-Type-Options, Referrer-Policy.
- Postgres row counts and blob storage are shared-instance costs; per-org
  quotas on watchlists (e.g. 100) and BYOL documents (e.g. 50) as guardrails.
- Rotate `CRON_SECRET`; move maintenance endpoints to instance-admin sessions
  where interactive.

## 8. Migration path (zero-downtime, backward compatible)

1. Ship schema additively (`org_id` nullable, `feed_token` backfilled for all
   existing watchlists; existing rows get org NULL = "legacy instance org").
2. On first boot after upgrade, create a `default` org; adopt legacy watchlists
   and private documents into it.
3. Self-hosted single-org mode: `OBLAG_AUTH=disabled` (default **on** for new
   config, `disabled` preserved for existing deployments) skips login entirely
   and pins every request to the default org — today's behavior, exactly.
4. Hosted mode: `OBLAG_AUTH=magic-link` + `OBLAG_INSTANCE_ADMINS` set.

## 9. Build phases

- **Phase 1 — foundation:** schema, magic-link auth, sessions, org onboarding,
  org-scoped watchlist CRUD, feed tokens, CSRF. (Biggest single phase.)
- **Phase 2 — programmatic access:** API keys, per-key rate limits, webhook
  HMAC + SSRF guards, org member invites.
- **Phase 3 — org depth:** BYOL isolation, per-org email preferences,
  instance-admin UI for curated assertions, quotas.
- **Phase 4 — commercial (optional):** billing (Stripe), plan limits, usage
  metering. Deliberately unspecified until the product proves out.

## 10. Explicitly out of scope

- Per-org ingestion pipelines or per-org source credentials (shared fetch is
  the economic moat of the design).
- Fine-grained per-user permissions beyond the three roles.
- Multi-region data residency (single Postgres; revisit if EU-only tenants
  demand it).
