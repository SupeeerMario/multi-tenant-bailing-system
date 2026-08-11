# Phase 1 & Phase 2 — build journal

Moved out of the root `CLAUDE.md` on 2026-08-11 so it stops loading into every
session. The "Traps hit" lists were removed at the same time — the ones that
kept repeating were promoted into `CLAUDE.md` under "Traps that repeat".

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

**Drifted and cleaned again 2026-08-08.** A verification call ran outside its
rollback block and billed Initech (15) on the zero-length `Aug 1 → Aug 1` window,
writing invoice `41` (`20.00`) and ledger rows `33`/`34`. Deleted in that order —
ledger pair first, then the invoice, because `Invoice` is `PROTECT`ed by
`LedgerEntry`. A later round of HTTP endpoint probing added tenants `35`–`37` and
subscriptions `70`/`72`; those were deleted too (subscriptions before tenants).
Baseline reconfirmed at **4 tenants / 1 plan / 5 subscriptions / 6 usage events /
4 invoices / 8 ledger rows**, ledger sum `0.00`.

Subscription `11` was **deliberately left at `Aug 1 → Sep 1`** rather than reset
to `Aug 1 → Aug 1` — the advance self-corrected it, and restoring the original
value would recreate a row that `prevent_zero_length_period` now forbids.
Initech therefore no longer serves as the `NoActiveSubscription` fixture it was
originally created to be; a Phase 5 factory needs to provide that case
explicitly.

Third time ad-hoc rows have drifted from what this file claims. The lesson that
keeps repeating: a verification call placed *after* the `with
transaction.atomic():` body instead of inside it writes real rows.

**No drift on 2026-08-09.** The Phase 3 payment work needed real writes (a
payment cannot be verified inside a rolled-back block if you want to curl it), so
it ran on two throwaway tenants — `38 PayTest` with invoices `42`/`43`, and
`39 RaceTest` with two more for the concurrency test. Both fully deleted
afterwards, ledger rows first, then invoices, then the tenant, because
`LedgerEntry` `PROTECT`s `Invoice`. Baseline reconfirmed at
**4 / 1 / 5 / 6 / 4 / 8**, ledger sum `0.00`. Deliberately did **not** pay Acme's
invoice 17 — that would have permanently changed the documented baseline.

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

**That moment arrived 2026-08-08** with `prevent_zero_length_period`, and the
handler answered 409 "already has an active subscription" to a tenant that had
none. Deriving `current_period_end` server-side closed the HTTP path, so the
handler is not currently wrong for any reachable request — but it is still
written to be wrong for the next constraint. See "Zero-length periods closed"
and item 1 under "Still not started".

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


#### Window resolution moved into the service (2026-08-08)

Closes the old queued item 1. `generate_invoice` now takes **only a tenant** and
resolves everything else itself:

```python
def generate_invoice(tenant):
```

Order inside the function: resolve the ACTIVE subscription (`get()`, raises
`NoActiveSubscription`) → bind `period_start` / `period_end` off
`current_period_start` / `current_period_end` → guard
`if period_end > timezone.now(): raise PeriodNotEnded(...)` → everything else
unchanged. New `PeriodNotEnded(BillingError)` sits alongside the other two
service exceptions. `django.utils.timezone` is now imported in `services.py` —
that is not an HTTP concept, so it does not violate decision 2; `Response` and
`Http404` are still the things that must never cross.

`InvoiceAPIView.post()` now calls `services.generate_invoice(request.tenant)`.
Its own subscription fetch and its own guard were **kept on purpose** — one extra
query buys the friendlier 409 message, which is the same layering rule as the fee
constraints (message in the outer layer, guarantee in the inner one).

Why the parameters had to go rather than just gaining a guard: the view was never
the only caller. A guard on `period_end` vs the clock stops an *open* window, but
nothing stopped a caller passing a *closed* window that was not the
subscription's own — and the advance at the end of the function trusts whatever
it was handed. Two failure shapes, both silent:

- **A stale window drags the cycle backwards.** `generate_invoice(acme, Jan 1,
  Feb 1)` on a subscription at `Jul 1 → Aug 1` passes the guard, trips no unique
  constraint, writes a `20.00` base-fee invoice with a real ledger pair, then
  advances to `Feb 1 → Mar 1`. Every later call bills Feb, Mar, Apr… until it
  crawls back to July and wedges on `InvoiceAlreadyExists` forever.
- **An overlapping window silently double-counts.** `generate_invoice(acme, Jul
  15, Aug 1)` is a distinct row under `unique_invoice_period` (`Jul 15 ≠ Jul 1`),
  so no collision. `frozen.update(invoice=invoice)` then **moves** the events off
  invoice 17 onto the new one, leaving 17 `OPEN` for `30.00` with zero events
  attached and AR double-counting the same usage. Both ledger invariants still
  pass — same blind spot as the negative-fee bug.

Demonstrated live with the ten lines a Phase 4 worker would plausibly contain
(`bill last complete calendar month`, the option decision 6 rejected), against a
subscription one cycle behind at `Jun 1 → Jul 1`:

```
sub owes    : 2026-06-01 -> 2026-07-01
worker bills: 2026-07-01 -> 2026-08-01
invoice     : 2026-07-01 -> 2026-08-01 amount 22.50
sub now     : 2026-08-01 -> 2026-09-01
june event invoice_id: None
```

June is now behind the cursor and the advance only moves forward, so `30.00` of
real usage can never bill. No exception raised; a worker logs it as a success.
The June event sits at `invoice_id NULL`, byte-identical to the legitimately
unbilled event 4 — the two states are indistinguishable by inspection.

**Verified live 2026-08-08** on a throwaway tenant seeded at `Jun 1 → Jul 1` with
usage in June and July, all inside a rolled-back transaction:

| Call | Result | Window | Amount | Subscription after |
|---|---|---|---|---|
| 1 | invoice | Jun 1 → Jul 1 | `30.00` | Jul 1 → Aug 1 |
| 2 | invoice | Jul 1 → Aug 1 | `22.50` | Aug 1 → Sep 1 |
| 3 | `PeriodNotEnded` | — | — | unchanged |

Each event landed on the correct invoice; windows tile with no gap. Acme →
`InvoiceAlreadyExists` carrying its own period. The behind-by-a-cycle case that
the old signature silently skipped now bills June first and stops on its own.


#### Zero-length periods closed (2026-08-08, migration `0007`)

Two layers landed together, and they do different jobs — same split as the
negative-money work.

**The guarantee: `prevent_zero_length_period` on `Subscription`.** Second entry
in the existing `Meta.constraints` list, alongside
`unique_active_subscription_per_tenant`:

```python
models.CheckConstraint(
    condition = Q(current_period_end__gt = F('current_period_start')),
    name = 'prevent_zero_length_period'
)
```

`F` added to the `django.db.models` import in `models.py`. Migration
`0007_subscription_prevent_zero_length_period` holds a single `AddConstraint`.
Postgres renders it as
`CHECK (current_period_end > current_period_start)`. No existing row violated it
(checked with `filter(current_period_end__lte=F('current_period_start'))` first —
`0` rows), so `AddConstraint` applied cleanly.

Verified live with raw `objects.create()`, bypassing every serializer, all rolled
back: `Aug 1 → Aug 1` and `Aug 1 → Jan 1` both `IntegrityError`, `Aug 1 → Sep 1`
created. The backwards case is a bonus — `__gt` also blocks the
`relativedelta(month=1)` cascade documented above, which would now fail at the
database instead of silently wedging the billing cycle.

**The message: the end is no longer client-supplied at all.**
`current_period_end` is `read_only` in `SubscriptionsSerializer`, and
`SubscriptionsCreateView.perform_create` derives it:

```python
period_start = serializer.validated_data['current_period_start']
period_end = period_start + relativedelta(months = 1)
serializer.save(tenant = tenant, current_period_end = period_end)
```

Same move as dropping `generate_invoice`'s window parameters: an ordering error
becomes unrepresentable rather than merely rejected. It also stops the rule
living in two places — `services.py` already advances by
`relativedelta(months=1)` on every cycle, so a client-supplied end would have
made cycle one a different length from every cycle after it.

A serializer-level `validate()` comparing the two fields was written and then
**deleted** — with the end derived, it had no job left, and a `read_only` field
is stripped from `attrs`, so it could only ever `KeyError`. The DB constraint
still covers admin, the shell, and the Phase 4 worker.

`months=1` is hardcoded. `Plan.interval` (currently only `'MONTHLY'`) is the
natural source for the step when a non-monthly plan appears.

**Verified live over HTTP 2026-08-08**, throwaway tenants, all deleted after:

| Case | Result |
|---|---|
| `POST subscriptions/` with `current_period_start` only | 201, `current_period_end` derived one month later |
| second ACTIVE sub, same tenant | 409, correct duplicate message |
| `current_period_end` spoofed in the body | ignored — `Jan 31` → `Feb 28`, not the value sent |
| `current_period_start` omitted | 400 `{"current_period_start":["This field is required."]}` |

`Jan 31 → Feb 28` confirms `relativedelta` clamps short months rather than
overflowing into March.


#### Ordering, and why it is not on the model (2026-08-08, `be5aff4`)

`UsageEventListCreateView.get_queryset` now ends `.order_by('occurred_at', 'id')`.
Rows come back chronological — `4 (Jun 20), 1 (Jul 2), 2 (Jul 15), 3 (Jul 31),
5 (Aug 1)`. Under the previous `.order_by('id')` the June event sat mid-list.

Two things had to be true, and they are separate:

- **An `ORDER BY` at all.** Without one Postgres returns rows however it likes and
  the order shifts as rows are updated. Under `LIMIT/OFFSET` pagination that means
  page 2 can repeat a row from page 1 and drop another. Django warns
  (`UnorderedObjectListWarning`) but does not stop it.
- **A unique tiebreaker.** `occurred_at` alone is not enough — it is not unique,
  and tied rows have no defined order among themselves, so pagination breaks again
  on exactly the pages where a tie falls. `id` is the tiebreaker because it is
  unique and already indexed.

`occurred_at` over `id` as the primary key of the sort: `id` is insertion order,
not event time, so a backdated event recorded today gets a high id and sorts last
despite happening first.

**`Meta.ordering` on the model was rejected**, with the cost understood. Model
ordering would cover admin, the shell, the Phase 4 worker, and every future view
in one line and one no-DDL migration. The view-level call covers one queryset.
The asymmetry that makes this a real debt: item 5 turns pagination on with a
**global** setting (`DEFAULT_PAGINATION_CLASS`), applying to every list view
including ones written later, while its precondition is now **local and opt-in**.
So:

- Items 2's invoice and ledger views each need their own `order_by`.
  `Invoice` → `('-period_start', 'id')`; `period_start` looks unique per tenant
  under `unique_invoice_period` but that constraint spans
  `(tenant, period_start, period_end)`, so two rows sharing a start with different
  ends are legal and the tiebreaker is still required. `LedgerEntry` → `('id',)`
  alone; the table is append-only by convention, so `id` is insertion sequence, is
  chronological, and is unique. `created_at` is worse there — it is `auto_now_add`
  evaluated once per row, so the two rows of a pair carry different timestamps.
- **Item 5 must not land before those three `order_by` calls exist.**

Unrelated to ordering but caused by pagination: a ledger `transaction_id` pair can
straddle a page boundary, so a single page's rows will not sum to zero. Phase 5's
ledger test has to read the database or every page, never one response.

A trap worth not re-learning: `ordering = [...]` was first written in
`UsageEventSerializer.Meta`. **Serializer `Meta` is not model `Meta`.** DRF reads a
fixed set of keys off it (`model`, `fields`, `exclude`, `read_only_fields`,
`extra_kwargs`, `depth`, `validators`, `list_serializer_class`); anything else is
set as an attribute nobody reads. No error, no warning, no `ORDER BY` in the SQL —
and `UsageEventSerializer.Meta.ordering` still prints the list, so the attribute
really is there. A serializer has no queryset; it receives already-fetched objects
and cannot influence SQL.

#### `__str__` on every model (2026-08-08, `be5aff4`)

All seven models print something readable now. `13f695e` had done `Tenant`,
`Plan`, `Subscription`; this adds `Invoice`, `UsageEvent`, `LedgerEntry`,
`IdempotencyKey`, and puts the row id into the first three.

**No migration.** `__str__` is a method, not field state — Django does not track
it. `makemigrations --check` reports `No changes detected`. Same as the
`is_authenticated` property on `Tenant`.

Three rules the final versions follow:

- **`_id`, never the related object.** `f"{self.tenant}"` loads the `Tenant` row —
  once per instance. Measured live: printing four invoices took **5 queries**
  (one list + one per tenant); switching to `self.tenant_id` took **1**. An admin
  page of 100 ledger rows would have been 101. The column is already in memory, so
  `tenant_id` is free. It also matters for nullable FKs: `self.invoice.id` is
  `AttributeError` on every unbilled usage event, `self.invoice_id` is `None`.
- **Own id first.** The id is what every shell check, `PROTECT` error and
  `invoice_id` stamp refers to — "delete ledger 33/34, then invoice 41" is
  unusable if the lines do not carry ids.
- **Truncate opaque and client-supplied fields.** `transaction_id` is 36 chars and
  was the widest thing on a 185-char ledger line; `str(self.transaction_id)[:8]`
  brought it to 157, which fits one terminal row. The value is never read, only
  *matched* between two rows, and 8 hex digits does that. `IdempotencyKey.key` is
  `max_length=512` and **client-supplied** (it arrives in the `Idempotency-Key`
  header, so the client picks the length) — `[:12]`. `response_body` (a whole
  JSONField payload) and `request_hash` (64 chars) were dropped from the output
  entirely. That table is empty today and fills in Phase 3, which is exactly when
  these rows get stared at.

The check that these work: print all 8 ledger rows and see whether the four pairs
are spottable at a glance. They are — same 8-char `transaction_id`, `+30.00` and
`-30.00`, adjacent.

Timestamps are printed **in full**, not `.date()`. Deliberate: a non-midnight
boundary is a live symptom, since `SubscriptionsSerializer` still accepts
`current_period_start` unnormalized and a cycle seeded at `T13:47:22.813Z` carries
that offset into every window forever (see the uniqueness item under Schema and
code cleanups). `.date()` would hide it.


Still not started, in order:

1. **Narrow the bare `except IntegrityError` in
   `SubscriptionsCreateView.perform_create`.** It reports every integrity error
   as a duplicate subscription. Looked at 2026-08-08 and deferred, but the
   discriminator was confirmed on this stack (psycopg 3.3.4):
   `e.__cause__.diag.constraint_name` returns
   `'unique_active_subscription_per_tenant'` for the duplicate and
   `'prevent_zero_length_period'` for the check violation, and
   `type(e.__cause__)` is `UniqueViolation` vs `CheckViolation`. Match on the
   **name**, not the type — a second unique constraint would collide on type.
   Guard the attribute access (`getattr(e.__cause__, 'diag', None)`; `__cause__`
   can be `None`) and let anything unmatched re-raise as a 500 — an unanticipated
   integrity error is a real server bug, and crashing beats answering 409 with a
   confident wrong explanation. Note the cost: `e.__cause__.diag` is
   psycopg-specific, so this ties `views.py` to the driver. Acceptable in a
   Postgres-only project, but comment it. The constraint name also becomes a
   string duplicated between `models.py` and `views.py` — pull it into a
   module-level constant both reference.
2. Remaining read endpoints: invoices, ledger. Both need the same `get_queryset`
   treatment; `Invoice` and `LedgerEntry` do have direct `tenant` FKs, so those
   filter without a join. `InvoiceSerializer` already exists and is already
   output-only, so the invoice one is mostly done.
3. ~~**URL naming.**~~ **Done 2026-08-09.** `billing/invoice/` is now
   `billing/invoices/generate/` (name `generate_invoices`), renamed as part of
   adding `billing/invoices/<int:pk>/pay/` rather than after the collision
   existed.
4. Nothing checks `plan.is_active` before subscribing. Not reachable over HTTP yet
   (`is_active` is read-only in `PlanSerializer`), but admin can deactivate a plan
   and the subscribe call still returns 201. A
   `PrimaryKeyRelatedField(queryset=Plan.objects.filter(is_active=True))` turns it
   into a 400.
5. **No pagination on any list view.** `GET billing/usage/` returned all 12 rows
   in one response. Usage events are the highest-volume table in the schema by a
   wide margin. `DEFAULT_PAGINATION_CLASS` + `PAGE_SIZE` in `REST_FRAMEWORK`
   applies to every list view at once — cheap now, Phase 5 otherwise. **Do not
   land this before every list view has its own `order_by`** — see item 6 for why
   that ordering is per-view rather than per-model here.
6. ~~**No `Meta.ordering` on `UsageEvent`.**~~ **Handled at the view, 2026-08-08.**
   `UsageEventListCreateView.get_queryset` ends `.order_by('occurred_at', 'id')`.
   `Meta.ordering` on the model was considered and **deliberately rejected** — see
   the "Ordering" subsection in Phase 2 for the tradeoff and for the standing debt
   that choice creates for items 2 and 5.
