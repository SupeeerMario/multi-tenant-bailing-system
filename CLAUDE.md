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
| 2 — Core endpoints | Create tenant → assign plan → record usage → generate correct invoice, all via API | **In progress** — auth layer done, `generate_invoice` done bar the ledger pair, no endpoints yet |
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

All of the above is committed as `fcd5daa`. Working tree clean as of 2026-07-31.

#### `billing/services.py` — `generate_invoice` (2026-08-04)

Open decision 2 answered by building it. Untracked (`?? billing/services.py`),
not committed yet. Holds `BillingError` base plus `NoActiveSubscription` and
`InvoiceAlreadyExists` subclasses, and `generate_invoice(tenant, period_start,
period_end)` returning the `Invoice`.

What it does, in order: resolve the tenant's one ACTIVE subscription via `get()`
(raises `NoActiveSubscription`), read `plan` off it, select usage events in the
window, freeze their ids, sum `quantity`, compute `amount`, then write the
invoice and stamp the events in a single `transaction.atomic()` block.

Verified live against Postgres — Acme July `Decimal('30.00')` (`20.00 + 0.0025 ×
4000`), Acme September `Decimal('20.00')` (no usage, base fee only), Globex July
`Decimal('36.67')` (`36.665` rounded up), Initech `NoActiveSubscription`. Second
call on an already-invoiced window raises `InvoiceAlreadyExists` and writes
nothing; `except BillingError` catches it, so the Phase 4 worker's skip-and-
continue loop will work.

Decisions made while building:

- New invoices are `status='OPEN'` — finalized and owed. `DRAFT` would imply a
  finalize step nothing has. Phase 3's `/pay` flips it to `PAID`.
- `currency=plan.currency`, not the model default. The `'USD'` default exists
  for admin-created rows and is not the source of truth for a generated invoice.
- Window is half-open: `occurred_at__gte=period_start, occurred_at__lt=period_end`.
  With `__lte`, an event landing exactly on `period_end` bills in two periods.
  Seed data has one at exactly `2026-08-01 00:00` (qty 7777) that exists to prove
  this — it is excluded from July and included in August, billed once.
- Errors are raised, never returned. A function returning an `Invoice` on success
  and a string on failure forces every caller to `isinstance` check, and a worker
  that forgets logs the string as a success.
- `amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)` before it touches
  `Invoice.amount`. `unit_fee` is 8 decimal places so the product is too; without
  the quantize, Postgres rounds money into the 2-place column by its own rule.
  Globex's `36.665` is the live case.

Traps hit, worth not re-learning:

- **A QuerySet is a recipe, not rows.** `.aggregate()` and `.update()` each send
  fresh SQL, and `.count()`/`.exists()`/`.aggregate()`/`.update()` never use the
  iteration cache. Filtering by window and then evaluating twice means an event
  inserted in between is stamped `invoice_id` without ever being charged —
  reproduced live: aggregate matched `[9]`, the later update touched `[9, 10]`.
  Fix in place: `events_ids = list(qs.values_list('id', flat=True))` forces one
  evaluation, then `frozen = UsageEvent.objects.filter(id__in=events_ids)` gives
  a `WHERE id IN (1, 2, 3)` that cannot grow. `transaction.atomic()` alone does
  **not** fix this — READ COMMITTED gives each statement a fresh snapshot.
- **Two `atomic()` blocks are two transactions.** Wrapping the create in one and
  the update in another leaves the exact gap the block was meant to close. One
  block around both writes.
- **The `except IntegrityError` goes outside the `with`.** Catching it inside and
  continuing leaves an aborted Postgres transaction where every later query
  fails. Confirmed live: with the inner block present, queries still worked after
  the catch, because the inner `atomic()` rolled back to a savepoint.
- `create()` is keyword-only — `create(amount, ...)` gives `QuerySet.create()
  takes 1 positional argument but 2 were given`.
- `create()` raises `IntegrityError`, never `DoesNotExist`. `DoesNotExist` is
  `get()` finding nothing — the opposite problem. An `except Model.DoesNotExist`
  around a `create()` is unreachable code and the real error leaks out raw.
- `filter()` returns a QuerySet and is never `None`, so `if qs is None` is dead.
  Same for `.get()`, which raises rather than returning falsy — `if not obj`
  after a `get()` never fires. Third and fourth times this shape appeared.
- `aggregate()` returns `{'total': None}`, not `0`, when nothing matched. Needs
  `or 0`; `None * unit_fee` is a `TypeError`. Zero usage is not an error — the
  tenant still owes `base_fee`.
- f-strings use `{}`, not `${}`. `f"tenant ${tenant.id}"` printed `tenant $15`,
  which reads as a dollar amount in billing logs.
- Postgres sequences do not roll back on a failed INSERT, so invoice ids skip
  after each rejected duplicate. Cosmetic.

Still missing before `generate_invoice` is finished: **the ledger pair.** The
invoice is written but `LedgerEntry` is still empty, so an `OPEN` invoice claims
money owed while the ledger says zero — and Phase 5's "ledger sums to zero" test
would pass on an empty table. Two rows inside the same `atomic()` block, sharing
**one** `transaction_id`, carrying `tenant`, `invoice`, `currency`, description:
`ACCOUNTS_RECEIVABLE` `+amount` and `REVENUE` `-amount`.

#### Seed data currently in the dev database

Committed rows (not fixtures — created ad hoc, will need a real fixture or
factory for Phase 5). Tenants `13 Acme`, `14 Globex`, `15 Initech` (no
subscription, exists to test `NoActiveSubscription`). Plan `2 Standard`,
`base_fee 20.00`, `unit_fee 0.00250000`. Subscriptions `8` (Acme, ACTIVE), `9`
(Globex, ACTIVE). Six `UsageEvent` rows: Acme `1000/2500/500` inside July,
`9999` in June, `7777` exactly on `2026-08-01 00:00`; Globex `6666` on
`2026-07-10`. Four invoices exist. Acme api_key
`VJqUYEgkyQUS6iSN4rcpYzjcHzPDos8PbSHaLIj7zAI`.

Events 1, 2, 3, 5 have `invoice_id` NULL despite being billed — their invoices
were generated before the stamping line existed. Stale, not a live bug.

Not started, in order:

1. Finish `generate_invoice` with the ledger pair, then commit `services.py`.
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
- Do not open a task with a full implementation spec. Tried during the Phase 2
  auth work and it did not land — the author said so directly. What worked:
  one step, then a live run in the container printing real values, then the next
  step. Save the sharp edges (bytes vs text, 401 vs 403) for the moment the
  author's code actually hits them, not up front.

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
2. ~~**Where business logic lives.**~~ **DECIDED 2026-08-04: `billing/services.py`,
   built.** Invoice generation is called from two places — the Phase 2 API
   endpoint and the Phase 4 background worker (which has no HTTP request) — so
   putting it in a view would force a Phase 4 rewrite. Everything in
   `services.py` takes a `Tenant` object and plain values, never `request`.
   Failures are custom exceptions (`BillingError` and subclasses) that the view
   maps to status codes and the worker catches to skip a tenant; no HTTP
   concepts, including `Http404`, cross into the service layer. See the Phase 2
   section above for what `generate_invoice` does and what is still missing.
3. ~~**One invoice per tenant, or per subscription?**~~ **DECIDED 2026-07-31:
   one active subscription per tenant, enforced (option 1).** `Invoice` stays
   tenant-level, the generator is `generate_invoice(tenant, period_start,
   period_end)`, and no migration to `Invoice` is needed. Cost is a rule that
   blocks a second `ACTIVE` subscription for the same tenant. Record it in the
   Phase 6 decision note as a deliberate simplification.

   Why the other two were rejected: **invoice per subscription** needs a
   `subscription` FK on `Invoice` plus a widened `unique_invoice_period` before
   a single endpoint works; **one invoice with `InvoiceLine` rows** is the most
   correct model for real billing but is past what Phase 2 needs.

   The problem it solves, concretely: with two active subscriptions in one
   window, a tenant-level generator has two `Plan` rows feeding one `amount`,
   and `unique_invoice_period` on `(tenant, period_start, period_end)` forbids
   writing a second row for the same window — so the second invoice is an
   `IntegrityError`, not a second bill.

   **Enforced and verified 2026-07-31.** `Subscription.Meta.constraints` holds
   `UniqueConstraint(fields=['tenant'], condition=Q(status='ACTIVE'),
   name='unique_active_subscription_per_tenant')`, and `Subscription.status`
   gained `default='ACTIVE'`. Migration
   `0004_alter_subscription_status_and_more` carries the `AlterField` plus the
   `AddConstraint`, and is applied. Postgres renders it as a partial index:
   `CREATE UNIQUE INDEX unique_active_subscription_per_tenant ON
   public.billing_subscription USING btree (tenant_id) WHERE
   ((status)::text = 'ACTIVE'::text)`.

   It is a DB constraint, not a Python `if exists` check, for the same reason
   as idempotency: two concurrent requests can both pass a check before either
   writes.

   Verified live against Postgres, all in one rolled-back transaction: second
   ACTIVE row rejected whether the period differs or matches; a different
   tenant's ACTIVE row unaffected; cancel then re-subscribe works; extra
   CANCELED and PAST_DUE rows pile up freely because non-matching rows are not
   in the partial index at all.

   Traps hit getting there:

   - `UniqueConstraint` requires `name=`. Without it: `ValueError: A unique
     constraint must be named.`
   - `condition` is a keyword argument **on** `UniqueConstraint`, not a
     separate constraint. Splitting it into a companion
     `CheckConstraint(Q(status='ACTIVE'))` makes `CANCELED` and `PAST_DUE`
     unstorable table-wide, so nothing can ever be canceled, and leaves the
     `UniqueConstraint` unconditional — one subscription per tenant forever, so
     re-subscribing after a cancel collides with the dead row.
   - Django 6.0 removed `CheckConstraint(check=...)`; the argument was renamed
     to `condition` in 5.1. Django here is 6.0.7.
   - The condition compares against the **left** value `'ACTIVE'`, what the
     column stores — not the `'Active'` label.

   Consequence for the endpoints: a second active subscription now surfaces as
   an `IntegrityError` from the database, not a validation error. The
   assign-plan path has to catch it and turn it into a 409, and canceling must
   move the old row off `ACTIVE` before inserting the new one.

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
- ~~`Subscription.status` has no default~~ — fixed in `6c95a9f`,
  `default='ACTIVE'`.
- No `__str__` on any model yet. Add short ones to make shell debugging
  readable. Live output is still `<Subscription: Subscription object (8)>`,
  which made every shell check during the `services.py` work harder to read.
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
Tracked file count is 24 as of `fcd5daa` — if it jumps, something got committed
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
