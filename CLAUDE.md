# CLAUDE.md

Guidance for Claude when working on this repository.

## What this project is

A multi-tenant billing API built in Django. It charges companies monthly, keeps
each company's data isolated, and never double-charges. The full spec is a
six-phase build (data model + Docker, core endpoints, idempotent payments,
multi-tenancy + worker, tests + docs, deploy).

## Phase gating — read this first

**Do not start work on a phase until the previous phase meets its "Done when"
in full.** Each phase has an explicit completion condition, and the schema being
in place is not the same as the phase being finished. If the author asks for
Phase N work while Phase N-1 is incomplete, say so and name the specific
remaining item rather than going along with it.

| Phase | "Done when" | Status |
|---|---|---|
| 1 — Data model + skeleton | `docker compose up` starts API + Postgres, migrations create all tables | **Done** (verified 2026-07-30) |
| 2 — Core endpoints | Create tenant → assign plan → record usage → generate correct invoice, all via API | **In progress** — auth layer done, no endpoints yet |
| 3 — Idempotent payments | Same payment request twice returns same result, charges once (save both curl commands + output) | Blocked on 2 |
| 4 — Multi-tenancy + worker | Worker generates invoices on a schedule; tenant isolation proven by a test | Blocked on 3 |
| 5 — Tests + docs + CI | CI green on push (lint + tests + Docker build), Swagger lists every endpoint | Blocked on 4 |
| 6 — Deploy + package | Live URL, README with architecture diagram + no-double-charge proof, decision note | Blocked on 5 |

### Phase 1 — what was verified

The **data model half**: all seven tables exist, both migrations apply,
`manage.py check` is clean, and shell checks pass for choices validity, reverse
accessors, both unique constraints, ledger balancing, tenant isolation, and
PROTECT behavior. `0001_initial.py` confirmed to contain
`unique_invoice_period`, `unique_key_per_tenant`, and the `LedgerEntry` index.

The **infrastructure half**, confirmed by running the stack:

- `Dockerfile` (single stage, `python:3.13-slim`), `docker-compose.yml`,
  `.dockerignore`, `requirements.txt` all in place.
- `docker compose up -d --build` brings up `db` (healthy) and `web` on
  `0.0.0.0:8000`. `/admin/` returns 302.
- Migrations run on container start and create all seven `billing_*` tables in
  Postgres. Named volume `multi-tenant-bailing-system_db` persists them.
- Postgres logs are clean — 0 `FATAL` lines.

### Phase 2 — where it stands (2026-07-31)

Done:

- `Tenant.api_key` — `CharField(max_length=256, unique=True, default=make_key)`.
  `make_key()` returns `secrets.token_urlsafe(32)`, which is 43 characters and
  256 bits of entropy. Migration `0003_tenant_api_key` generated and applied;
  confirmed it serializes `default=billing.models.make_key`, the **callable**,
  so every row gets a fresh key. Verified live: two tenants, two distinct keys.
- Note on the length argument: `token_urlsafe(n)` takes **bytes of entropy**, not
  output characters. Base64 expands ~4/3, so `token_urlsafe(256)` returns 342
  chars and overflows `varchar(256)`.
- DRF installed — `djangorestframework==3.17.1` in `requirements.txt`,
  `rest_framework` in `INSTALLED_APPS`. Confirmed importable **inside the
  container**, which is the only check that counts after a requirements change.
- `billing/url.py` renamed to `urls.py`, holds `urlpatterns = []`, and
  `config/urls.py` includes it under `billing/`. An empty file is not enough —
  `include()` imports the module and reads `urlpatterns`, so a missing name is
  `ImproperlyConfigured`, raised lazily on the first request rather than at boot.
- **Auth layer complete** (open decision 1). `billing/authenticate.py` holds
  `TenantAuthentication(BaseAuthentication)` with `keyword = 'Api-Key'`,
  `authenticate()`, and `authenticate_header()`. `Tenant.is_authenticated` is a
  `@property` returning `True` (no migration — `makemigrations --check` reports
  no changes). `REST_FRAMEWORK` in settings sets
  `DEFAULT_AUTHENTICATION_CLASSES = ['billing.authenticate.TenantAuthentication']`
  and `DEFAULT_PERMISSION_CLASSES = ['rest_framework.permissions.IsAuthenticated']`.

  Verified end to end through real DRF dispatch (throwaway probe view in a shell,
  not committed): no header → 401, bad key → 401, inactive tenant → 401, wrong
  scheme → 401, valid key → 200 with the correct `request.tenant`. Every failure
  carries `WWW-Authenticate: Api-Key`, and bad-key and inactive-tenant responses
  are byte-identical.

Traps hit while building it, worth not re-learning:

- `get_authorization_header()` returns **bytes**. `auth[0] != 'Api-Key'` is
  always true, and `Tenant.objects.filter(api_key=b'...')` returns empty with
  **no error** — both fail silently as permanent 401s. Decode before comparing
  or querying.
- `filter()` returns a QuerySet and never raises `DoesNotExist`; `get()` returns
  the row and does. The `except Tenant.DoesNotExist` branch only works with
  `get()`.
- Without `authenticate_header()`, DRF rewrites every 401 to **403** — a 401
  must carry `WWW-Authenticate` to be a valid response, so DRF downgrades rather
  than emit a malformed one.
- `is_authenticated` must be a `@property`. As a plain method,
  `bool(<bound method>)` is `True`, so `IsAuthenticated` passes by accident and
  any `if not user.is_authenticated` silently misbehaves.

Not started, in order:

1. Create `billing/services.py` (open decision 2) before writing views.
2. Endpoints: create tenant, create plan, assign plan (Subscription), record
   usage, generate invoice, plus read endpoints. Create-tenant and create-plan
   cannot authenticate as a tenant — they need `permission_classes = [AllowAny]`
   or an admin-only path. Decide that when writing them.
3. Every queryset in every view filters on `request.tenant`. Auth resolves
   identity; it does not isolate data. Phase 4's isolation test targets this.

## How to work with me on this

This is a learning project. The author is building it themselves on purpose.

- Do NOT write finished code files or hand over complete solutions unless
  explicitly asked for them in that turn.
- Default to reviewing the author's code, explaining concepts, and pointing at
  the specific thing to fix, not fixing it for them.
- When a concept is not landing, explain it with concrete example values (real
  rows, real return values), not just rules.
- Poke holes. Point out bugs, ordering issues, and design traps, and let the
  author make the edit.
- One clear next action at a time is better than a wall of changes.
- It is fine to run code to verify the author's work and report what passed or
  failed. Verifying is not the same as writing it for them.
- When the author reports having done something, verify the actual state rather
  than taking it as done. Report what is genuinely correct before what is not —
  a commit that half-worked is not a failure.

## Locked conventions

These were decided during Phase 1. Keep them consistent across the codebase.

### Money
Never use floats for money. `DecimalField` everywhere. `base_fee` and invoice
`amount` use `decimal_places=2`. Per-unit prices (`unit_fee`) use
`decimal_places=8` because they can be fractions of a cent. Postgres returns
clean `Decimal` values; the trailing-zeros noise seen in SQLite is a SQLite
artifact only.

### Choices tuples
Format is `(value_stored_in_db, human_label)`. The left side is uppercase snake
(`PAST_DUE`) and is what code compares against and what defaults point to. The
right side is a pretty label (`Past Due`) shown in admin and Swagger. Defaults
always reference the LEFT value. Prefer `models.TextChoices` for new choice sets
so a typo becomes an AttributeError instead of a silently non-matching string.

### related_name
Names the reverse accessor, which always returns a collection of the model the
FK is declared in. Rule: name it after what comes back, which is the plural of
the class you are writing the FK inside. Examples in this schema:
`tenant.subscriptions`, `plan.subscriptions`, `tenant.invoices`,
`subscription.usage_events`, `invoice.usage_events`, `tenant.ledger_entries`,
`invoice.ledger_entries`, `tenant.idempotency_keys`. Never name it after the
parent being pointed at (not `subscribers`, not `customer`, not `bill`).

### on_delete
Financial records use `PROTECT` so a referenced plan, tenant, or invoice cannot
be deleted out from under history. The one exception is `UsageEvent.invoice`,
which is `SET_NULL` (an unbilled event legitimately has no invoice, and voiding
an invoice should release its events, not delete them).

## The ledger (double-entry)

Every movement of money is TWO rows sharing one `transaction_id`, with amounts
that sum to zero (one positive, one negative). The account name is a value in
the `account` column, never a column of its own. Accounts in use:
`ACCOUNTS_RECEIVABLE`, `REVENUE`, `CASH`.

Invariants that tests will assert:
- Sum of `amount` across all ledger rows for a tenant is always 0.
- Outstanding balance = sum of `amount` where account is `ACCOUNTS_RECEIVABLE`.
- Ledger rows are append-only. Never update or delete a row to fix a mistake.
  Write a reversing pair instead.

When a payment succeeds, the idempotency row, the two ledger rows, and the
invoice update must all be written in a single atomic transaction
(`transaction.atomic()`). A partial write breaks the sum-to-zero invariant
permanently.

## Idempotency (the hero feature)

`POST /pay` requires an `Idempotency-Key` header. Correctness relies on the
database unique constraint `(tenant, key)`, NOT on a Python "if exists" check,
because two concurrent requests can both pass a check before either writes.

Flow: claim the key first with `state='PROCESSING'`, then call the mock gateway,
then save `response_status` / `response_body` and set `state='COMPLETED'`. A
second request with the same key returns the saved response and does nothing
else. A second request with the same key but a different `request_hash` is a
client error, not a replay.

Keys are scoped per tenant. The same key string under two different tenants is
allowed and must not collide.

The Phase 3 deliverable is the two curl commands and their output. Capture them
when the endpoint works; they are the demo.

## Testing conventions

Wrap each expected-failure insert in `with transaction.atomic():`. Postgres
poisons the surrounding transaction after an `IntegrityError`, so an unwrapped
assertion breaks every following query in the same test. This differs from
SQLite, so tests must be run against Postgres to be trustworthy.

The three required tests (Phase 5) map directly to Phase 1 shell checks:
double-pay charges once, tenant A cannot read or affect tenant B, ledger sums
to zero.

## Open design decisions

These block or shape later phases. Raise them at the right phase; do not decide
them unilaterally.

1. ~~**How a request identifies its tenant.**~~ **DECIDED 2026-07-30: API key on
   `Tenant`.** Add a unique, indexed `api_key` field to `Tenant`, plus a custom
   DRF authentication class that reads the key from a request header and resolves
   `request.tenant`. No `django.contrib.auth.User` involvement. Rejected: linking
   to `auth.User` (couples billing identity to user accounts and adds a signup
   flow nothing needs yet), and passing the tenant id in the URL path (any caller
   could pass any id, so Phase 4's isolation test would prove nothing).

   **Resolved 2026-07-31, three sub-decisions:**

   - **Header is `Authorization: Api-Key <key>`.** Chosen over `X-API-Key`
     because ecosystem log scrubbers (Sentry, Datadog, proxy configs) redact
     `Authorization` by default while a custom header needs configuring. Django's
     own `DEBUG` error page redacts both — its regex is
     `API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE` — so that was not the
     tiebreaker. Trade-off accepted: Apache + mod_wsgi strips `Authorization`
     unless `WSGIPassAuthorization On`; irrelevant on gunicorn, but Phase 6 must
     not land on mod_wsgi without setting it.
   - **`authenticate()` returns `(tenant, None)`**, so `request.user` *is* the
     `Tenant`. Costs a two-line `is_authenticated` property on the model, which
     is why `IsAuthenticated` works unmodified. `authenticate()` also sets
     `request.tenant` as a readable alias — views should use that, not
     `request.user`. Rejected `(AnonymousUser(), tenant)`: it keeps `request.user`
     honest but forces a custom permission class and makes every call site read
     `filter(tenant=request.auth)`.
   - **API keys stay plaintext at rest.** Deliberate, not an oversight — record
     it in the Phase 6 README. Hashing would be correct for production (a plain
     fast SHA-256, *not* bcrypt/argon2 — the key is 256 bits of entropy, so slow
     hashing buys nothing and adds latency to every request). It was deferred
     because it breaks `default=make_key`: a hashed column cannot hand back the
     raw key, so generation must move to the service layer with a show-once
     contract, plus a `key_prefix` column for admin display. Switching later is
     one migration and one localized change to the creation path.
2. **Where business logic lives.** Invoice generation is called from two places:
   the Phase 2 API endpoint and the Phase 4 background worker (which has no HTTP
   request). Same for the payment/ledger atomic block. If that logic goes inside
   a view, Phase 4 forces a rewrite. A `billing/services.py` that both views and
   tasks call avoids it. Decide before writing views, not after.
3. **One invoice per tenant, or per subscription?** `Invoice` links to `Tenant`
   only, but amount derives from `Plan` via `Subscription`. With one active
   subscription per tenant this is unambiguous; with several it is not.

## Known open items

Carried out of Phase 1:
- Dockerfile `CMD` chains `migrate && runserver` via `sh -c`, so the shell is
  PID 1 and swallows `SIGTERM`. Fine for dev; Phase 6 wants a real entrypoint
  script and a WSGI server instead of `runserver`.
- `requirements.txt` lists `dotenv==0.9.9`. Cosmetic only — verified that package
  ships **no Python module**, just `dist-info` metadata declaring a dependency on
  `python-dotenv`. `from dotenv import load_dotenv` already resolves to
  `python-dotenv 1.2.2`, which is listed directly. Removing it changes nothing
  functionally; the reason to drop it is to stop installing an abandoned
  third-party placeholder on every Docker build and CI run. Low priority.

Resolved during Phase 2 setup:
- Bind mount `.:/app` added to `web`. Before this, `docker compose exec web
  python manage.py check` validated the **stale `COPY . .` snapshot** baked at
  build time and reported "no issues" for code with a `NameError` in it. A green
  check against a stale image is the worst failure mode in this stack — if a
  result looks impossibly good, confirm the container sees your file first.

Needed before Phase 6 deploy:
- `config/settings.py` is stock `startproject`: hardcoded `SECRET_KEY`,
  `DEBUG=True`, empty `ALLOWED_HOSTS`. Docker needs env-var config anyway, so
  doing it now costs nothing extra.

Schema and code cleanups:
- Invoice period uniqueness is enforced on exact `period_start` / `period_end`
  timestamps. If the Phase 4 worker derives these from `timezone.now()`, two
  runs microseconds apart will NOT collide and a duplicate invoice slips
  through. Normalize period boundaries before saving (truncate to a day
  boundary, or switch those fields to `DateField`).
- Nothing prevents attaching a `UsageEvent` to an `Invoice` belonging to a
  different tenant — `UsageEvent` reaches tenant only via `subscription.tenant`,
  and `invoice` is an independent FK. Needs a validation check in the invoice
  generator.
- `Subscription.status` has no default, while `Plan.interval` and
  `IdempotencyKey.state` do.
- No `__str__` on any model yet. Add short ones to make shell debugging
  readable.
- `billing/admin.py` registers nothing. Registering the seven models gives a
  clickable UI for inspecting tenants, invoices, and ledger rows during Phases
  2–3.
- Consider migrating existing choice sets to `TextChoices`.

## Commands

The Docker stack is now the real environment — Postgres only exists inside it,
so anything touching the database has to run there:

```
docker compose up -d --build
docker compose logs -f web
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py test
docker compose exec db psql -U lamehabiba -d multi_tenant_bailing_system_db
docker compose down
```

`docker compose config` validates the compose file and prints resolved `${...}`
values without starting anything. Use it before every `up`.

`migrate` runs automatically on container start (it is in the Dockerfile `CMD`
chain), but it only **applies** files already in `billing/migrations/` — it never
reads the models. After editing a model you must run `makemigrations` yourself to
generate the file. That stays manual on purpose: migrations are reviewed source
code that gets committed, and auto-generating them per environment would produce
conflicting numbering between dev, CI, and prod.

Never `docker compose down -v` once there is data worth keeping — `-v` deletes
the Postgres volume.

The venv at `.venv/` is not activated automatically and is now only good for
offline checks that never touch the database (`manage.py check` will still fail
on `DB_HOST=127.0.0.1` unless Postgres is reachable on the host):

```
.venv/bin/python manage.py check
```

After generating a migration, confirm it contains the two `AddConstraint`
operations (`unique_invoice_period`, `unique_key_per_tenant`) and the
`LedgerEntry` index before applying.

## Repository hygiene

`.gitignore` covers `.venv/`, `__pycache__/`, `*.pyc`, `db.sqlite3`, `.env`.
Tracked file count is 23 as of `94ba8ce` — if it jumps, something got committed
that shouldn't have been.

`requirements.txt` and `.dockerignore` were **gitignored and untracked** until
`94ba8ce`, despite an earlier note here claiming `ce7e443` committed them. A
Dockerized project cannot ignore `requirements.txt` — `COPY requirements.txt`
is the whole build, so a fresh clone could not build at all. Both are tracked
now.

Related: `requirements.txt` was once clobbered by a bare `pip freeze` run with
the venv deactivated, which wrote the host's Ubuntu system packages into it and
broke the image build on `bcc==0.29.1`. Generate deps with `.venv/bin/pip
freeze`, never bare `pip freeze`. Because the file was untracked at the time,
git could not restore it.

`.env` holds real credentials and is gitignored, but `docker-compose.yml`
references it through `${...}` interpolation, so a fresh clone has no database
config. Phase 6's README needs to say which keys are required.

Note that `.gitignore` only affects **untracked** files; anything already in the
index keeps being tracked until `git rm -r --cached` removes it. The `.venv` and
`db.sqlite3` blobs still exist in the older commits `e8166d4` and `41fb8c3`.
Purging them would mean rewriting all history — not worth it here, and nothing
new is being added.
