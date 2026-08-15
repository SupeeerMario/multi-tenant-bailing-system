# Phase 5 — tests + docs + CI

Covers the two remaining required tests (double-pay, ledger), the production
bug the double-pay test found, the fixture mixin, the Swagger layer with its
live contract verification, and CI. **Phase 5's "Done when" is met as of
2026-08-15** — CI is green on push across lint, tests and Docker build, and
Swagger lists every endpoint.

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

## CI

Built 2026-08-15, green on push. `.github/workflows/ci.yml`, `on: push`, three
jobs — `lint`, `test`, `docker-build` — against
`git@github.com:SupeeerMario/multi-tenant-bailing-system.git`.

Six commits, and the order matters because each one was pushed and read before
the next was written:

| Commit | What it added |
|---|---|
| `fd10750` | ruff `0.16.3` in `requirements.txt`, `pyproject.toml` at the repo root, and the autofix pass it produced |
| `c99136f` | the workflow file with the `lint` job only |
| `ab8b3a1` | the `test` job with **no database** — deliberately red |
| `f423f9e` | the Postgres service, the health check, the six `DB_*` env vars |
| `21fc638` | the `docker-build` job |
| `d4555e5` | `runs-on: ubuntu-lateset` → `ubuntu-latest` |

### The lint ruleset, and why it is this narrow

**ruff, `select = ["F", "E9"]` only.** Bug rules — undefined names, unused
imports, unused variables, f-strings with no placeholders, syntax errors.
Whitespace and line-length rules are deliberately off: this codebase writes
keyword arguments as `name = value` throughout, which default PEP 8 rejects as
`E251`, so a full ruleset would go red on nearly every file on the first run and
the honest fix would be a reformat pass nobody asked for. Tighten later on
purpose, not as a side effect of adding CI.

For the record, ruff's own defaults on this tree:

```
46	RUF012	mutable-class-default
 1	BLE001	blind-except
 1	I001  	unsorted-imports
Found 48 errors.
```

All 46 `RUF012` are `permission_classes = [permissions.IsAuthenticated]` — plain
DRF. The `BLE001` is the deliberate `except Exception` arm of the skip contract
in `generate_invoice_to_all`. Not one of the 48 is a bug. Under `["F", "E9"]`
the same tree answers `All checks passed!`.

### Why a linter at all, demonstrated on this repo's own code

Tests check code that runs. A linter checks code that does not.

`services.py:96-104` — the `except IntegrityError` constraint-name discriminator
— is executed by **zero** of the four tests. None of them provokes an
`IntegrityError` inside `generate_invoice`. So a typo in that block cannot be
caught by the suite. Proven by copying the file to a scratchpad and misspelling
one name:

```
F821 Undefined name `tenat`
   --> services_typo.py:101:50
    |
101 |             raise InvoiceAlreadyExists(f"tenant {tenat.id} has already been invoiced ...
```

Two seconds, nothing executed, and `manage.py test` would still have printed
`OK`. `E9` covers the same class as the `SyntaxError: 'return' outside function`
that killed `runserver` while `docker compose ps` still read `Up 2 hours`.

### `pyproject.toml` belongs at the repo root

`ruff init` was run inside `billing/`, which is where the config first landed.
Ruff resolves configuration **per file**, walking up from each file, so
`billing/*.py` found that config and `config/*.py` walked to the root, found
nothing, and fell back to ruff's built-in defaults. Two packages in one repo
linted by two different rulesets, with nothing reporting it. Moving the file to
the root fixed it. `target-version` was also `py310` against a `python:3.13-slim`
Dockerfile; now `py313`.

The `select` line arrived commented out, which is not an empty ruleset — it is
ruff's defaults, the 48 findings above.

### The test job was built in two pushes, and the first was meant to fail

Push one (`ab8b3a1`) had checkout, setup-python, `pip install -r
requirements.txt` and `python manage.py test`, and no database at all. It failed
exactly where it should:

```
Found 4 test(s).
Traceback (most recent call last):
  ...
  File ".../django/db/backends/base/creation.py", line 206, in _get_test_db_name
    return TEST_DATABASE_PREFIX + self.connection.settings_dict["NAME"]
TypeError: can only concatenate str (not "NoneType") to str
```

Three things were learned from one red run, none of which needed guessing at:

- **`Found 4 test(s).` printed first**, so dependencies installed, Django
  imported, settings loaded and `billing/tests.py` was collected.
- **`CELERY_BROKER_URL` unset in CI is harmless.** `config/__init__.py` imports
  the Celery app on every `manage.py` call, so an import-time problem there would
  have fired before test discovery. It did not.
- **The failure is `DB_NAME` being `None`**, not a connection error. All six
  `DB_*` are `os.getenv` with no default; `NAME` is simply the first one Django
  touches. The prediction going in was `ImproperlyConfigured` on `ENGINE` — wrong,
  because Django silently swaps an empty `ENGINE` for the dummy backend rather
  than raising, and the test runner reads `NAME` before issuing any query.

Django always creates a `test_`-prefixed database rather than using `DB_NAME`
directly, which is why the crash is a string concatenation and not a connect
call.

### The service block, and why there are two `env` blocks

`docker-compose.yml` already had this shape and is the clearest way to see it:
`db` carries `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`, and `web`
carries `DB_ENGINE` / `DB_NAME` / `DB_HOST` / … Two processes, two vocabularies.

CI is that file with one substitution:

- **`services.postgres.env`** carries `POSTGRES_*` and configures the container
  at boot — it is what *creates* the database, user and password.
- **Job-level `env`** carries the six `DB_*` and configures Django — it is how
  `config/settings.py:86-91` *finds* them.

Nothing links the two automatically. They agree only because the same three
values are typed twice. Values are throwaway — `billing_ci` / `ci_user` /
`ci_password` — and written in plaintext on purpose: that database is created
when the job starts, is reachable only from inside the job, and is destroyed with
it. GitHub Secrets would be ceremony protecting nothing, and the real `.env`
values must stay out of a file being pushed.

`POSTGRES_USER` is a superuser in the Postgres image, which is what lets Django
issue `CREATE DATABASE test_billing_ci`.

**`DB_HOST` is `localhost`, not `db`.** Compose resolves service names over a
shared network; on a runner the steps execute on the VM itself, not in a
container, so Postgres is reached through a published port. `ports: - 5432:5432`
is what publishes it. Copying `DB_HOST=db` across from the compose file is a DNS
failure.

**`options:` is what makes the runner wait.** Actions has no `depends_on`: it
always blocks steps until service containers report healthy — but a container
only *has* a health state if health flags were passed. With no `--health-cmd`
there is nothing to poll and the steps begin immediately, racing Postgres's
startup. That is a flaky red, not a reproducible one, which is worse. The string:

```
--health-cmd pg_isready --health-interval 10s --health-timeout 5s --health-retries 5
```

The equivalent in the compose file is `healthcheck:` plus `depends_on: db:
condition: service_healthy` on `web`. Same guarantee, different syntax, and in
Actions the second half is implicit. The wait is visible in the run log under
**Initialize containers**, as repeated
`docker inspect --format="{{.State.Health.Status}}"` until it answers `healthy`.
Interval 10s × 5 retries gives Postgres about 50 seconds; it normally needs two
or three.

### The Docker job

Two steps, checkout and `docker build -t multi-tenant-billing .`. No
setup-python, no service, no env — Docker is preinstalled on the runner.

`docker build` **never executes `CMD`**, so the Dockerfile's `migrate &&
runserver` chain does not run and no database is required. What the job proves is
that the image assembles: `requirements.txt` installs, `COPY` paths resolve. What
it cannot prove is anything about runtime.

The `.` is required. Unlike `ruff check`, which defaults to the current
directory, Docker has no default context and answers `"docker build" requires
exactly 1 argument.`

Timing, measured rather than assumed — a local `--no-cache` build:

```
real	1m2.382s
```

CI is slower: no layer cache between runs, `--no-cache-dir` in the Dockerfile
stopping pip reusing wheels within the build, and the `python:3.13-slim` base
pulled over the network. Three to six minutes is normal. The build context is not
a factor — `.dockerignore` excludes `.venv/` and `.git/`, leaving 612K (`.git`
alone is 17M).

### `runs-on: ubuntu-lateset`

The docker job then sat for five minutes doing nothing. Its log header:

```
Requested labels: ubuntu-lateset
Waiting for a runner to pick up this job...
```

**GitHub does not validate runner labels.** An unknown label is not an error —
it simply matches no runner, so the job queues for 24 hours and then times out.
No failed step, no message, and from the Actions list it is indistinguishable
from a slow build. Diagnosis came from opening the queued job and reading its
header, not from any error.

Fourth instance of "a wrong string in config is accepted silently" in this
project, after the three Celery ones on 2026-08-13, and the worst-behaved of the
four.

It also survived a structural parse check, because that check printed key
*names* and never looked at `runs-on`'s value. The check that would have caught
it asserts values:

```
python3 -c "
import yaml
d = yaml.safe_load(open('.github/workflows/ci.yml'))
for n, j in d['jobs'].items():
    assert j['runs-on'] == 'ubuntu-latest', (n, j['runs-on'])
print('runs-on OK:', list(d['jobs']))
"
```

### YAML traps, all silent or misleading

- **A duplicate top-level `jobs:` key drops the first block entirely.** Two
  `jobs:` mappings were written, one per job. YAML does not merge them — the
  second replaces the first, and `lint` vanished with nothing reported. The parse
  showed `jobs: ['test']`. Detection: the run page must list three jobs.
- **`services:` is three rungs, not two.** `services:` → a name you choose →
  `image:` / `env:` / `ports:` / `options:`. Writing `name: postgres` as a field
  yields `services.name`, not `services.postgres`, and the name key then sits
  empty as `postgres: null` with its would-be children as siblings.
- **`env:` is a mapping; `steps:` and `ports:` are lists.** The rule is per key,
  not per block. `- POSTGRES_DB:` produces a list of one-key mappings with `None`
  values — which is the same shape as the traceback above, arriving from the
  other direction.
- **`- 5432:5432` with no space is the string you want. `- 5432: 5432` with a
  space is a nested mapping.** Verified both ways:
  `{'ports': ['5432:5432']}` versus `{'ports': [{5432: 5432}]}`.
- **`options:` takes a scalar, not a list**, and a value placed on the line after
  it at the same indent is a parse error — `could not find expected ':'`. Same
  line, or indented deeper so YAML folds it into the value.

Every one of these was caught locally by parsing the file before pushing, except
the `runs-on` typo. That is the argument for the check: a malformed workflow
reaches GitHub as an instant red run with no steps executed, and a *well-formed*
one with a wrong value reaches it as silence.

### What CI does not cover

Stated so it is not assumed later:

- `docker build` does not run `CMD`, so `migrate` and `runserver` are unexercised.
- Nothing in CI starts Redis, the Celery worker, or beat. The whole Phase 4
  scheduling layer is untested by CI and remains verified only by the live run
  recorded in `phase-4.md`.
- The lint job's ruleset is narrow by choice. It will not catch a style
  regression, an unused argument, or a mutable default — only the bug classes in
  `F` and `E9`.

## What is left

Phase 5's "Done when" is met. The items below were open before CI landed and
remain open after it; none of them block Phase 6.

### Cheap, not blocking

- `TITLE`, `DESCRIPTION` and `VERSION` in `SPECTACULAR_SETTINGS` are still
  defaults, so the docs render as "Swagger" / `0.0.0`.
- The other five endpoints document no `401`, though the security scheme is
  declared globally so a reader can infer it.
- `test_ledgerentry` was renamed to
  `test_ledger_invariants_hold_before_and_after_payment`; the payment test
  names are already claim-shaped.
