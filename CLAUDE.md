# CLAUDE.md

Guidance for Claude when working on this repository.

## What this project is

A multi-tenant billing API built in Django. It charges companies monthly, keeps
each company's data isolated, and never double-charges. The full spec is a
six-phase build (data model + Docker, core endpoints, idempotent payments,
multi-tenancy + worker, tests + docs, deploy).

## Start here next session

Decided 2026-08-08, before anything else is picked up. Both are under
"Still not started, in order" in the Phase 2 section; the full reasoning is
there.

1. **Move the closed-window guard out of `InvoiceAPIView` and into the service
   layer.** Today the check lives in the view, so the Phase 4 worker calls
   `generate_invoice` directly and bills periods that have not ended —
   reintroducing exactly the bug decision 6 exists to prevent. A service function
   holding *fetch active subscription → refuse if not closed → generate →
   advance* gives the view and the worker one shared path. This is the only
   queued item that is a correctness bug rather than a cleanup.
2. **Add `CheckConstraint(current_period_end > current_period_start)` to
   `Subscription`.** Subscription 11 (Initech) is `Aug 1 → Aug 1` and bills a
   `base_fee`-only invoice with real ledger rows against a window that can match
   no usage event. Needs a migration, and the existing bad row has to be fixed
   before `AddConstraint` will apply.

Do these two first, in this order. Everything else in that list is quality work
and can wait.

## Phase gating — read this first

**Do not start work on a phase until the previous phase meets its "Done when"
in full.** Each phase has an explicit completion condition, and the schema being
in place is not the same as the phase being finished. If the author asks for
Phase N work while Phase N-1 is incomplete, say so and name the specific
remaining item rather than going along with it.

| Phase | "Done when" | Status |
|---|---|---|
| 1 — Data model + skeleton | `docker compose up` starts API + Postgres, migrations create all tables | **Done** (verified 2026-07-30) |
| 2 — Core endpoints | Create tenant → assign plan → record usage → generate correct invoice, all via API | **"Done when" met** (verified 2026-08-08) — full flow runs over HTTP. Auth layer, `generate_invoice`, and five endpoints done (tenant, plan, subscription, usage read+write, generate-invoice). Remaining cleanup, not blocking Phase 3: invoice/ledger read endpoints, pagination, ordering |
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

### Phase 2 — where it stands (last verified 2026-08-07)

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

#### `billing/services.py` — `generate_invoice` (2026-08-04, ledger pair 2026-08-05)

Open decision 2 answered by building it. **Complete and committed as `7973e99`.**
Holds `BillingError` base plus `NoActiveSubscription` and
`InvoiceAlreadyExists` subclasses, and `generate_invoice(tenant, period_start,
period_end)` returning the `Invoice`.

What it does, in order: resolve the tenant's one ACTIVE subscription via `get()`
(raises `NoActiveSubscription`), read `plan` off it, select usage events in the
window, freeze their ids, sum `quantity`, compute `amount`, then — in a single
`transaction.atomic()` block — write the invoice, stamp the events, and write
the two `LedgerEntry` rows.

Verified live against Postgres — Acme July `Decimal('30.00')` (`20.00 + 0.0025 ×
4000`), Acme August `Decimal('39.44')` (`20.00 + 0.0025 × 7777`), Acme September
`Decimal('20.00')` (no usage, base fee only), Globex July `Decimal('36.67')`
(`36.665` rounded up), Initech `NoActiveSubscription`. Second call on an
already-invoiced window raises `InvoiceAlreadyExists` and writes nothing —
confirmed by row count, the `IntegrityError` fires on the invoice `create()`
before either ledger row. `except BillingError` catches both, so the Phase 4
worker's skip-and-continue loop will work.

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
- The ledger pair is `ACCOUNTS_RECEIVABLE +amount` / `REVENUE -amount`, both
  carrying `tenant`, `invoice`, `currency`, and one shared `transaction_id`
  generated into a local before the two `create()` calls. Ledger rows go after
  the invoice `create()` on purpose: a duplicate window trips the
  `IntegrityError` on the invoice, so no half-pair can exist even before the
  rollback.

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
- **A `default=` callable fires once per row.** `transaction_id` is
  `UUIDField(default=uuid.uuid4, ...)`, so two `create()` calls produced two
  different uuids and the "pair" was two unrelated one-sided rows. Caught live:
  `distinct transaction_ids: 2`. `sum == 0` still passed, because summing the
  whole table hides it — the pair link is what breaks. Same mechanism as
  `default=make_key` on `Tenant.api_key`, where per-row evaluation is the
  desired behavior.
- **Bare `uuid.uuid4` is the function, not a uuid.** Passing it as a *value*
  gives `ValidationError: ['"<function uuid4 at 0x...>" is not a valid UUID.']`.
  Bare is correct for `default=` (Django calls it); a value needs the `()`.
  And calling `uuid.uuid4()` inline in both `create()` calls is back to two
  distinct uuids — it has to go into a local first.

**Ledger pair verified live 2026-08-05.** Per invoice: exactly 2 rows, 1
distinct `transaction_id`, pair summing to `0.00`. Per tenant: total ledger sum
`0`, and `sum(ACCOUNTS_RECEIVABLE)` equals the sum of that tenant's `OPEN`
invoices (`89.44` Acme, `36.67` Globex). No ledger row whose `tenant` disagrees
with its invoice's tenant.

Still open on the ledger rows: `description` is never set, so both rows land
with `''`. Not an error (`blank=True`), but Phase 3 wants something readable
like `f"Invoice {invoice.id} {period_start.date()}–{period_end.date()}"`.

#### Seed data currently in the dev database

Committed rows (not fixtures — created ad hoc, will need a real fixture or
factory for Phase 5). Tenants `13 Acme`, `14 Globex`, `15 Initech` (no
subscription, exists to test `NoActiveSubscription`). Plan `2 Standard`,
`base_fee 20.00`, `unit_fee 0.00250000`. Subscriptions `8` (Acme, ACTIVE), `9`
(Globex, ACTIVE). Six `UsageEvent` rows: Acme `1000/2500/500` inside July,
`9999` in June, `7777` exactly on `2026-08-01 00:00`; Globex `6666` on
`2026-07-10`. Acme api_key
`VJqUYEgkyQUS6iSN4rcpYzjcHzPDos8PbSHaLIj7zAI`.

**Re-seeded 2026-08-05.** The original four invoices predated both the
event-stamping line and the ledger pair, so they had zero `LedgerEntry` rows and
left events 1/2/3/5 unstamped — a tenant owing `89.44` read as AR `0.00`. Fixed
by deleting all four and regenerating through `generate_invoice`, which is the
only path that writes a correct ledger. Deleting was safe because
`UsageEvent.invoice` is `SET_NULL` (releases events rather than blocking) and no
ledger rows existed to trip `PROTECT`. The regenerated amounts came back
identical to the deleted ones, which is itself a check on the generator.

Current invoices are `17` (Acme July `30.00`), `18` (Acme August `39.44`), `19`
(Acme September `20.00`), `20` (Globex July `36.67`) — ids jumped because
Postgres sequences do not roll back. Eight ledger rows total. Events 1/2/3 → 17,
5 → 18, 6 → 20; event 4 (`9999`, June) is correctly NULL, genuinely unbilled.

Do not hand-backfill ledger rows if this drifts again — regenerate through the
service. The ledger is append-only and meant to be produced by the code path.

**Endpoint testing garbage cleaned out 2026-08-07.** Probing the three new
endpoints created tenants `21/22/23` and eight throwaway plans; all deleted, in
dependency order (subscriptions, then tenants, then plans — `PROTECT` blocks the
reverse). Invoices, ledger rows, and usage events were never touched and still
read `4 / 8 / 6`.

Two pieces of drift this exposed, both still present:

- **Initech (`15`) now has an ACTIVE subscription, `11`.** The line above says it
  exists to test `NoActiveSubscription`. It no longer does — `generate_invoice`
  will happily bill it. Cancel subscription `11` to restore the fixture's purpose.
- Tenant `20 test name` exists with subscriptions `20` (CANCELED) and `22`
  (ACTIVE), left from the constraint work. Harmless, but it is not in any seed
  description.

This is the second time ad-hoc rows have drifted from what this file claims. A
real fixture or factory is Phase 5 work, but the cost is being paid now.

**Cleaned again after the usage-endpoint work (2026-08-07).** Usage events
`21`–`27` and tenant `26 NoSubCo` deleted; all seven events were unbilled
(`invoice_id` NULL) so nothing was released from an invoice. Baseline confirmed
back to **6 usage events, 4 tenants, 4 invoices, 8 ledger rows, 1 plan**. Use
those five numbers as the check that the dev database is clean.

#### First three endpoints (2026-08-06, `c3086cb` + `13f695e`)

`serializers.py`, `views.py`, `urls.py`, `admin.py` all have content now. Routes
sit under the `billing/` prefix from `config/urls.py`:

| Route | View | Permission | Serializer |
|---|---|---|---|
| `POST billing/tenants/` | `TenantCreateView` (`CreateAPIView`) | `AllowAny` | `TenantSerializer` |
| `POST billing/plans/` | `PlanCreateView` (`CreateAPIView`) | `AllowAny` | `PlanSerializer` |
| `POST billing/subscriptions/` | `SubscriptionsCreateView` (`CreateAPIView`) | `IsAuthenticated` | `SubscriptionsSerializer` |
| `GET/POST billing/usage/` | `UsageEventListCreateView` (`ListCreateAPIView`) | `IsAuthenticated` | `UsageEventSerializer` |
| `POST billing/invoice/` | `InvoiceAPIView` (plain `APIView`) | `IsAuthenticated` | `InvoiceSerializer` (output only) |

Decisions visible in the code:

- `TenantSerializer` exposes `name`, `api_key`, `is_active`; the last two are
  `read_only`. Read-only still **renders in the response**, so create-tenant
  hands back the generated `api_key` exactly as open decision 4 requires — the
  flag only blocks it as input.
- `SubscriptionsSerializer` marks `tenant` and `status` read-only. `tenant` comes
  from `request.tenant` in `perform_create`, never from the body; `status` falls
  back to the model `default='ACTIVE'`.
- `SubscriptionAlreadyExists(APIException)` lives in `views.py` with
  `status_code = 409`. `perform_create` wraps `serializer.save(tenant=tenant)` in
  `try/except IntegrityError` and raises it, which answers the old Phase 2 item 3.
- `admin.py` registers all seven models. `__str__` added to `Tenant`, `Plan`,
  `Subscription` in `13f695e`.

**Verified live 2026-08-07** against the running stack, real curls:

| Case | Result |
|---|---|
| `POST tenants/` no auth | 201, `api_key` in body |
| `POST plans/` no auth | 201 |
| `POST subscriptions/` valid key | 201, `tenant` = the key's tenant |
| second ACTIVE sub, same tenant | 409 `"Conflict. This tenant already has an active subscription"` |
| no `Authorization` header | 401 |
| missing body fields | 400, field-keyed |
| `{"plan": 9999}` | 400 `Invalid pk "9999" - object does not exist.` |

Isolation proved on the write path: a request carrying tenant A's key with
`"tenant": 15` in the body created the row under **A**, not 15. `read_only` on
`tenant` plus `perform_create` overriding it is what makes body-spoofing a no-op.

Why the bare `except IntegrityError` is safe today: `ATOMIC_REQUESTS` is not set,
so the request runs in autocommit and the failed INSERT is its own transaction.
Enabling `ATOMIC_REQUESTS` later would break it — the surrounding transaction
would already be poisoned when the `except` runs. Also note DRF's
`PrimaryKeyRelatedField` validates the `plan` FK during `is_valid()`, so a bad
plan returns 400 and never reaches `save()`;
`unique_active_subscription_per_tenant` really is the only integrity error that
can currently reach that handler. It becomes a liability the moment a second
constraint lands on `Subscription`.

#### Negative money closed (2026-08-07, `04bfbfd` + `73af90a`)

Found by probing the live plan endpoint: `POST billing/plans/` with
`base_fee: "-99.00"` returned **201**. Nothing validated fee sign. A plan at
`base_fee -99.00, unit_fee 0.001` with 1000 units yields `amount = -98.00`, and
`generate_invoice` writes `ACCOUNTS_RECEIVABLE -98.00` / `REVENUE +98.00`. Both
ledger invariants in this file still **pass** — the pair sums to zero and the
tenant total sums to zero. Only "outstanding balance = sum where
`ACCOUNTS_RECEIVABLE`" exposes it, reading `-98.00`: you owe the customer. A
sum-to-zero test goes green on corrupt data.

Fixed in three layers, each doing a different job:

| Layer | Covers | Produces |
|---|---|---|
| `PlanSerializer.validate_base_fee` / `validate_unit_fee` | HTTP only | 400, field-keyed message |
| `Plan.clean()` | admin, explicit `full_clean()` | `ValidationError` |
| `CheckConstraint` `base_fee__gte=0`, `unit_fee__gte=0` | everything, including shell and the Phase 4 worker | `IntegrityError` |

The serializer is for the message; **the constraint is the guarantee**. Migration
`0006_remove_plan_prevent_negative_base_fee_and_more` carries the final `>= 0`
pair. Verified live: negatives 400 over HTTP and `IntegrityError` from a raw
`Plan.objects.create()`, valid plans 201.

`>= 0`, not `> 0` — free (`base_fee 0`) and flat-rate (`unit_fee 0`) plans are
both legitimate products. Billed end to end to confirm: free plan with 1000 units
at `unit_fee 0.005` → `amount 5.00`, ledger `[5.00, -5.00]`, sum `0.00`. Zero
usage on a free plan → `amount 0.00` and a `0.00`/`0.00` ledger pair, which
balances but records no movement of money. Decide later whether a zero invoice
should write a pair at all; not a Phase 2 problem.

Traps hit, worth not re-learning:

- **Django 6.0 has no `CheckConstraint(check=...)`** — renamed to `condition=` in
  5.1. `TypeError: CheckConstraint.__init__() got an unexpected keyword argument
  'check'`. This is an **import-time** error, so nothing runs: not
  `makemigrations`, not `check`, not the server. Second time this rename has bitten
  (see decision 3).
- **A failed migration kills the web container**, `Exited (1)`, because the
  Dockerfile `CMD` is `migrate && runserver`. A bad migration means no server at
  all, not just an unapplied change. That is the PID-1/entrypoint item under Known
  open items showing its teeth.
- **`AddConstraint` validates existing rows.** A leftover `base_fee -99.00` row
  gave `check constraint "prevent_negative_base_fee" of relation "billing_plan" is
  violated by some row`. Clean the data before adding the constraint.
- **Editing a constraint's `condition` changes no DDL until `makemigrations`.**
  Model said `>= 0` while Postgres still enforced `> 0`. Django cannot ALTER a
  check constraint, so the generated migration is drop-then-recreate — four
  operations for two constraints.
- **DRF never calls `full_clean()`.** `ModelSerializer.save()` goes straight to
  `objects.create()`, so `Model.clean()` is dead code on the API path — it only
  fires from admin. `serializer.is_valid()` returned `True` on `base_fee -99.00`
  and the request 500'd on the DB constraint. Model `validators=` would be picked
  up (ModelSerializer copies them onto the generated field); `clean()` is not.
- **A `validate_<field>` method must `return value`.** It is a transformer, not a
  predicate. Falling off the end returns `None`, DRF stores that in
  `validated_data`, and *every valid request* 500s on
  `null value in column "base_fee" ... violates not-null constraint`. The invalid
  cases keep working because they `raise` before returning — so the bug hides
  behind passing negative tests.
- **`CHECK` constraints are blind to NULL.** `NULL >= 0` is UNKNOWN, and a CHECK
  only fails on explicit false, so `CHECK (base_fee >= 0)` let the NULL through.
  `NOT NULL` caught it. Every check constraint needs NULL considered separately.
- **Field-level validators must raise a bare message, not a dict.** DRF already
  keys the error by field name from the method name, so
  `ValidationError({'base_fee': '...'})` double-nests into
  `{"base_fee":{"base_fee":"..."}}`. The dict form belongs in `Model.clean()` and
  in serializer-level `validate()`, which have no field context.
- **`{'key': 'value'}` without the braces is a `SyntaxError`.** Removing the dict
  wrapper but keeping the colon gives
  `ValidationError('base_fee' : 'message')` — `key: value` is legal only inside
  `{}`. Another import-time failure that takes the container down.
- **`Decimal('0')` is falsy.** `if self.base_fee and self.unit_fee:` in
  `Plan.clean()` skips zero entirely — the exact value the rule now cares about.
  Guard on `is not None`, never truthiness, for numeric fields.
- **DRF collects all field validators; `Model.clean()` stops at the first raise.**
  Sending both fees negative returns both errors from the serializer, but
  `clean()` would report only one.

#### Usage endpoint, read and write (2026-08-07)

`GET/POST billing/usage/` → `UsageEventListCreateView`. `id` also added to all
four serializers' `fields`, which unblocks the API-only flow (a caller can now
learn a plan's id from the create response instead of reading it out of psql).

The write path, and why it looks different from the subscription one:
`UsageEvent` has **no `tenant` column** — it reaches a tenant only through
`subscription.tenant`. So the `read_only` + `save(tenant=...)` trick does not
transfer; there is no field to override. Instead `perform_create` resolves the
tenant's one ACTIVE subscription itself and passes it as the save kwarg:

```python
sub = models.Subscription.objects.get(tenant=tenant, status='ACTIVE')
serializer.save(subscription=sub)
```

`subscription` and `invoice` are both `read_only`, so neither can be client
supplied. `invoice` is not "read-only input" so much as *not part of creating a
usage event at all* — it starts NULL and `generate_invoice` stamps it later via
`frozen.update(invoice=invoice)`. Event 4 (`9999`, June) is permanently NULL and
that is correct data, not missing data.

The read path uses `get_queryset`, not a `queryset` class attribute:

```python
return models.UsageEvent.objects.filter(subscription__tenant=self.request.tenant)
```

`subscription__tenant` spans the FK in one join because there is no direct
column. The class attribute was deliberately **removed** — `objects.all()` is
harmless on a POST-only view but serves every tenant's rows the moment GET works.
`get_queryset` is the hook with `self.request` in scope.

**Verified live 2026-08-07**, both verbs:

| Case | Result |
|---|---|
| `POST` valid | 201, `subscription 8`, `invoice null` |
| `POST {"subscription": 9}` (another tenant's) | 201, landed on **8** — spoof ignored |
| `POST {"invoice": 20}` | 201, `invoice` stayed `null` |
| `POST` missing fields | 400 on `metric` / `quantity` / `occurred_at` |
| `POST {"quantity": -5}` | 400 `Ensure this value is greater than or equal to 0.` |
| `POST`, tenant with no ACTIVE sub | 409 `No active subscription found for the current tenant` |
| `GET` as Acme | 200, 12 rows, all `subscription 8` |
| `GET` as Globex | 200, 1 row (event 6) |
| `GET` as a tenant with no subscription | 200 `[]` |
| `GET`/`POST` no auth | 401 |

Row counts matched the DB exactly (13 → 12 events, 14 → 1). **This is the first
proof of the read half of tenant isolation** — every earlier check only proved a
tenant could not *affect* another's data. Phase 4's test targets this view.

`quantity: -5` returning 400 is free: `PositiveIntegerField` carries a
`MinValueValidator(0)` and `ModelSerializer` copies model-field validators onto
the serializer field. Same mechanism noted under the fee work — `validators=`
transfers to DRF, `clean()` does not.

Note the deliberate asymmetry: a tenant with no active subscription gets **200
`[]` on GET** but **409 on POST**, same URL. An empty collection is a successful
read; recording usage with nothing to attach it to genuinely cannot proceed.

Traps hit, worth not re-learning:

- **`Meta.model`, not `Meta.models`.** The typo just sets an unused attribute, so
  import succeeds and it fails lazily on first use with `AssertionError: Class
  UsageEventSerializer missing "Meta.model" attribute`. Easy to conflate because
  `from . import models` puts the plural in scope in the same file.
- **`status.HTTP_404_NOT_FOUND` is the integer `404`**, not an exception.
  `raise` on it gives `TypeError: exceptions must derive from BaseException`.
  That module is a bag of named numbers; raising needs an `APIException` subclass.
- **`serializers.save(...)` vs `serializer.save(...)`.** The module is imported as
  `serializers` in `views.py`, which shadows the parameter name by one letter.
  `hasattr(serializers, 'save')` is `False`, so it is an `AttributeError` 500.
- **`get_queryset()`, not `list()`.** `list()` builds the HTTP response
  (paginate → serialize → `Response`); returning a raw QuerySet from it breaks
  rendering. `get_queryset()` answers "which rows" and lets DRF do the rest.
- **A comment is not a statement.** `if not tenant:` followed only by `# ...` is
  `IndentationError: expected an indented block`. Import-time, so nothing runs.
- **That `if not tenant` was dead anyway.** `IsAuthenticated` runs before the
  handler, so an absent tenant is a 401 and the method is never entered. Fifth
  time this shape has appeared in the project (see the `services.py` notes on
  `filter()` never being `None` and `get()` never returning falsy). When writing
  the next `if not ...`, ask what would have to fail upstream for it to fire.
- **The `runserver` reloader can miss bind-mount edits.** A `NameError` for a
  renamed class survived two runs while `docker compose exec web grep` showed the
  container reading the **new** file — the process was serving stale bytecode.
  Sibling of the stale-`COPY` trap below, different mechanism: if a result
  contradicts source you just read, confirm the process reloaded, not just the
  file. `docker compose restart web`.

#### Generate-invoice endpoint (2026-08-08, `ff871e1`)

`POST billing/invoice/` → `InvoiceAPIView`. **Phase 2's "Done when" is met with
this** — tenant → plan → subscription → usage → correct invoice all run over
HTTP now.

**The billing window comes from the subscription, not the request.** Open
decision 6 below records why. The endpoint takes **no request body at all**:
it resolves the tenant's one ACTIVE subscription, reads
`current_period_start` / `current_period_end` off it, refuses if that period
has not ended, and hands those two values to `generate_invoice`. After a
successful invoice the service advances the subscription one month, so the next
call bills the next cycle.

Shape of `post()`, and why each piece exists:

- `Subscription.objects.get(tenant=request.tenant, status='ACTIVE')` wrapped in
  `except Subscription.DoesNotExist` → `views.NoActiveSubscription` (409).
- Guard: `if sub.current_period_end > timezone.now(): raise PeriodNotEnded(...)`.
  Closed windows only — see the trap notes for what billing an open window costs.
- `services.generate_invoice(...)` wrapped in `except services.InvoiceAlreadyExists`
  and `except services.NoActiveSubscription`, both re-raised as the `APIException`
  twins (409).
- `Response(serializers.InvoiceSerializer(invoice).data, 201)`.

`InvoiceSerializer` is a `ModelSerializer` over all nine `Invoice` fields, used
**output-only** in both this view and the coming list view — nothing is ever
deserialized through it, so `read_only` is documentation here rather than
enforcement. It is also why this is a plain `APIView` and not `CreateAPIView`:
`CreateAPIView.create()` does `get_serializer(data=request.data)` → `is_valid()`
→ `perform_create()`, and there is no input to validate and no object to save
through the serializer.

Two new `APIException` subclasses in `views.py`, both 409: `PeriodNotEnded` and
`InvoiceAlreadyExists`. 409 not 400 — there is no request body, so nothing the
client sent can be wrong; the conflict is with server state.

**Verified live 2026-08-08** against the running stack. Four POSTs on a throwaway
tenant seeded at `May 1 → Jun 1`, with usage `1000` in May and `2000` in June:

| Call | Result | Window | Amount |
|---|---|---|---|
| 1 | 201 | May 1 → Jun 1 | `22.50` |
| 2 | 201 | Jun 1 → Jul 1 | `25.00` |
| 3 | 201 | Jul 1 → Aug 1 | `20.00` (no usage, base fee) |
| 4 | 409 | — | `Cannot make a bill for 2026-09-01 ... as the period has not ended` |

Subscription ended at `Aug 1 → Sep 1`, which is open, so it stops on its own.
Ledger: 6 rows, sum `0.00`, AR `67.50` — the three invoices. May's event stamped
to the May invoice, June's to the June invoice. Also confirmed against the real
seed rows: Acme → 409 carrying the service's own message (`tenant 13 has already
been invoiced a bill from 2026-07-01 ... to 2026-08-01 ...`), `test name`
(period ends Sep 1) → 409 period not ended, no `Authorization` → 401.

All throwaway rows deleted afterwards. Baseline reconfirmed at
**4 tenants / 1 plan / 5 subscriptions / 6 usage events / 4 invoices / 8 ledger
rows**.

Decisions made while building:

- **Exception messages: detail at the source, generic at the boundary.** The
  service messages carry `tenant.id` and the period because the Phase 4 worker
  logs them in a loop over every tenant — `"tenant has already been invoiced"`
  with no ids produces N identical useless log lines. Stripping them from
  `services.py` was tried and reverted. What the client sees is the view's call:
  `raise InvoiceAlreadyExists(e)` forwards the service text, bare
  `raise InvoiceAlreadyExists()` uses `default_detail`. Safe to forward here
  because the message only names the caller's own tenant and period — not a
  blanket rule, and Phase 3's payment errors will carry gateway text that must
  not be forwarded.
- The period advance lives **inside `generate_invoice`'s existing
  `transaction.atomic()` block**, not in the view and not in a second block.
  Invoice committed but period not advanced is an infinite bill loop; period
  advanced but invoice rolled back skips a month silently.
- New start is the **old end**, so windows tile with no gap and no overlap,
  matching the half-open `[start, end)` the generator already uses.
- `python-dateutil==2.9.0.post0` (plus its `six` dependency) added to
  `requirements.txt` for `relativedelta`. Image must be rebuilt, not restarted.

Traps hit, worth not re-learning:

- **`relativedelta(month=1)` is the absolute form — it *sets* the month to
  January.** The relative form is plural, `months=1`. Both are accepted, neither
  errors. `2026-08-01 + relativedelta(month=1)` is `2026-01-01`, seven months
  backwards. Shipped briefly; the cascade is worth understanding because nothing
  raises: the subscription lands `start Aug 1 2026 / end Jan 1 2026` (end before
  start), the closed-window guard still passes because Jan 1 is in the past, the
  next call bills `[Aug 1, Jan 1)` which matches no events and writes a
  base-fee-only invoice with real ledger rows, then advances to `Jan 1 → Jan 1`
  and bills a zero-length window before sticking on the duplicate constraint
  forever. Two garbage invoices, then permanently wedged.
- **`Model.save()` does not take field values.** Its signature is
  `save(force_insert, force_update, using, update_fields)`, so
  `obj.save(period_start=x)` gives `TypeError: Model.save() got an unexpected
  keyword argument 'period_start'`. Assign the attributes, then call `save()`
  with no arguments.
- **`Subscription.current_period_end` on the class is a descriptor, not a
  value** — `<django.db.models.query_utils.DeferredAttribute object at 0x...>`.
  Only an instance gives the datetime.
- **`datetime + 1` is a `TypeError`**, `unsupported operand type(s) for +:
  'datetime.datetime' and 'int'`. And `timedelta(days=30)` is not a month: Jul 1
  → Jul 31 → Aug 30 → Sep 29, sliding earlier every cycle, never raising.
- **The guard direction is easy to invert.** `current_period_end > now()` reads
  naturally but means "bill it if the period ends in the future" — exactly the
  case to refuse. Closed means the end is in the **past**: the guard raises on
  `>`, proceeds otherwise. Written backwards first; Acme (ends Aug 1, today Aug 8)
  was skipped while `test name` (ends Sep 1) was billed.
- **`filter()` where `get()` was meant, again.** `sub = ...filter(...)` then
  `sub.current_period_end` gives `AttributeError: 'QuerySet' object has no
  attribute 'current_period_end'`. Sixth appearance of this shape.
- **An unbound local in the skipped branch.** Assigning `invoice` only inside
  `if <closed>:` and then serializing it unconditionally gives `UnboundLocalError:
  cannot access local variable 'invoice' where it is not associated with a
  value`. Every path out of a view has to produce a `Response`; the refusal case
  needs its own raise, not a fall-through. A guard clause that raises early is
  flatter than `if/else`.
- **`Response(invoice)` passes a model object.** The JSON renderer cannot
  serialize it — `Object of type Invoice is not JSON serializable`. Needs
  `.data` off the serializer.
- **Plain `Exception` subclasses are invisible to DRF.** `services.BillingError`
  and its subclasses are not `APIException` and not `Http404`, so an uncaught one
  is a **500**, not a 409. Same for `Subscription.DoesNotExist`.
- **The two `NoActiveSubscription` classes shadow each other.** One in
  `views.py` (an `APIException`), one in `services.py` (a `BillingError`). Inside
  `views.py` a bare `except NoActiveSubscription:` binds the local one, which the
  service never raises — so the clause never fires, the real exception escapes,
  and the result is a 500 with no hint at the cause. The `services.` prefix is
  mandatory in that except clause.
- **A stray bare token at module level is an import-time `NameError`.** A loose
  `end` between two classes gave `File "/app/billing/views.py", line 51, in
  <module> / NameError: name 'end' is not defined`, which takes down
  `manage.py check`, the URLconf, and the container. Nothing else is verifiable
  until it is gone.
- **`manage.py check` does not execute function bodies.** It reported "no
  issues" while `post()` still contained `invoice.save(period_start=...)`,
  `Subscription.current_period_end`, and `period_start + 1` — three `TypeError`s
  waiting. A clean check means the module imports, not that the code works.
- **`docker compose logs -f web` dumps the whole history before following.** A
  201 was declared missing when it was sitting at line ~1800 of 1844. Use
  `docker compose logs web | grep "POST /billing/invoice"` or `--tail=20 -f`.

Still not started, in order:

1. **Move the closed-window guard into the service layer.** It lives in the view
   today, so the Phase 4 worker — which never runs a view — would call
   `generate_invoice` directly and bill open periods, reintroducing the exact bug
   the guard exists to stop. A service function holding *fetch subscription →
   check closed → generate → advance* gives the view and the worker one shared
   path, which is the whole point of decision 2. This is the only queued item
   that is a correctness bug rather than a cleanup.
2. **`CheckConstraint` on `Subscription`: `current_period_end >
   current_period_start`.** Subscription 11 (Initech) is `Aug 1 → Aug 1`. A
   zero-length window matches no events (`occurred_at >= X AND occurred_at < X`),
   so it bills `base_fee` only and writes real ledger rows for it. Since the
   advance landed it self-corrects to `Aug 1 → Sep 1` after one junk invoice, but
   the invoice and its ledger pair are still wrong data. Same layering rule as the
   fee constraints — the DB is the only layer the worker cannot bypass.
3. Remaining read endpoints: invoices, ledger. Both need the same `get_queryset`
   treatment; `Invoice` and `LedgerEntry` do have direct `tenant` FKs, so those
   filter without a join. `InvoiceSerializer` already exists and is already
   output-only, so the invoice one is mostly done.
4. **URL naming.** The generate route is `billing/invoice/`, one character from
   the `billing/invoices/` list route about to be added. Rename it to something
   that reads as an action — `billing/invoices/generate/` — before the collision
   exists.
5. Nothing checks `plan.is_active` before subscribing. Not reachable over HTTP yet
   (`is_active` is read-only in `PlanSerializer`), but admin can deactivate a plan
   and the subscribe call still returns 201. A
   `PrimaryKeyRelatedField(queryset=Plan.objects.filter(is_active=True))` turns it
   into a 400.
6. **No pagination on any list view.** `GET billing/usage/` returned all 12 rows
   in one response. Usage events are the highest-volume table in the schema by a
   wide margin. `DEFAULT_PAGINATION_CLASS` + `PAGE_SIZE` in `REST_FRAMEWORK`
   applies to every list view at once — cheap now, Phase 5 otherwise. Ordering
   (item 7) has to land first or alongside it.
7. **No `Meta.ordering` on `UsageEvent`.** The list came back `[4, 1, 2, 3, 5, …]`
   — Postgres returns rows in whatever order it likes without `ORDER BY`, and that
   order shifts as rows are updated. This becomes a correctness bug the moment
   pagination lands: unordered pagination silently drops and duplicates rows
   across pages, and Django warns about it.

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
   section above for what `generate_invoice` does. Built and committed as
   `7973e99`.
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
4. ~~**How create-tenant and create-plan authenticate.**~~ **DECIDED 2026-08-05:
   `permission_classes = [AllowAny]` on both.** Neither can authenticate as a
   tenant — create-tenant has no tenant yet and no key to present, and a plan
   belongs to no tenant. Phase 2's "Done when" requires the whole flow via API,
   so moving them to Django admin or a management command was not an option.

   Consequence: both endpoints are open. Record it in the Phase 6 decision note
   next to the plaintext-keys entry as a deliberate demo simplification.
   Create-tenant returns the generated `api_key` in its response — that is the
   only way a caller obtains credentials for every later request.

   Rejected for now: an `ADMIN_API_KEY` env var plus a permission class checking
   it. Correct, but a second auth scheme to build and test before a single
   endpoint works. Clean Phase 6 upgrade once the flow runs end to end. Nothing
   about this choice affects Phase 4's isolation test, which targets the
   tenant-scoped endpoints (usage, invoices, ledger).
5. ~~**Are zero-fee plans a legal product?**~~ **DECIDED 2026-08-07: yes, both
   shapes.** `base_fee 0` (usage-only) and `unit_fee 0` (flat-rate) are real
   billing products, so the constraints are `__gte=0`, not `__gt=0`. First cut
   used `__gt=0` and had to be migrated back — `0006` is that reversal.

   Consequence worth watching: a free plan with zero usage generates an invoice
   for `0.00` and a `0.00`/`0.00` ledger pair. It balances and breaks no
   invariant, but it records a movement of no money. Whether a zero invoice
   should write a ledger pair at all is a Phase 3 question, not a Phase 2 one.
6. ~~**Where the billing window comes from.**~~ **DECIDED 2026-08-08:
   per-subscription cycle, read off `Subscription.current_period_start` /
   `current_period_end`, advanced one month after each successful invoice.** The
   client never supplies a timestamp; the generate endpoint has no request body.

   The rule underneath all the options was the same: **never bill a period that
   has not ended.** Today is Aug 8; billing the window `[Aug 1, Sep 1)` now means
   every event from this moment to Aug 31 falls inside an already-invoiced window
   and can never be charged, because the second call trips
   `unique_invoice_period`. Those events also match no future window — September
   bills `[Sep 1, Oct 1)` — so they end up permanently `invoice_id NULL`, the
   same state as the legitimately-unbilled June event but for the opposite
   reason. Silent revenue loss, nothing raises.

   Rejected: **client sends both timestamps raw.** `unique_invoice_period`
   compares to the microsecond, so `T00:00:00Z` and `T00:00:00.001Z` are two
   distinct rows and both generate a July invoice. Worse than a duplicate:
   `frozen.update(invoice=invoice)` *moves* the events to the second invoice, so
   the first is left `OPEN` for `30.00` with zero events attached and AR reads
   `60.00` for `30.00` of real usage. Both ledger invariants still pass — same
   blind spot as the negative-fee bug.

   Rejected: **client sends both, server truncates to a day boundary.** Fixes the
   microsecond collision and nothing else. Overlapping windows are still legal
   (`[Jul 1, Aug 1)` and `[Jul 15, Aug 15)` are both midnight-aligned, both
   distinct, and double-bill everything in the overlap), and truncation has to
   pick a timezone — `2026-07-01T00:00:00+02:00` is `2026-06-30T22:00:00Z`, so
   two clients asking for "July" produce two different rows.

   Rejected: **calendar month, server-derived from the clock** (bill last complete
   month, same boundaries for every tenant). Genuinely simpler, and the Phase 4
   worker would just wake on the 1st and bill everyone. Lost on fitting the schema
   worse — `current_period_start` / `current_period_end` already exist on
   `Subscription` and would sit unused — and on having no way to bill a period
   other than the immediately-previous one.

   Cost accepted, and it is real: the two period fields are denormalized state
   that must stay consistent, which is what item 2 under "Still not started"
   exists to guard. Also, `generate_invoice(tenant, period_start, period_end)`
   now has arguments that must always equal the subscription's own values —
   passing anything else advances the cycle to the wrong place. Worth folding the
   window resolution into the service when the guard moves there.

   Why `current_period_end` is kept rather than derived from
   `current_period_start`: billing works on a range, so both values have to exist
   somewhere regardless (`Invoice` stores both, and the unique constraint spans
   both); "+1 month" has no single right answer for a Jan 31 start, so storing the
   end means a human decided once instead of every caller deciding the same way;
   and a non-monthly plan later needs no schema change.

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
  timestamps, so two boundaries a microsecond apart do NOT collide and a
  duplicate invoice slips through. **Largely defused by decision 6** — boundaries
  are now copied from `Subscription.current_period_*` and advanced by whole
  months, never derived from `timezone.now()` at call time, so repeat calls
  produce byte-identical values and the constraint fires. The exposure that
  remains is anything that writes those subscription fields with a
  non-midnight-aligned timestamp: `SubscriptionsSerializer` accepts
  `current_period_start` / `current_period_end` straight from the request body
  with no normalization, so a client can seed a cycle at `T13:47:22.813Z` and
  every window from then on carries that offset. Normalizing on write (or
  switching those fields to `DateField`) closes it.
- Nothing prevents attaching a `UsageEvent` to an `Invoice` belonging to a
  different tenant — `UsageEvent` reaches tenant only via `subscription.tenant`,
  and `invoice` is an independent FK. Needs a validation check in the invoice
  generator.
- ~~`Subscription.status` has no default~~ — fixed in `6c95a9f`,
  `default='ACTIVE'`.
- ~~No `__str__` on any model~~ — partly fixed in `13f695e`. `Tenant`, `Plan`,
  `Subscription` have one. `Invoice`, `UsageEvent`, `LedgerEntry`, and
  `IdempotencyKey` still print as `<Invoice: Invoice object (17)>`, and those are
  the four that matter most for reading ledger checks in the shell.
- ~~`billing/admin.py` registers nothing~~ — fixed in `c3086cb`, all seven
  registered.
- `Plan.clean()` guards with `if self.base_fee and self.unit_fee:`. `Decimal('0')`
  is falsy, so a free or flat-rate plan skips the whole block. Harmless right now
  (nothing in the block would reject zero) but it silently disables any check
  added there later. Guard on `is not None`.
- Consider migrating existing choice sets to `TextChoices`.

Validation layering (settled during the negative-money work, applies to every
model from here on):

- A DB `CheckConstraint` is the only layer that cannot be bypassed. Anything the
  Phase 4 worker, a shell, or admin can write has to be guarded there.
- DRF never calls `full_clean()`, so `Model.clean()` does **not** run on the API
  path. Model-field `validators=` do reach DRF (ModelSerializer copies them onto
  the generated field); `clean()` does not.
- Put the human-readable message in the serializer, put the guarantee in the
  constraint, and expect to write the rule twice. That duplication is deliberate.
- `CHECK` constraints ignore NULL — `NULL >= 0` is UNKNOWN, not false. Pair every
  check with `NOT NULL` if NULL is not a legal value.

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
Tracked file count is **29** as of `ff871e1`, unchanged from `73af90a` (was 26 at
`7973e99`; `serializers.py` and migrations `0005`/`0006` account for the three) —
if it jumps, something got committed that shouldn't have been. `ff871e1` touched
five existing files and added none.

`requirements.txt` gained `python-dateutil==2.9.0.post0` and its `six==1.17.0`
dependency in `ff871e1`, for `relativedelta` in the period advance. A
requirements change needs `docker compose up -d --build` — a restart does
nothing — and the only check that counts is importing it **inside** the
container.

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
