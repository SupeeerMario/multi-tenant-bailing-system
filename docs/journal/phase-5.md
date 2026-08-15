# Phase 5 — tests + docs + CI

Covers the two remaining required tests (double-pay, ledger), the production
bug the double-pay test found, the fixture mixin, and the Swagger layer with
its live contract verification. **CI is the only Phase 5 item still open**, and
the lint ruleset for it is decided below.

Everything here was verified against Postgres inside the Docker stack. Nothing
was taken as working because it looked right.

## The double-pay test

`IdempotentPaymentTests` in `billing/tests.py`, two methods:
`test_payment_succeeds` and `test_replay_returns_identical_response`.

### Why a separate class from `UsageIsolationTests`

The question came up as "why not just add a method to the isolation class",
and the answer is not style. **One `TestCase` class is one fixture story**, and
these two stories contradict each other:

- Isolation needs a **forward** window — `now` to `now + 1 month` — because it
  records usage events and reads them back. It never calls `generate_invoice`.
- Payment needs a **backward** window, because a payment needs an invoice, and
  an invoice only exists for a period that has already ended.

Point the isolation fixtures at `generate_invoice` and `services.py:55` raises
`PeriodNotEnded` on line one. There is no single `setUp` that serves both.

Two secondary reasons: the payment story is four-plus methods sharing one
invoice, which is exactly what a class-level `setUp` is for; and the class name
is what the runner prints, so `UsageIsolationTests.test_double_pay ... FAIL`
sends the reader to the wrong file.

### `setUp` is per-method, not per-class

Worth stating plainly because it drove every fixture decision. Per `test_*`
method the runner does:

```
1. open a transaction
2. build a NEW instance of the test class
3. call instance.setUp()
4. call instance.test_whatever()
5. ROLL BACK
```

Consequences that actually bit:

- **Anything a test method reads must be on `self`.** `setUp` locals die at
  return. `self.tenant`, `self.invoice`.
- **Each method gets its own rows with its own ids.** Nothing survives between
  methods, so both payment methods can reuse the key string `'test-key-1'`.
- **A "second" POST cannot live in a second method.** It would be a *first*
  POST against a fresh invoice. Both POSTs of the replay test are in one
  method, necessarily.
- **With zero `test_*` methods, `setUp` never runs at all.** An early note in
  this session said `Ran 0 tests / OK` would confirm the fixtures built. It
  confirms nothing — no method, no `setUp` call.

### The fixture, and the three things that otherwise raise

Built with the ORM, then one service call:

```
tenant, plan(base_fee 20, unit_fee 1, currency USD)
subscription  current_period_start = now - 2 months
              current_period_end   = now - 1 month
invoice = services.generate_invoice(tenant)
```

- `generate_invoice(tenant)` takes **only** the tenant (`services.py:38`) and
  reads the window off the subscription.
- The window must be fully past or `PeriodNotEnded` fires (`services.py:55`).
  A full month back rather than hugging `now` is for margin and readability;
  `now - 1 month` to `now` would technically pass by microseconds.
- No usage events, so `total_quantity` is `0` and `amount == base_fee ==
  20.00`. Predictable, and clear of `Decimal('66.66')`, the mock gateway's
  decline amount.
- `is_active` defaults `True` on both `Tenant` (`models.py:17`) and `Plan`
  (`models.py:38`), so the `CANCELED` branch at `services.py:79` is not taken.

**First failure hit, worth keeping:** the signs were copied forward instead of
flipped, giving `now + 2 months` to `now + 1 month`. Start *after* end, and
`prevent_zero_length_period` caught it at insert:

```
DETAIL:  Failing row contains (1, ACTIVE, 2026-10-15 ..., 2026-09-15 ..., ...)
```

Reading the two dates out of `DETAIL` is what named the bug. The constraint
earned its keep here — without it the row inserts and the failure surfaces
later as something less obvious.

### What the two methods assert, and the mutation proof for each

`test_payment_succeeds` — one POST:

| assertion | proven red by |
|---|---|
| `status_code == 200` | live, `404 != 200`, from a URL missing its leading slash |
| invoice `PAID`, re-fetched | commenting out `services.py:212` → `'OPEN' != 'PAID'` |
| `LedgerEntry` delta exactly 2 | dropping the `CASH` create at `services.py:226` → `3 != 4` |

`test_replay_returns_identical_response` — two POSTs, same key, same body:

| assertion | proven red by |
|---|---|
| both `200` | — |
| `response.content` byte-identical | returning a different body from the `COMPLETED` arm at `services.py:178` → `b'{"id":1,...}' != b'{"detail":"replayed"}'` |
| delta still 2 | — (shares the mutation above) |
| exactly one `IdempotencyKey` row | **not proven, deliberately** |

The last row is honest bookkeeping. Any mutation that writes a second key row
also changes the status code, so `assertEqual(status_code, 200)` fires first
and the key-count assertion is never reached. It stays a cheap consistency
check, not evidence.

**The content comparison is the assertion that earns its keep.** CLAUDE.md
records a real bug where a path stored `{'detils': ...}` and returned
`{'detail': ...}` — both responses look correct in isolation, and only a test
comparing the two **to each other** catches it. The mutation reproduced exactly
that shape: both statuses stayed `200` while the bodies diverged.

**Use `.content`, not `.data`.** `.data` is the parsed dict; dict equality
passes even when the two responses serialize differently. `.content` is bytes,
which is what "byte-identical" in the Phase 3 deliverable actually means.

### Deliberate rule-break in the fixtures

The isolation test's rule is fixtures-via-ORM-only, so a break elsewhere cannot
send it red. This test breaks it by calling `services.generate_invoice(tenant)`.

Narrow reason: the thing under test is the **delta a payment causes**.
Hand-writing the `ACCOUNTS_RECEIVABLE`/`REVENUE` pair means writing by hand the
exact rows the test then asserts about — the test would agree with itself.

Cost, accepted and real: these tests also go red if invoice *generation*
breaks.

## The bug the double-pay test found

The first run of the replay test did not fail an assertion. It errored:

```
django.db.transaction.TransactionManagementError: An error occurred in the
current transaction. You can't execute queries until the end of the 'atomic'
block.
```

`services.py:166-169` was:

```python
try:
    idem_key_object = IdempotencyKey.objects.create(...)
except IntegrityError:
    exist = IdempotencyKey.objects.get(...)      # <- this query
```

The second POST hits `unique_key_per_tenant`, as designed. **Postgres then
marks the whole transaction aborted**, and every later statement in it is
refused. The `get()` is a later statement.

In production this survived on autocommit — each statement is its own
transaction, so the failed INSERT aborts only itself. `django.test.TestCase`
wraps every test method in one `atomic()` block, so the test was the first
caller that was not in autocommit. CLAUDE.md had predicted this exactly and
said "`ATOMIC_REQUESTS` is unset so this is currently safe". The test was the
revisit that note asked for.

### Why savepoints fix it

Postgres accepts exactly two statements inside an aborted transaction:
`ROLLBACK`, and `ROLLBACK TO SAVEPOINT`. The second rewinds to a mark and
leaves the transaction **usable** instead of dead.

Demonstrated live in `psql`, temp tables, both rolled back. Without a savepoint:

```
BEGIN
CREATE TABLE
INSERT 0 1
ERROR:  duplicate key value violates unique constraint "t_pkey"
ERROR:  current transaction is aborted, commands ignored until end of transaction block
```

With one:

```
BEGIN
CREATE TABLE
INSERT 0 1
SAVEPOINT
ERROR:  duplicate key value violates unique constraint "t_pkey"
ROLLBACK                                   <- ROLLBACK TO SAVEPOINT s1
 this is the get() in your except block
(1 row)
```

Identical duplicate-key error both times. The only difference is one command
issued between the error and the SELECT.

The mapping to Python:

| SQL | emitted by |
|---|---|
| `SAVEPOINT s1` | entering `with transaction.atomic():` |
| `ROLLBACK TO SAVEPOINT s1` | the exception **leaving** that `with` |

The fix, `services.py:166-170`:

```python
try:
    with transaction.atomic():
        idem_key_object = IdempotencyKey.objects.create(...)
except IntegrityError:
    exist = IdempotencyKey.objects.get(...)
```

**The `except` must stay outside the `with`.** The `ROLLBACK TO SAVEPOINT` is
emitted by `atomic.__exit__`, which only runs as the exception leaves the
block. Catch it inside and `__exit__` never fires, nothing rewinds, and the
behaviour is identical to having no savepoint at all. This is the mechanism
under the existing "the `except IntegrityError` goes outside the `with`" trap
in CLAUDE.md.

### Why this beat switching to `APITransactionTestCase`

`APITransactionTestCase` runs without a wrapping transaction and truncates
tables afterwards, which would have made the test pass by matching production
autocommit exactly. It was rejected: it makes the *test* pass and leaves the
code fragile. The savepoint version is correct under autocommit **and** under
any atomic caller — a future `ATOMIC_REQUESTS`, a management command, the
Phase 7 work. In autocommit the same block is the outermost one and degrades to
a real `BEGIN`/`ROLLBACK`.

## The ledger test

`LedgerInvariantTests.test_ledger_invariants_hold_before_and_after_payment`.

The framing that made it click: the test compares **two independent records of
the same fact**.

- `outstanding` — what the **invoices** say customers owe.
- `ar_total` — what the **ledger** says customers owe.

Different tables, written by different lines of code. They must agree. When
they drift, one is lying.

Real values, `base_fee 20`, no usage:

**After `generate_invoice`, unpaid:**

| account | amount |
|---|---|
| ACCOUNTS_RECEIVABLE | +20.00 |
| REVENUE | −20.00 |

```
ledger_total = 0.00      every movement has both halves
ar_total     = 20.00     ledger says: owed 20
outstanding  = 20.00     invoices says: owed 20
```

**After the payment POST**, two rows appended (`CASH +20.00`,
`ACCOUNTS_RECEIVABLE −20.00`), invoice `PAID`:

```
ledger_total = 0.00
ar_total     = 0.00      owed nothing
outstanding  = 0.00      no OPEN invoices
```

`CASH +20.00` remains — the money is held.

### Why both assertions, proven

Mutation: duplicate the invoicing pair at `services.py:77-78` so four rows are
written instead of two.

```
line 130  ledger_total == 0        PASSED    (+20 -20 +20 -20)
line 131  ar_total == outstanding  FAILED    Decimal('40.00') != Decimal('20.00')
```

Sum-to-zero waved through a ledger claiming customers owe `40.00` against a
single `20.00` bill. It only ever checks that pairs are **well-formed**, never
that they **should exist** — which is the blind spot CLAUDE.md records four
real bugs using.

### Traps in the assertions themselves

- **`sum` is not `Sum`.** `aggregate(sum('amount'))` calls Python's builtin,
  which iterates the string: `TypeError: unsupported operand type(s) for +:
  'int' and 'str'`. Needs `from django.db.models import Sum`.
- **`aggregate()` returns a dict.** Two of the three calls were missing
  `['amount__sum']`, so the assertion compared two *dicts*. It passed —
  both dicts happened to carry the same key and value — for entirely the wrong
  reason.
- **Computed before, asserted after.** The first version computed all three
  numbers before the POST and asserted after it. They are plain values, nothing
  re-reads them, so the assertions described the pre-payment state. Deleting
  the POST entirely would have left the test green. **Recompute after the
  write.**
- **`aggregate` returns `None`, not `0`, on an empty match.** After payment
  there are no `OPEN` invoices, so `outstanding` comes back `None` and
  `None == Decimal('0')` is False. Use `Coalesce(Sum(...), Decimal('0'))` or an
  explicit `is None` test. **Not `or Decimal('0')`** — `Decimal('0.00')` is
  falsy, the same mechanism as the `Plan.clean()` bug in CLAUDE.md's Known open
  items.

## `BillingFixtureMixin`

Both payment classes want the same tenant/plan/backwards-window/`generate_invoice`
fixture, so it moved to a plain mixin — **not** an `APITestCase` subclass,
because the runner collects every `TestCase` it finds.

```python
class BillingFixtureMixin:
    def setUp(self):
        super().setUp()
        ...

class IdempotentPaymentTests(BillingFixtureMixin, APITestCase):
class LedgerInvariantTests(BillingFixtureMixin, APITestCase):
```

**Order is load-bearing — mixin first.** Written `(APITestCase,
BillingFixtureMixin)`, Python finds `unittest.TestCase`'s do-nothing `setUp`
first, the mixin never runs, and every test dies on `AttributeError: ...
object has no attribute 'tenant'` with nothing saying the mixin was skipped.

**Trap hit live:** the refactor half-landed. `LedgerInvariantTests` listed the
mixin *and* kept its own `setUp`, which had no `super().setUp()` call. Defining
`setUp` in a subclass **replaces** the parent's unless `super()` is called, so
the mixin was inert for that class and the duplication being removed was still
there. The suite stayed green the whole time, because the override happened to
build the same fixtures. A green refactor is not a completed refactor.

`UsageIsolationTests` deliberately does **not** use the mixin — two tenants, a
forward window, a different story.

## Swagger

`drf-spectacular`, served at `/api/schema/` and `/api/docs/`.

### Wiring, and what fought back

1. `requirements.txt`: `drf-spectacular`, later `drf-spectacular-sidecar`.
   New dependency means `docker compose up -d --build`; a restart installs
   nothing.
2. `INSTALLED_APPS`: `'drf_spectacular'`, `'drf_spectacular_sidecar'`.
3. `REST_FRAMEWORK['DEFAULT_SCHEMA_CLASS'] =
   'drf_spectacular.openapi.AutoSchema'`.
4. `SPECTACULAR_SETTINGS`, with `'SERVE_PERMISSIONS':
   ['rest_framework.permissions.AllowAny']`.
5. `config/urls.py`: `SpectacularAPIView` at `api/schema/` with `name='schema'`,
   `SpectacularSwaggerView` at `api/docs/` with `url_name='schema'`.
6. Verify with `manage.py spectacular --file /dev/null` — generates and prints
   every warning without a browser.

**`SERVE_PERMISSIONS` is not optional here.** `DEFAULT_PERMISSION_CLASSES` is
`IsAuthenticated` globally (`settings.py:137`), which the docs views inherit,
so without it the documentation itself 401s.

**Hyphen vs underscore, hit twice.** The pip package is `drf-spectacular-sidecar`;
the importable module is `drf_spectacular_sidecar`. The hyphenated form in
`INSTALLED_APPS` is `ModuleNotFoundError: No module named
'drf-spectacular-sidecar'` at startup, and the container exits — `docker compose
ps` shows nothing, and `up -d`, not `restart`, is what brings it back.

**The management command works without step 5.** It introspects `urlpatterns`
directly, so the schema generated cleanly while both HTTP endpoints 404'd. Two
separate things to verify.

### The auth extension

Without it, seven `could not resolve authenticator` warnings, empty
`securitySchemes`, and no Authorize button — meaning nothing on the page is
callable. `TenantAuthenticationScheme(OpenApiAuthenticationExtension)` lives at
the bottom of `billing/authenticate.py`.

```python
target_class = 'billing.authenticate.TenantAuthentication'
name = 'ApiKeyAuth'
# -> {'type': 'apiKey', 'in': 'header', 'name': 'Authorization', 'description': ...}
```

- **`apiKey`, not `http`/`bearer`.** Bearer makes Swagger UI send
  `Bearer <key>`; `authenticate.py:14` compares the first token against
  `api-key`, so every request 401s while the docs look correctly configured.
- Because it is `apiKey` on `Authorization`, the UI sends **verbatim** what the
  user types. They must type `Api-Key <key>`, prefix included — which is why
  the `description` says exactly that.
- **It must live in a module that is already imported.** Extensions register as
  an import side effect; `authenticate.py` is imported because `settings.py:132`
  names it. A new module nobody imports registers nothing, silently.

Warnings went 7 → 0.

### `@extend_schema` on `InvoicesPay.post`

Declares the `Idempotency-Key` header, the request body, and the responses.

- **`location = OpenApiParameter.HEADER`.** The default is `QUERY`, which
  renders a query-string field the view never reads — the form looks filled in
  and the request 400s for a missing header.
- **`request = serializers.PaymentSerializer`** fixed a second bug quietly: the
  inferred body was `Invoice`, from `serializer_class` on the view, which is not
  what the endpoint accepts. The schema now says `Payment`.

## Verifying the documented contract against reality

The response codes were not trusted from the source. Every one was exercised
over real HTTP with `APIClient`, inside a `transaction.atomic()` block that
raises at the end, so the dev database is untouched.

| case | actual |
|---|---|
| valid payment | 200 |
| replay, same key + body | 200, byte-identical |
| gateway decline (`66.66`) | 402 |
| decline replay | 402, byte-identical |
| amount mismatch | 400 |
| missing `Idempotency-Key` | 400 |
| same key, different body | **422** |
| already-paid invoice, new key | 409 |
| unknown or other-tenant invoice | 404 |
| no `Authorization` header | 401 |

This corrected the docs twice:

- **`RequestHashDiffers` is 422, not 409** (`exceptions.py:59`). The first draft
  of the `responses` dict described the different-body case under 409. 409 is
  actually "invoice is not OPEN" or "payment already processing".
- **401, 404 and 422 were undocumented entirely.**

**Reaching 422 needs an extra field in the body.** With `amount` as the only
real field, a different amount trips `AmountMismatch` (400) at `services.py:162`
*before* the key is claimed at `:166`. Sending `{'amount': '50.00', 'extra':
'x'}` changes `request_hash` while the amount still matches, which is the only
way through.

**Two `ALLOWED_HOSTS` failures surfaced doing this**, both real and both already
on the Phase 6 list: `ALLOWED_HOSTS` is empty, so the schema is unreachable by
container hostname (`400`), and `APIClient` outside the test runner is rejected
with `DisallowedHost: Invalid HTTP_HOST header: 'testserver'`. Overridden
in-process for the verification run only.

### Sidecar

The default `SpectacularSwaggerView` loads swagger-ui from
`cdn.jsdelivr.net/npm/swagger-ui-dist@latest`. Two problems in one URL: the
page needs internet at *view* time and renders blank without it, and `@latest`
means a swagger-ui release can break the docs with no change on this side.

`drf-spectacular-sidecar` plus `'SWAGGER_UI_DIST': 'SIDECAR'` and
`'SWAGGER_UI_FAVICON_HREF': 'SIDECAR'` moves them to local static, pinned by
`drf-spectacular-sidecar==2026.8.1`. Verified: zero `cdn.jsdelivr.net`
references in the page, all four assets 200 from
`/static/drf_spectacular_sidecar/swagger-ui-dist/`.

**Carry into Phase 6:** those URLs work because `DEBUG=True` makes Django serve
static files itself. Under `DEBUG=False` nothing serves them and the docs render
blank — the exact failure the sidecar was added to prevent, arriving by a
different route. Needs `collectstatic` plus whitenoise or nginx.

### Proof the UI actually works

Two tenants named `'string'` appeared in the dev database, created 2026-08-15 at
04:38 and 04:45 with no subscription and no invoice. `'string'` is
drf-spectacular's default example value — those rows are `POST /billing/tenants/`
executed from the Swagger UI's **Try it out**. The UI renders, submits, and
persists.

They were kept deliberately. The recorded baseline therefore moves from 4
tenants to **6**; everything else in it is unchanged.

## What is left

**CI**, and only CI. Lint + tests + Docker build on push, GitHub Actions
against `git@github.com:SupeeerMario/multi-tenant-bailing-system.git`.

**Lint ruleset decided 2026-08-15: ruff, `select = ["F", "E9"]` only.** Bug
rules — undefined names, unused imports, unused variables, f-strings with no
placeholders, syntax errors. Whitespace and line-length rules are deliberately
off: this codebase writes keyword arguments as `name = value` throughout, which
default PEP 8 rejects as `E251`, so a full ruleset would go red on nearly every
file on the first run and the honest fix would be a reformat pass nobody asked
for. Tighten later on purpose, not as a side effect of adding CI.

The test job needs a Postgres service and these env vars, all read in
`config/settings.py`: `DB_ENGINE`, `DB_HOST`, `DB_NAME`, `DB_PASSWORD`,
`DB_PORT`, `DB_USERNAME`.

### Cheap, not blocking

- `TITLE`, `DESCRIPTION` and `VERSION` in `SPECTACULAR_SETTINGS` are still
  defaults, so the docs render as "Swagger" / `0.0.0`.
- The other five endpoints document no `401`, though the security scheme is
  declared globally so a reader can infer it.
- `test_ledgerentry` was renamed to
  `test_ledger_invariants_hold_before_and_after_payment`; the payment test
  names are already claim-shaped.
