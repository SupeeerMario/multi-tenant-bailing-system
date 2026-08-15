# CLAUDE.md

Guidance for Claude when working on this repository.

## What this project is

A multi-tenant billing API built in Django. It charges companies monthly, keeps
each company's data isolated, and never double-charges. The full spec is a
six-phase build (data model + Docker, core endpoints, idempotent payments,
multi-tenancy + worker, tests + docs, deploy).

## Start here next session

Updated 2026-08-15.

## THE PROJECT IS DONE. Phases 1-6 are closed; Phase 7 is the only thing left, and it is optional.

**Declared complete by the author on 2026-08-15.** Do not open this file looking
for the next task — there isn't one unless Phase 7 is picked up deliberately.

**Live:** `http://89.168.19.242:8000/api/docs/` — an Oracle Cloud server running
this repo's own `docker-compose.yml`. Deployed by SSH, `git clone`, a hand-placed
`.env`, then `docker compose up -d --build`. No separate production compose file
and no registry. **Ship changes by committing locally, pushing, then `git pull`
on the server** — never by editing files on the box, which is how the Dockerfile
drifted between local and deployed during the deploy session.

### Phase 6, done 2026-08-15 — two of three deliverables, third skipped on purpose

- **Live URL** — up and reachable from outside, verified from a third machine.
- **README** (`9daf6f7`, `46099e9`, `ebf9a83`) — mermaid architecture diagram,
  full API walkthrough, the `.env` contract, and the no-double-charge proof
  captured **against the deployed instance**, with the two Swagger screenshots
  in `docs/images/`.
- **Decision note — SKIPPED, deliberately.** The author chose not to write it.
  Its content is not lost: every decision it would have held is argued out under
  "Open design decisions" further down this file. **This is a closed question,
  not an outstanding task.** Do not reopen it or file it as incomplete work.

**The live proof, which supersedes the localhost curls as the project's headline
evidence.** Two `POST /billing/invoices/1/pay/` with the same
`Idempotency-Key: dummy_key`, five minutes apart — `date` headers `18:21:25` and
`18:26:22`, so genuinely two requests — returning **byte-identical** bodies, the
second carrying the first's `paid_at`. The database afterwards:

```
 id |       account       |     amount     |            transaction_id            | invoice_id
  1 | ACCOUNTS_RECEIVABLE |  4647112584.00 | 3f376041-… |  1     invoicing pair
  2 | REVENUE             | -4647112584.00 | 3f376041-… |  1
  3 | ACCOUNTS_RECEIVABLE | -4647112584.00 | 1813cadb-… |  1     payment pair
  4 | CASH                |  4647112584.00 | 1813cadb-… |  1
```

One payment pair, one `IdempotencyKey` row (`COMPLETED`, `200`), `sum = 0.00`,
and `AR = outstanding OPEN = 4647112584.00`. **That the invariant matched at a
non-zero value is the strong form** — `0 == 0` would have passed even with both
queries broken. Rows 5-6, a second invoice, are the documented one-cycle-per-wake
catch-up, not a second charge.

### What Phase 6 changed in the app

Four things, all in `f0fadea` and `2c27389`:

- **`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` now come from the environment.**
  `DEBUG` is a **string comparison**, not a cast — `bool("False")` is `True`, so
  a cast makes every value truthy and production silently runs in debug.
  `ALLOWED_HOSTS` filters empties before splitting, because `''.split(',')` is
  `['']`, a non-empty list holding a host that matches nothing, which also
  disables Django's `DEBUG=True` localhost fallback.
- **whitenoise + `STATIC_ROOT`.** Under `DEBUG=False` Django serves no static
  files, so `/api/docs/` returned `200` while every asset 404'd and the page
  rendered blank. **WhiteNoise indexes `STATIC_ROOT` once at startup**, so
  `collectstatic` after boot changes nothing until the process restarts — which
  is why it runs in the entrypoint, before gunicorn binds.
- **gunicorn behind `entrypoint.sh`**, replacing `sh -c "migrate && runserver"`.
  The script ends on **`exec "$@"`**, not a hardcoded gunicorn line: once an
  `ENTRYPOINT` exists, a Compose `command:` becomes the entrypoint's *arguments*,
  so hardcoding would have silently turned `celery-worker` and `celery-beat` into
  web servers. The `exec` is what makes gunicorn PID 1 — `docker compose stop web`
  now returns in **0.925s** instead of waiting out `SIGKILL`.
- **`migrate` is its own one-shot Compose service**, with `web` gating on
  `condition: service_completed_successfully`. Not in the entrypoint, because
  three containers share that entrypoint and would race each other through
  migrations on every `up`. Proven on the server's first boot: 28 migrations
  applied, exit 0, then `web` started.

### Phase 6 traps, all silent

- **`chmod +x` in the Dockerfile cannot reach a bind-mounted file.** `.:/app`
  overlays the image's `/app` at runtime and carries the **host's** permissions,
  so the build-time `chmod` is invisible in dev. Image showed `-rwxrwxr-x`, host
  showed `-rw-rw-r--`, container answered `permission denied`. **Both fixes are
  needed** — the host bit for dev, the Dockerfile line for production, which has
  no mount. Same mechanism means anything the build writes to `/app` is hidden in
  dev, which is why `collectstatic` lives in the entrypoint.
- **`set -e python manage.py collectstatic --noinput` on one line.** `set` takes
  trailing words as **positional parameters**, so it discarded the gunicorn
  command and `exec "$@"` ran collectstatic instead. All three containers exited
  `0` with nothing in the log looking wrong.
- **`entrypoint: ['python manage.py migrate']`** — a one-element list. Docker
  treats element zero as the executable path and does **not** split on spaces.
  Use separate elements or a plain string.
- **A missing comma in `MIDDLEWARE`.** `'whitenoise'` newline
  `'django.contrib.sessions…'` is implicit string concatenation — one entry
  reading `whitenoisedjango.contrib.…`, whitenoise absent and `SessionMiddleware`
  eaten. Legal Python, no syntax error, and **`["F", "E9"]` does not catch it**;
  the rule is `ISC001`. Worth adding `ISC` if the ruleset is ever widened — it is
  a bug rule, not a style rule.
- **The `Api-Key` prefix.** Pasting a bare key into Swagger's Authorize box gives
  `Authentication credentials were not provided.`, because `authenticate.py:16`
  returns `None` when the first header word does not match. A *wrong key with the
  right prefix* gives `Tenant not found`. **The two messages tell you which
  mistake you made.**
- **gunicorn logs no requests by default.** `runserver` did. Add
  `--access-logfile -` to the `CMD` — as **two** array elements.

### Phase 5, done 2026-08-15

Both remaining required tests landed, Swagger is built and verified end to end,
and CI is green on push across all three jobs. Full record in
`docs/journal/phase-5.md` — read "The bug the double-pay test found" and
"Verifying the documented contract against reality" before touching `pay_invoice`
or the schema.

What landed, three commits plus the CI series:

- **`0965809` — the double-pay test.** `IdempotentPaymentTests` with
  `test_payment_succeeds` and `test_replay_returns_identical_response`. Same
  `Idempotency-Key` twice returns **byte-identical** `response.content` and
  writes **no** second ledger pair. The Phase 3 deliverable is no longer two
  curls in a journal.
- **`98b9ca5` — the ledger test.** `LedgerInvariantTests`, asserting both
  `sum(all) == 0` **and** `sum(ACCOUNTS_RECEIVABLE) == outstanding OPEN`, once
  before the payment and again after. Plus `BillingFixtureMixin`, which both
  payment classes share.
- **`34a88d3` — Swagger.** `drf-spectacular` + sidecar, `/api/schema/` and
  `/api/docs/`, an `OpenApiAuthenticationExtension` so the Authorize button
  works, and `@extend_schema` on `InvoicesPay.post`.

Every assertion but one was **proven able to fail** by mutating the code under
test. The exception is recorded as unproven on purpose: any mutation that
writes a second `IdempotencyKey` row also changes the status code, so the
earlier assertion fires first and the key-count check is never reached.

### The production bug the double-pay test found — read this before touching `pay_invoice`

`services.py:166-170` now wraps the idempotency claim `INSERT` in its own
`transaction.atomic()`, with `except IntegrityError` **outside** the `with`:

```python
try:
    with transaction.atomic():
        idem_key_object = IdempotencyKey.objects.create(...)
except IntegrityError:
    exist = IdempotencyKey.objects.get(...)
```

The `get()` in that `except` only ever worked under autocommit. Postgres marks
the **whole transaction** aborted on any `IntegrityError` and refuses every
later statement, so under `django.test.TestCase` — which wraps each test method
in one `atomic()` block — the replay raised `TransactionManagementError`. The
inner block emits a `SAVEPOINT` and the exception leaving it emits
`ROLLBACK TO SAVEPOINT`, which clears the aborted state before the `except` body
runs. In autocommit the same block is the outermost one and degrades to a real
`BEGIN`/`ROLLBACK`, so production behaviour is unchanged.

**The `except` must stay outside the `with`.** The rewind is emitted by
`atomic.__exit__`; catch the exception inside and `__exit__` never fires. This
supersedes the old "do not wrap the claim in `atomic()`" note — the test was the
revisit that note asked for.

### CI — built and green 2026-08-15

`.github/workflows/ci.yml`, `on: push`, three jobs — `lint`, `test`,
`docker-build`. Remote is
`git@github.com:SupeeerMario/multi-tenant-bailing-system.git`. Lint config is
`pyproject.toml` **at the repo root**.

**Lint ruleset: ruff `0.16.3`, `select = ["F", "E9"]` only.** Bug rules —
undefined names, unused imports, unused variables, f-strings with no
placeholders, syntax errors. Whitespace and line-length rules are **deliberately
off**: this codebase writes keyword arguments as `name = value` everywhere,
which default PEP 8 rejects as `E251`, so a full ruleset goes red on nearly every
file and the honest fix is a reformat pass nobody asked for. Tighten later on
purpose, not as a side effect of adding CI. For the record, ruff's *defaults* on
this tree report 48 findings — 46 `RUF012` on `permission_classes = [...]`, 1
`BLE001` on the deliberate `except Exception` skip arm, 1 `I001`. None are bugs.

**The linter earns its place on unreachable code.** `services.py:96-104`, the
`except IntegrityError` discriminator, is executed by **zero** of the four tests.
Typo `tenant.id` as `tenat.id` in that block and the suite still prints `OK`;
ruff answers `F821 Undefined name 'tenat'` in two seconds. Tests check code that
runs, the linter checks code that does not. `E9` covers the same class as the
`SyntaxError: 'return' outside function` already in the traps list.

**`pyproject.toml` must stay at the repo root.** Ruff resolves config per file by
walking up from it. The `ruff init` boilerplate first landed in `billing/`, which
left `config/` walking to the root, finding nothing, and falling back to ruff's
built-in defaults — two packages in one repo linted by different rules, silently.

Six things about the workflow that the YAML does not explain:

- **`lint` installs ruff alone, not `-r requirements.txt`.** Lint imports no
  Django, so the job stays seconds instead of minutes. Version pinned to
  `0.16.3` so CI cannot go red because ruff shipped a new rule.
- **`DB_HOST` is `localhost`, not `db`.** Compose resolves service names on a
  shared network; Actions steps run on the runner VM and reach Postgres through
  the published port. Copying `db` across from `docker-compose.yml` is a DNS
  failure. `ports: - 5432:5432` is what publishes it.
- **Two `env` blocks, deliberately.** `services.postgres.env` carries
  `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` and configures the
  container at boot. Job-level `env` carries the six `DB_*` and configures
  Django. Two processes, different variable names, linked only by typing the
  same three values twice. Same split as `db` and `web` in the compose file.
  Values are throwaway (`billing_ci` / `ci_user` / `ci_password`) and in
  plaintext on purpose — that database lives for the length of one job and is
  reachable only from inside it. The real `.env` values stay out.
- **`options:` is what makes the runner wait.** There is no `depends_on`
  equivalent: Actions always blocks on service health, but **only if health
  flags are set**. With no `--health-cmd` the container has no health state,
  there is nothing to poll, and steps start immediately into a race. The string
  is `--health-cmd pg_isready --health-interval 10s --health-timeout 5s
  --health-retries 5`, and it is one **scalar**, not a list.
- **`ruff check` bare is fine; `docker build` needs the `.`.** Ruff defaults to
  the current directory. Docker has no default context and answers
  `"docker build" requires exactly 1 argument.`
- **The test job went red before it went green.** First push, no DB env:
  `TypeError: can only concatenate str (not "NoneType") to str` at
  `TEST_DATABASE_PREFIX + settings_dict["NAME"]`. That failure is the evidence
  the six env vars are load-bearing — the same standard the tests are held to.
  It also proved, from `Found 4 test(s).` printing first, that install and
  imports were fine and that `CELERY_BROKER_URL` being unset in CI is harmless.

**YAML traps hit while writing it**, all silent or misleading:

- **A duplicate top-level `jobs:` key drops the first block entirely.** That is
  YAML, not GitHub — the second mapping replaces the first. `lint` vanished with
  nothing reported. Detection: the run page must list **three** jobs.
- **`services:` is three rungs, not two** — `services:` → a name you choose →
  `image:`/`env:`/`ports:`/`options:`. Writing `name: postgres` as a *field*
  produces `services.name`, not `services.postgres`.
- **`env:` is a mapping, `steps:` and `ports:` are lists.** Per key, not per
  block. A `- ` on `env` or `options` silently changes the type.
- **`- 5432:5432` with no space is the string you want; `- 5432: 5432` with a
  space is a nested mapping.**
- **A value on the line *after* `options:` at the same indent is a parse error**
  (`could not find expected ':'`). Same line, or indented deeper.

**Check the file locally before pushing, and assert values, not just structure:**

```
python3 -c "
import yaml
d = yaml.safe_load(open('.github/workflows/ci.yml'))
for n, j in d['jobs'].items():
    assert j['runs-on'] == 'ubuntu-latest', (n, j['runs-on'])
print('runs-on OK:', list(d['jobs']))
"
```

That assertion exists because `runs-on: ubuntu-lateset` shipped and cost a
session — see the config-string trap below. A structural parse check printed the
key names and never looked at the value.

**What CI does not cover, stated so it is not assumed:** `docker build` never
executes `CMD`, so `migrate` and `runserver` are unexercised; and nothing in CI
starts Redis, the Celery worker, or beat.

### Cheap and unblocking, carried into Phase 6

- `TITLE`, `DESCRIPTION`, `VERSION` in `SPECTACULAR_SETTINGS` are still
  defaults, so the docs render as "Swagger" / `0.0.0`.
- The other five endpoints document no `401`. The security scheme is declared
  globally so a reader can infer it, but `pay` is the only one documented
  properly.
- Write isolation on the usage endpoint (a POST under A's key must attach to A's
  subscription — `UsageEventSerializer.subscription` is read-only and
  `perform_create` resolves it from `request.tenant`, so a forged `subscription`
  in the body must be ignored), and the 401 paths.
- `SubscriptionsCreateView.perform_create`'s bare `except IntegrityError`, the
  oldest Phase 2 item. Read "What is actually wrong with the subscription view"
  below first — the payoff is smaller than it looks and copy-pasting the `.diag`
  walk into `views.py` is the wrong shape.

## History — Phase 4, done 2026-08-14

**Both halves met.** The worker was built and verified 2026-08-13; the beat
schedule was corrected and the isolation test written and verified 2026-08-14.
Full record in `docs/journal/phase-4.md`.

- **The beat schedule.** `config/settings.py:149` reads `crontab(minute='*/5')`.
  The old `crontab(minute=5)` meant **once an hour at :05**. Confirmed from
  beat's own log, not from the source: started `16:52:59`, fired `16:55:00`, a
  `*/5` boundary. **A settings change does not reach a running beat process** —
  `docker compose restart celery-beat`.
- **The isolation test.** `UsageIsolationTests(APITestCase)`. Two tenants, ORM
  fixtures, A's key on `GET /billing/usage/` returns exactly A's two events and
  never B's. Verified in **both** directions: green as written, and red when
  `billing/views.py:59`'s filter is mutated to `.all()`. A green test that has
  never gone red is not evidence in this project.

**`crontab(minute='*/5')` is a demo value, deliberately.** It becomes
`crontab(hour=0, minute=0)` — daily at midnight UTC — when the project is done.
Decided 2026-08-14: each subscription carries its own window, so the worker only
has to catch a window the day it ends, but a five-minute tick is observable in a
demo and a daily one is not.

**The mock gateway is deterministic** (`billing/services.py:128-148`) — it
declines only on an unsupported currency, an empty reference, or an amount of
exactly `Decimal('66.66')`. No randomness, so nothing flakes. Keep fixture
amounts away from `66.66`, and remember that value: it is a free decline test,
and the decline path is the one that must *store and replay* its 402.

## History — the worker, done 2026-08-13

Built and verified end to end in one session. **Detail, evidence and the eight
traps hit along the way are in `docs/journal/phase-4.md`** — read it before
touching `billing/tasks.py`, the command, or the Celery services. What follows
is only what shapes future work.

**The loop lives in `services.py`, not in the command.** `generate_invoice_to_all()`
is a plain function — no `BaseCommand`, no Celery import — because three callers
want it: the management command, the Celery task, and a test. It **returns** a
list of `(tenant_id, outcome, detail)` tuples rather than printing; the command
formats them with `self.stdout.write`, the task with a logger. Decision 2's rule,
applied again.

**`BillingError` is the skip contract.** Two arms, specific first:

```python
except BillingError as e:   -> 'skipped'    expected, not due, guarded
except Exception as e:      -> 'errored'    a real bug
```

`except X` matches every subclass, so all of `PeriodNotEnded`,
`OpenInvoiceNotPaid`, `NoActiveSubscription`, `InvoiceAlreadyExists` land in the
first arm. **`BillingError` is itself an `Exception`, so ordering is
load-bearing** — the general clause above the specific one makes the specific one
dead code. And nothing automatic decides a failure is benign: a new expected
failure raised as a bare `ValueError` gets filed as `errored`. **New expected
failures must subclass `BillingError`.**

**No `atomic()` around the loop.** Each `generate_invoice` owns its transaction.
One wrapping block would roll back the invoices already written for earlier
tenants and poison the connection for every tenant after the first
`IntegrityError`.

**Only `errored` makes the command exit non-zero** (`CommandError`). `skipped` is
a normal run — on the current dev database *every* tenant skips, so an exit-1 on
skips would fire every single time and mean nothing.

**Celery over a loop container**, decided 2026-08-13. The `sleep`-loop container
was trivial and would have served the demo, but Phase 7 puts two web replicas
behind nginx and it would have to be deleted then anyway. Redis is the broker,
no result backend — the worker log is the only trace a run leaves.

**Two rules that outlive the phase:**

- **Exactly one beat container, ever.** The worker scales; beat does not. Two
  beats is two billing runs per tick.
- **A double-fire cannot double-bill.** `unique_invoice_period`,
  `unique_open_invoice_per_tenant` and the guard turn a repeat run into a clean
  skip. The safety net was built before the thing that needs it, deliberately.

**Verified live**, baseline `4/1/5/4/12` unchanged: the command skipped all four
tenants with truthful reasons through `call_command`; a forced run with two
throwaway tenants produced `billed` (invoice 102, ledger `12 -> 14`) and
`errored` (a patched `TypeError`) and raised `CommandError: 1 tenants errored`;
and with all five services up, beat sent the due task and the worker executed it
in 0.103s. **Not proven:** continuation *past* an errored tenant — the broken
tenant happened to be last in the loop.

## History — prerequisite 2, done 2026-08-13

**Both pieces built and verified live in rolled-back transactions.** This closes
the last Phase 4 prerequisite.

**1. The guard**, `billing/services.py:50-52` — above the `try:` and above
`with transaction.atomic():`, before the `PeriodNotEnded` check:

```python
open_invoice = Invoice.objects.filter(tenant = tenant, status = 'OPEN').first()
if open_invoice:
    raise OpenInvoiceNotPaid(f'tenant {tenant.id} has an open invoice {open_invoice.id}')
```

`OpenInvoiceNotPaid` is a new `BillingError` in `services.py` with its
`APIException` twin in `exceptions.py` at `409`, wired into `InvoiceAPIView.post`.
`InvoiceAlreadyExists` was deliberately **not** reused — the two mean different
things: `unique_invoice_period` asks "is *this window* already billed?",
`unique_open_invoice_per_tenant` asks "does this tenant owe money *right now?*",
and the second is window-independent.

Verified live, baseline `4/1/5/4/12` unchanged before and after:

```
Acme, period NOT ended        OpenInvoiceNotPaid | tenant 13 has an open invoice 17
Acme, period forced past      OpenInvoiceNotPaid | tenant 13 has an open invoice 17
clean tenant, ended period    invoice 96 OPEN 10.00      <- must NOT raise
same tenant now holds 96      OpenInvoiceNotPaid | tenant 45 has an open invoice 96
```

Row three is the one that matters. The first draft used
`Invoice.objects.get(...)`, which raises `Invoice.DoesNotExist` when there is no
`OPEN` invoice — a plain `Exception`, invisible to DRF, so **every clean tenant's
generate call was a 500**. `.first()` and a `None` test, not `get()`. The `if`
around a `get()` is dead code besides: `get()` never returns falsy.

**2. The discriminator**, `billing/services.py:86-94` — replaces the single
`except IntegrityError` that blamed `unique_invoice_period` whichever constraint
fired:

```python
except IntegrityError as e:
    name = getattr(getattr(e, '__cause__', None), 'diag', None)
    name = getattr(name, 'constraint_name', None)
    if name == 'unique_invoice_period': ...
    if name == 'unique_open_invoice_per_tenant': ...
    raise
```

The chain is `IntegrityError` -> `e.__cause__` (`psycopg.errors.UniqueViolation`)
-> `.diag` (`psycopg.errors.Diagnostic`) -> `.constraint_name` (the string).
**Match on the name, not the type** — both of these are `UniqueViolation`, so
`type(e.__cause__)` cannot separate them. The `getattr` guards matter because an
`AttributeError` raised *inside* the handler replaces the `IntegrityError` and
the traceback then names the wrong bug entirely; the bare `raise` at the end
sends anything unmatched up as a 500, which is the honest answer for an
integrity error nobody anticipated.

Verified live, each branch isolated, baseline unchanged:

```
PAID invoice covers the same window, guard passes naturally
  InvoiceAlreadyExists | tenant 46 has already been invoiced ... 2026-06-14 to 2026-07-14
OPEN invoice on a DIFFERENT window, guard blinded to fake the race
  OpenInvoiceNotPaid | tenant 47 has an open invoice
```

The second is the exact input the old code got wrong. Blinding the guard —
`mock.patch.object(Invoice.objects, 'filter')` returning `None` — is how the
concurrent race was simulated without threads; `Invoice.objects.filter` appears
nowhere else in `generate_invoice`, so the patch is precise.

**Trap, caught in review before it ran:** the first discriminator interpolated
`open_invoice.id` into the `unique_open_invoice_per_tenant` message. Execution
only reaches the `try` when `open_invoice is None` — the guard raised otherwise —
so that branch was a guaranteed `AttributeError: 'NoneType' object has no
attribute 'id'` inside the `except`. The `except` branch has no invoice in hand
by construction; it knows a constraint name and nothing else.

**Not verified:** the bare `raise` fallthrough. No unmatched integrity error is
reachable through `generate_invoice` today, so that arm is untested — do not
record it as proven.

**Ordering consequence, confirmed live.** The guard sits **above** the
`PeriodNotEnded` check, so a tenant holding an unpaid invoice answers
`OpenInvoiceNotPaid` even when its window has not ended. Acme today
(`Aug 1 -> Sep 1`, not ended, invoice `17` `OPEN`) therefore answers
`OpenInvoiceNotPaid`, **not** the `PeriodNotEnded` recorded further down this
file. Over HTTP nothing changes — `InvoiceAPIView.post` duplicates both checks
itself at `billing/views.py:71-77` and raises `PeriodNotEnded` before the service
is ever called. Only the worker sees the service's own ordering.

### What is actually wrong with the subscription view

`SubscriptionsCreateView.perform_create` (`billing/views.py:37-41`) still reports
every `IntegrityError` as `SubscriptionAlreadyExists`. Now that the discriminator
exists, closing it looks like a copy-paste. Three reasons it is not:

1. **The second branch is unreachable from that view.** `Subscription` has two
   constraints — `unique_active_subscription_per_tenant` and
   `prevent_zero_length_period` (`billing/models.py:88-100`). The view computes
   `period_end = period_start + relativedelta(months = 1)`, which is *always*
   greater than `period_start`, so the check constraint can never fire on that
   path. The journal's `UniqueViolation` vs `CheckViolation` example
   (`docs/journal/phase-2.md`, item 1 under "Still not started") predates that
   line. The discriminator there degrades to "match one name, else re-raise" —
   still worth doing, because an unanticipated integrity error currently gets a
   confident 409 explaining the wrong thing, but the payoff is smaller than the
   journal implies.
2. **`.diag` is psycopg-specific.** It is already a driver dependency inside
   `services.py`; pasting the walk into `views.py` spreads it to a second module.
   One shared helper — `constraint_name_of(exc)` — imported by both is the right
   shape, and the `services.py` copy should move into it at the same time.
3. **The constraint names would then be strings duplicated across three files**
   (`models.py`, `services.py`, `views.py`). A typo in a comparison is silent:
   the branch simply never fires and the caller gets a 500 that looks unrelated.
   Module-level constants that `models.py` and both consumers reference kill
   that.

**Committed 2026-08-13** together with the leftovers from the previous session:
`CLAUDE.md`, `billing/models.py`,
`billing/migrations/0009_remove_invoice_unique_invoice_period_and_more.py`,
`billing/migrations/0010_invoice_unique_open_invoice_per_tenant.py`. Both
migrations were **already applied to the dev database** before the commit. The
Acme data repair is applied too and is tracked in no file — only
`scratchpad/untangle_acme.py`, which is outside the repo and will not survive.

## History — prerequisite 1, done and committed as `774c91f`

**Prerequisite 1 is built and verified live, all three steps.** Phase 3 remains
done, its "Done when" met and committed.

What landed, and the evidence for each:

1. **The serializer guard**, `billing/serializers.py:55` — the explicit
   `plan = serializers.PrimaryKeyRelatedField(queryset = models.Plan.objects.filter(is_active=True))`
   above `class Meta` on `SubscriptionsSerializer`. **Verified per-request, not
   frozen at import**: with plan `2` active the POST returned `409` (the
   `unique_active_subscription_per_tenant` conflict, which means the field
   passed); flipping `is_active=False` in the shell **without restarting `web`**
   turned the same POST into `400 {"plan":["Invalid pk \"2\" - object does not
   exist."]}`; flipping back returned it to `409`. The 400 arrives before the
   view reaches the constraint, and no subscription was written on either `409`,
   so the baseline is untouched.

   **Trap worth keeping:** the first attempt put that line on `PlanSerializer`.
   A `Plan` has no `plan` field, so it guarded nothing *and* took the plans
   endpoint down entirely — DRF asserts every declared field appears in
   `Meta.fields`, so every request touching the serializer 500s with
   `The field 'plan' was declared on serializer PlanSerializer, but has not been
   included in the 'fields' option.` A declared field on the wrong serializer is
   not a no-op.
2. **The `generate_invoice` branch**, `billing/services.py:73-77` — inside the
   same `with transaction.atomic():` that opens at line 68, with
   `active_subscription.save()` left **outside** the `if/else` so both arms
   persist. Verified live inside one rolled-back `transaction.atomic()` using a
   throwaway tenant, plan and subscription (period `Jun 1 -> Jul 1`, already
   ended):

   - **Inactive plan:** invoice written `OPEN` for the full `Jun 1..Jul 1`
     window, 2 ledger rows, sum `0.00`, one shared `transaction_id`, then
     `status=CANCELED` with `current_period_start` / `current_period_end` left
     at the last billed window. The second wake raised
     `NoActiveSubscription: tenant 43 has no active subscription` — accrual
     stops, exactly as option (b) intends.
   - **Active plan (control):** advanced to `Jul 1 -> Aug 1`, stayed `ACTIVE`.
     Its second wake generating another invoice is **correct**, not a bug — that
     window has also ended as of Aug 12, so it is the documented one-cycle-per-
     wake catch-up.

   Counts after rollback confirmed the baseline: `tenants=4 plans=1 subs=5
   invoices=4 ledger=8`. Only residue is the invoice id sequence burning `91`
   and `92`, which Postgres sequences do regardless of rollback.

   **What that test cannot prove:** it rolls back both writes, so it never
   demonstrates that the invoice and the cancel commit *together*. That
   guarantee comes from the code being inside one `atomic()` block, not from the
   run.
3. **`DRAFT` dropped** from `INVOICES_CHOICES` at `billing/models.py:110`, with
   `0008_alter_invoice_status.py` carrying the `AlterField` only. Applied —
   `showmigrations` reads `[X] 0008`. Grep confirms no `DRAFT` outside
   `0001_initial.py`, which stays untouched.

Phase 4 proper — worker, schedule, isolation test — is summarised once under
"History — Phase 4, done 2026-08-14" near the top of this file. Do not maintain
a second copy here.

### Dev database baseline — moved 2026-08-12, read this before trusting old numbers

**Acme was wedged and is now repaired.** The three-`OPEN`-invoice state the
earlier baseline recorded was not just in the way of prerequisite 2 — it was
broken data that the Phase 4 worker would have hit silently. Diagnosis, in
order:

- Acme's invoices `18` (`Aug 1 -> Sep 1`) and `19` (`Sep 1 -> Oct 1`) were both
  created `2026-08-05`, billing periods that **had not ended**. Today's code
  cannot produce them — `PeriodNotEnded` fires first — so they are artifacts of
  the pre-`fb35920` signature, when the caller supplied the window. Nothing is
  wrong with the current service.
- Subscription `8` still pointed at `Jul 1 -> Aug 1`, behind both, because the
  old path never advanced it. That window has ended, so every wake tried to
  rebill it and hit invoice `17`. Confirmed live in a rolled-back transaction:
  `InvoiceAlreadyExists: tenant 13 has already been invoiced a bill from
  2026-07-01 ... to 2026-08-01`. `InvoiceAlreadyExists` is a `BillingError`, so
  the worker would have caught it and skipped — **Acme silently never bills
  again, with nothing raised where a human looks.**

The repair, run 2026-08-12 in one `transaction.atomic()`, by hand in the shell —
**it lives in no migration and no service, only in
`scratchpad/untangle_acme.py`**:

- `18` and `19` voided *properly*: a reversing pair each
  (`ACCOUNTS_RECEIVABLE -amount` / `REVENUE +amount`, one new `transaction_id`
  per invoice, `description` actually set — `void of invoice 18`), then
  `status='VOID'`. The ledger was appended to, never edited.
- Usage event `5` (`7777` units, Aug 1) released from `18` back to
  `invoice=None`. **`UsageEvent.invoice` is `SET_NULL`, but `SET_NULL` only
  fires on delete** — voiding does not delete, so without this line the event
  stays stamped to a void invoice and is never billed again. Any future
  `void_invoice` service must carry this explicitly.
- Subscription `8` advanced to `Aug 1 -> Sep 1`, past what invoice `17`
  legitimately covers.

**New baseline, verified 2026-08-12, tenant count amended 2026-08-15:**

```
6 tenants / 1 plan / 5 subscriptions / 6 usage events / 4 invoices / 12 ledger rows
global ledger sum 0.00, 0 idempotency keys
  Acme       AR=30.00  outstanding OPEN=30.00  PASS
  Globex     AR=36.67  outstanding OPEN=36.67  PASS
  Initech    AR=0      OPEN=0                  PASS
  test name  AR=0      OPEN=0                  PASS
```

Acme's invoices are now `17` `30.00` `OPEN` (`Jul 1 -> Aug 1`), `18` `39.44`
`VOID`, `19` `20.00` `VOID`; Globex still holds `20` (`36.67`, `OPEN`). Ledger
went `8 -> 12` rows globally (Acme `6 -> 10`). The stronger invariant —
`sum(ACCOUNTS_RECEIVABLE)` equals outstanding `OPEN` — passes for every tenant,
which is the check that catches what sum-to-zero misses. No demo payment has
ever touched real seed data, deliberately.

**Tenant count moved 4 → 6 on 2026-08-15, and this is expected.** Tenants `50`
and `51`, both named `'string'`, were written by clicking **Try it out** on
`POST /billing/tenants/` in the new Swagger UI — `'string'` is
drf-spectacular's default example value. They are orphans: no subscription, no
invoice, nothing references them. **Kept deliberately** as the evidence that the
UI renders, submits and persists. Every other number in the block above is
unchanged, and the ledger verification below still passes for all four real
tenants. Contract verification against `pay_invoice` was done with `APIClient`
inside a rolled-back `transaction.atomic()`, so it left no rows at all.

Acme's next wake was recorded here as answering `PeriodNotEnded: Cannot make a
bill for 2026-09-01 ... as the period has not ended`. **Stale as of 2026-08-13**
— the new guard sits above the period check, so the service now answers
`OpenInvoiceNotPaid: tenant 13 has an open invoice 17`. Both statements are true
and either way it is a correct skip. Over HTTP it is still `PeriodNotEnded`,
because the view checks that itself first. On Sep 1, once `17` is paid or voided,
it bills `Aug 1 -> Sep 1` and picks up the released event `5`.

**That last part only works because of `0009`.** `unique_invoice_period` was
unconditional, so a `VOID` row kept reserving its period forever and voiding
`18` would have re-wedged Acme on Sep 1 with the identical error. The constraint
now carries `condition=~Q(status='VOID')` (`billing/models.py:130`), applied as
migration `0009_remove_invoice_unique_invoice_period_and_more` — a
`RemoveConstraint` plus an `AddConstraint`, which validates trivially because
the new index covers strictly fewer rows than the old one. Postgres renders it:

```
"unique_invoice_period" UNIQUE, btree (tenant_id, period_start, period_end)
  WHERE NOT status::text = 'VOID'::text
```

Same mechanism as `unique_active_subscription_per_tenant`, condition inverted. A
voided invoice drops out of the index entirely, so its period is free to be
rebilled — which is what voiding is supposed to mean. Two `OPEN`s, or an `OPEN`
plus a `PAID`, on one window are still rejected; neither is `VOID`. `status` is
`NOT NULL`, so the `~Q` has no NULL hole.

**Committed 2026-08-13** alongside the guard work. The data repair is applied to
the database and is in no file under version control.

Left open in Phase 3, not blocking the commit, and all three are the same
question about key lifetime — decide them together:

- **Orphaned `PROCESSING` rows — narrowed 2026-08-10, not closed.** The
  `status != 'OPEN'` path now stores a `409` instead of raising, so only a
  genuine crash between the claim and the stamp can strand a key. `PROCESSING`
  therefore has one meaning now, which is what makes an age-based rule usable.
- **A decline burns the key.** A retry after a decline replays `402` forever, so
  a customer who fixes their card can never pay that invoice with the same key.
  Correct as designed, but it needs the "one key per attempt" line in the Phase 6
  README, and a **5-minute TTL on `COMPLETED 402` keys** is agreed and unbuilt.
- **`COMPLETED 200` keys never expire.** Real gateways age keys out at 24h.

See "A decline burns the key" and "Orphaned `PROCESSING` rows" in
`docs/journal/phase-3.md` for the
shapes and the race that the TTL has to avoid.

Still the oldest unstarted Phase 2 item, untouched for a sixth session:
narrowing the bare `except IntegrityError` in
`SubscriptionsCreateView.perform_create`. The discriminator now exists in
`services.py` and is verified, but read "What is actually wrong with the
subscription view" at the top of this file before pasting it across — item 1
under "Still not started" in `docs/journal/phase-2.md` describes a
`CheckViolation` branch that view can no longer reach.

### Phase 4 — the two prerequisites

Both were raised as things that bite the moment an unattended worker exists, and
the author chose to do them **before** step 1. **Prerequisite 1 is built and
verified; prerequisite 2 is still blocked on data.**

**1. Nothing checks `plan.is_active` before subscribing. DECIDED 2026-08-12:
`is_active = False` means "no tenant can subscribe AND no billing is provided" —
reading 2 — with mid-cycle deactivation resolved as (b), bill the final full
period then cancel. BUILT AND VERIFIED 2026-08-12** — the serializer guard, the
`generate_invoice` branch and the `DRAFT` drop are all committed, with the live
evidence recorded under "History — prerequisite 1" below. What follows is why
the decision went this way; keep it, because the reasoning is not recoverable
from the three lines of code it produced.

Reading 2 means the worker has to check too, which forced a second question: a
plan deactivated **mid-cycle**. Concretely — Acme's cycle is `Aug 1 -> Sep 1`,
admin sets `is_active=False` on Aug 15, the worker wakes Sep 1:

- **(a) skip silently — rejected.** `generate_invoice` raises, the worker catches
  `BillingError` and continues. `current_period_end` never advances, the
  subscription stays `ACTIVE`, and the usage endpoint keeps accepting events that
  no window will ever bill. Silent revenue loss with nothing raised — the exact
  shape of the stale-window invoice, the `relativedelta(month=1)` cascade, and
  the behind-by-a-cycle skip.
- **(b) bill the final full period, then cancel — CHOSEN.** The customer had
  access for the whole month, so the month is billed; then `status = 'CANCELED'`
  instead of the period advance. The next wake raises `NoActiveSubscription`,
  accrual stops, and the usage endpoint is closed too (it resolves the
  subscription with `get(status='ACTIVE')`). Needs no new fields. Events recorded
  between the deactivation and the worker run fall inside the billed window and
  are charged correctly.
- **(c) hard stop, no final invoice — rejected.** Matches "no billing" most
  literally, but Aug 1–15 usage is then served free.

Consequence, now confirmed live rather than predicted: a `CANCELED` row keeps its
old `current_period_start` / `current_period_end`. That is stale but harmless —
it now reads as "last billed period", and a later re-subscribe creates a new row
with its own window.

**This one cannot have the usual DB guarantee.** A Postgres `CHECK` cannot
reference another table, so no `CheckConstraint` can say "the plan I point at is
active". The validation-layering rule from the negative-money work does not fully
apply — there is a serializer layer and a service layer, and that is all. Do not
go looking for the constraint later.

**`DRAFT` is dropped alongside this, decided and done 2026-08-12** (migration
`0008`, applied). Nothing writes it and
there is no gap for it to live in: `generate_invoice` writes `OPEN` and books the
AR pair in the same `atomic()` block, whereas `DRAFT`'s real meaning in billing is
"invoice exists, no ledger pair booked yet". It would also sit outside
`unique_open_invoice_per_tenant`'s partial index and pile up freely. `VOID`
stays — it is the only correct cancel path, and it is what unblocked
prerequisite 2 below.

**2. `unique_open_invoice_per_tenant` — DONE. The constraint was built and
applied 2026-08-12 (migration `0010`); the `OpenInvoiceNotPaid` guard and the
`except IntegrityError` discriminator landed and were verified live
2026-08-13** — evidence under "History — prerequisite 2" at the top of this file.
What is in `Invoice.Meta`:

```python
models.UniqueConstraint(fields = ['tenant'], condition = Q(status = 'OPEN'),
                        name = 'unique_open_invoice_per_tenant')
```

`AddConstraint` validates existing rows, and it used to fail exactly like the
`-99.00` plan did, because Acme held three `OPEN` invoices. **Cleared
2026-08-12** by the void repair described under "Dev database baseline" above —
Acme is down to invoice `17` alone and every tenant has at most one `OPEN`, so
`0010` applied cleanly, a single `AddConstraint` with no `RemoveConstraint`.

The two ways of clearing it were weighed and **voiding won on the merits, not on
cost**: `18` and `19` were invoices for periods that had never ended, so paying
them would have booked `CASH` for money that was never legitimately owed.

**Do not re-derive the guard placement from the old rationale.** The line that
used to sit here — "placed at the top the period advance never runs" — was
wrong. The advance is inside `atomic()` and rolls back either way, so you get
that outcome from the rollback, not from where the guard sits. The real reason
for the placement is that it is a precondition: check before doing work, so the
caller gets a truthful error instead of a misattributed one. Same correction
applies to the guard-placement rationale in `docs/journal/phase-3.md`.

**`void_invoice` is still unwritten.** The repair was done by hand in the shell,
so the domain still has no code path for voiding — it remains the only operation
here with none. When it gets written, it has to do all three things the manual
repair did: append the reversing pair (`ACCOUNTS_RECEIVABLE -amount` /
`REVENUE +amount`, one shared new `transaction_id`), release the invoice's usage
events to `invoice=None`, and only then set `status='VOID'`, all in one
`atomic()` block. Setting the status alone **breaks the ledger invariant** — AR
stays booked while `OPEN` no longer counts the invoice, and the books read
settled while still claiming the money.

## Phase gating — read this first

**Do not start work on a phase until the previous phase meets its "Done when"
in full.** Each phase has an explicit completion condition, and the schema being
in place is not the same as the phase being finished. If the author asks for
Phase N work while Phase N-1 is incomplete, say so and name the specific
remaining item rather than going along with it.

| Phase | "Done when" | Status |
|---|---|---|
| 1 — Data model + skeleton | `docker compose up` starts API + Postgres, migrations create all tables | **Done** (verified 2026-07-30) |
| 2 — Core endpoints | Create tenant → assign plan → record usage → generate correct invoice, all via API | **"Done when" met** (verified 2026-08-08) — full flow runs over HTTP. Auth layer, `generate_invoice`, and five endpoints done (tenant, plan, subscription, usage read+write, generate-invoice). Remaining cleanup, not blocking Phase 3: invoice/ledger read endpoints, pagination, narrowing the bare `except IntegrityError` |
| 3 — Idempotent payments | Same payment request twice returns same result, charges once (save both curl commands + output) | **"Done when" met** (verified 2026-08-10) — gateway, `pay_invoice`, the pay endpoint, the header guard, `request_hash`, the claim `INSERT`, all four collision arms, and the stored-and-replayed decline are done and verified live. Two identical requests return byte-identical bodies and write one ledger pair. Both curls and their output are pasted in "The two-curl proof" in `docs/journal/phase-3.md`. Remaining cleanup, not blocking Phase 4: orphaned `PROCESSING` rows, `LedgerEntry.description` still `''`, `InvoicesPay`'s duplicate invoice lookup |
| 4 — Multi-tenancy + worker | Worker generates invoices on a schedule; tenant isolation proven by a test | **"Done when" met** (verified 2026-08-14). Both prerequisites closed 2026-08-12/13; `generate_invoice_to_all`, the `generate_invoices` command, Redis, Celery worker and beat verified end to end 2026-08-13. 2026-08-14: beat schedule corrected to `crontab(minute='*/5')` and confirmed from beat's log, and `UsageIsolationTests` in `billing/tests.py` proves A's key returns only A's usage events — verified both green and red (mutation of `billing/views.py:59`). Remaining cleanup, not blocking Phase 5: write-isolation and 401 assertions, continuation past an errored tenant |
| 5 — Tests + docs + CI | CI green on push (lint + tests + Docker build), Swagger lists every endpoint | **"Done when" met** (verified 2026-08-15). All three required tests pass (`0965809` double-pay, `98b9ca5` ledger, isolation from Phase 4); Swagger lists all 6 routes / 7 operations with auth and the `Idempotency-Key` header documented (`34a88d3`); `.github/workflows/ci.yml` green on push with `lint`, `test` and `docker-build`, ruff pinned at `0.16.3` with `select = ["F","E9"]`. Remaining cleanup, not blocking Phase 6: `SPECTACULAR_SETTINGS` `TITLE`/`VERSION`, `401` on the other five endpoints, usage write-isolation test |
| 6 — Deploy + package | Live URL, README with architecture diagram + no-double-charge proof, decision note | **Closed 2026-08-15 on the author's revised terms.** Live at `http://89.168.19.242:8000/api/docs/` on an Oracle Cloud server; README carries the mermaid diagram, the API walkthrough, the `.env` contract and the live no-double-charge proof with screenshots. **The decision note was skipped deliberately** — its content lives under "Open design decisions" in this file. Not an open task |
| 7 — Horizontal scale | nginx in front of 2 identical web containers, one Postgres; the same-key retry proof rerun across containers, not threads | **Unblocked, not started, optional.** Outside the original six-phase spec. Considered for removal 2026-08-15 and kept. Prerequisites are already in place — `migrate` is a one-shot service and gunicorn is PID 1, the two things `phase-7.md` named as blockers |

## Build journal — not loaded by default

The phase-by-phase build record lives in `docs/journal/` and is **not** in
context unless you load it. Invoke the `project-journal` skill, or read the file
directly:

| File | Covers |
|---|---|
| `docs/journal/phase-2.md` | Phase 1 verification, `generate_invoice`, all five Phase 2 endpoints, negative money, zero-length periods, ordering, `__str__` |
| `docs/journal/phase-3.md` | The double-charge repro, the `Idempotency-Key` claim, the two-curl proof, the decline/TTL contract, check ordering |
| `docs/journal/phase-4.md` | The service loop, the `BillingError` skip contract, the management command, Redis/Celery/beat wiring, the eight traps hit, the end-to-end run, the beat-schedule correction, and the isolation test with its fixture and assertion shape |
| `docs/journal/phase-5.md` | The double-pay and ledger tests with a mutation proof per assertion, the savepoint bug the double-pay test found, `BillingFixtureMixin` and MRO order, the Swagger wiring, the live verification of all seven response codes, and CI — the six-commit build order, the deliberately-red test job, the two `env` blocks, the health-check wait, the `runs-on` typo, and the YAML traps |
| `docs/journal/phase-7.md` | Horizontal-scale plan, the `migrate`-in-`CMD` blocker |

The "Traps hit, worth not re-learning" lists were deleted on 2026-08-11. The
ones that kept repeating survive under "Traps that repeat" below; the rest were
one-time typos and version-rename facts that a traceback already names.

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

### Traps that repeat

Promoted out of the deleted per-phase traps lists on 2026-08-11. Every entry
here bit more than once, or produced a **silent** wrong answer rather than a
traceback. Read before writing a service function, an `except` clause, or a
ledger write.

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
- **A `default=` callable fires once per row.** `transaction_id` is
  `UUIDField(default=uuid.uuid4, ...)`, so two `create()` calls produced two
  different uuids and the "pair" was two unrelated one-sided rows. Caught live:
  `distinct transaction_ids: 2`. `sum == 0` still passed, because summing the
  whole table hides it — the pair link is what breaks. Same mechanism as
  `default=make_key` on `Tenant.api_key`, where per-row evaluation is the
  desired behavior.
- **That `if not tenant` was dead anyway.** `IsAuthenticated` runs before the
  handler, so an absent tenant is a 401 and the method is never entered. Fifth
  time this shape has appeared in the project (see the `services.py` notes on
  `filter()` never being `None` and `get()` never returning falsy). When writing
  the next `if not ...`, ask what would have to fail upstream for it to fire.
- **The `runserver` reloader can miss bind-mount edits.** A `NameError` for a
  renamed class survived two runs while `docker compose exec web grep` showed the
  container reading the **new** file — the process was serving stale bytecode.
  Sibling of the stale-`COPY` trap under Known open items, different mechanism: if a result
  contradicts source you just read, confirm the process reloaded, not just the
  file. `docker compose restart web`.
- **A wrong string in config is accepted silently — four instances now, and the
  newest is the worst-behaved.** `runs-on: ubuntu-lateset` (2026-08-15,
  `.github/workflows/ci.yml`) matched no runner. **GitHub does not validate
  runner labels**, so an unknown one is not an error — the job simply sits in
  `Waiting for a runner to pick up this job...`, queues for 24 hours, then times
  out. No failed step, no message, and the run reads as a slow build. Diagnosed
  only by opening the queued job and reading `Requested labels: ubuntu-lateset`
  in its header. The three from 2026-08-13, all Celery: `CELERY_BROKER_URL` set
  to the literal string
  `CELERY_BROKER_URL` was parsed as a *hostname* and fell back to
  `transport: py-amqp`, so the worker would have retried RabbitMQ forever. The
  task file named `task.py` instead of `tasks.py` was never imported by
  `autodiscover_tasks()`, so the task registered nowhere. The beat entry naming
  `billing.generate_invoices_task`, then `billing.tasks`, published fine while
  the worker answered `Received unregistered task of type ...` once per tick and
  beat's own log looked healthy. None of the three raised at startup. **After
  any config-string change, print what the system resolved it to** —
  `celery_app.conf.broker_url`, `.connection().transport.driver_name`,
  `entry['task'] in set(celery_app.tasks)` — do not read the string back and
  assume.
- **Plain `Exception` subclasses are invisible to DRF.** `services.BillingError`
  and its subclasses are not `APIException` and not `Http404`, so an uncaught one
  is a **500**, not a 409. Same for `Subscription.DoesNotExist`.
- **The two `NoActiveSubscription` classes shadow each other.** One in
  `views.py` (an `APIException`), one in `services.py` (a `BillingError`). Inside
  `views.py` a bare `except NoActiveSubscription:` binds the local one, which the
  service never raises — so the clause never fires, the real exception escapes,
  and the result is a 500 with no hint at the cause. The `services.` prefix is
  mandatory in that except clause.
- **`manage.py check` does not execute function bodies.** It reported "no
  issues" while `post()` still contained `invoice.save(period_start=...)`,
  `Subscription.current_period_end`, and `period_start + 1` — three `TypeError`s
  waiting. A clean check means the module imports, not that the code works.
- **A parameter accepted and never used is an isolation hole.** `pay_invoice`
  took `tenant` and looked up `filter(id=invoice_id)` without it — any tenant
  could pay any invoice by id. It was masked only by the view's duplicate lookup,
  which was about to be deleted.
- **`except: pass` and `except: raise PaymentDeclined` are both worse than no
  handler.** The second is the dangerous one: the gateway has *already
  succeeded*, so an internal error after it gets reported to the customer as
  "declined" while their account is debited, and they retry. A 500 is the honest
  answer — "something broke, state unknown".
- **Three ledger rows do not sum to zero.** Adding `CASH` while keeping `REVENUE`
  gave `REVENUE +30 / AR -30 / CASH +30` = `+30.00`. A payment is exactly two
  rows; the revenue was already booked at invoice time. Keeping `REVENUE` and
  dropping `CASH` is the other failure — every account nets to zero and the books
  claim you earned nothing, hold nothing, and are owed nothing after being paid.
- **The view and the service hold two different `Invoice` instances of the same
  row.** Serializing the view's copy after the service mutated its own returns
  `status: "OPEN"`, `paid_at: null` on a *successful* payment. Serialize what the
  service returns — which also means the service has to `return invoice`, easy to
  omit since the function ends on a `create()` call.
- **`filter()` where `get()` was meant — seventh appearance.**
  `invoice = Invoice.objects.filter(id=invoice_id)` then `invoice.status` is
  `AttributeError: 'QuerySet' object has no attribute 'status'`, and the
  `except Invoice.DoesNotExist` below it is unreachable because `filter()` never
  raises.
- **Comparing a `Decimal` to a `str` is silently always unequal.**
  `Decimal(str(x)) != str(invoice.amount)` made every payment a mismatch. The
  `str()` belongs on the *input*, to dodge float expansion, never on the invoice.
- **Deleting the view's duplicated checks made the service's exceptions
  unreachable, then removing the `try` made them 500s.** `BillingError` subclasses
  are invisible to DRF. The `except services.X as e: raise <twin>(e)` clauses are
  load-bearing, and the `services.` prefix is mandatory now that
  `InvoiceAlreadyPaid` exists in both files — a bare `except InvoiceAlreadyPaid:`
  binds the view's own class, which the service never raises.
- **A `get()` *before* the `create()` is the check-then-write bug.** Two
  concurrent requests both `SELECT` nothing and one still dies on the constraint,
  so the `except` branch is needed regardless — and the pre-`get` makes the
  sequential path look correct while the concurrent path stays untested. React to
  the `IntegrityError`; do not try to predict it.
- **The `get()` inside `except IntegrityError` only works in autocommit —
  FIXED 2026-08-15, keep reading for the mechanism.** Postgres marks the whole
  transaction aborted on any `IntegrityError` and refuses every later statement
  with `current transaction is aborted, commands ignored until end of
  transaction block`. It survived in production only because autocommit makes
  each statement its own transaction. `django.test.TestCase` wraps each test
  method in one `atomic()` block, so the first replay test raised
  `TransactionManagementError`. The claim `INSERT` is now inside its own
  `with transaction.atomic():` (`services.py:166-170`) with the `except`
  **outside** it: the inner block emits a `SAVEPOINT`, the exception leaving it
  emits `ROLLBACK TO SAVEPOINT`, and the transaction is usable again. Only two
  statements are accepted inside an aborted transaction — `ROLLBACK` and
  `ROLLBACK TO SAVEPOINT`. **The `except` outside the `with` is load-bearing**:
  the rewind is emitted by `atomic.__exit__`, so catching inside means it never
  fires.
- **`aggregate()` returns a dict, and `None` rather than `0` on an empty
  match.** Two separate bugs in one line. Forgetting `['amount__sum']` compares
  two *dicts*, which can pass for entirely the wrong reason. And after a payment
  there are no `OPEN` invoices, so the outstanding aggregate is `None`, and
  `None == Decimal('0')` is False. Use `Coalesce(Sum(...), Decimal('0'))` or an
  explicit `is None` test — **not `or Decimal('0')`**, because `Decimal('0.00')`
  is falsy, the same mechanism as the `Plan.clean()` bug below.
- **`sum` is not `Sum`.** `aggregate(sum('amount'))` calls Python's builtin,
  which iterates the string and dies on `unsupported operand type(s) for +:
  'int' and 'str'` two frames from anything meaningful.
- **Values captured before a write do not update after it.** A test that
  computes its numbers, POSTs, then asserts on the captured numbers is asserting
  about the pre-write state. It stays green if the POST is deleted entirely.
  Recompute after the write.
- **A subclass `setUp` replaces the parent's unless it calls `super().setUp()`,
  silently.** A class listing a fixture mixin *and* defining its own `setUp`
  runs only its own. The suite stays green if the override happens to build the
  same fixtures, so the refactor looks done and is not. Related: mixins go
  **first** in the bases list, or `unittest.TestCase`'s do-nothing `setUp` wins
  and every test dies on a missing attribute.
- **`INSTALLED_APPS` takes the module name, not the pip package name.**
  `'drf-spectacular-sidecar'` with hyphens is `ModuleNotFoundError` at startup
  and the container exits; `requirements.txt` wants the hyphens and
  `INSTALLED_APPS` wants the underscores. Hit twice in one session. A container
  that vanishes from `docker compose ps` needs `up -d`, not `restart`.
- **A branch that neither returns nor raises keeps going.** Hit twice in one
  session. First: the `COMPLETED` arm was left empty, so a retry fell out of the
  `except` block into the status check and the gateway — reproduced live on
  invoice `79` (`OPEN` invoice, `COMPLETED` key), `HTTP 200`, `inv79: PAID`,
  2 ledger rows, a second charge with no error anywhere. Second: the decline
  branch, still live. **Every path out of the collision block must `return` or
  `raise`.**
- **Copy-paste in the body dict is invisible.** `'period_start':
  invoice.period_end.isoformat()` stores a plausible date in the wrong field and
  no test currently looks at it.
- **A crash after the gateway cannot be undone.** The `Tenant`-not-serializable
  500 happened after `mock_payment_gateway` returned `succeeded`, so the
  `atomic()` block rolled back and left no invoice update and no ledger rows —
  while a real gateway would have taken the money. Not a code bug; it is the
  reason the ledger writes are in one transaction, and the reason the claim is
  committed before the gateway rather than after.
- **Unpacking a dict gives you the keys, not the values.** The decline returned
  one dict and the view did `invoice_dict, invoice_dict_status = ...`. A 2-key
  dict unpacks happily into two names, so `invoice_dict` became `'detail'` and
  the status became `'code'`. The failure surfaced two frames away as
  `ValueError: invalid literal for int() with base 10: 'code'` from
  `Response(status=...)`. Hit **three times** in one session as the body shape
  changed — `'declined'`, then `'code'`, then `'detail'`. CLAUDE.md predicted
  exactly this in session 2 ("a 2-key body would bind key *names* and look
  plausible"); it still landed three times.
- **A bare `return` in a branch the caller unpacks.** The first version of the
  decline `return` fixed the ledger bug and created a new 500 — `None` cannot be
  unpacked into two names. Adding the `return` is half the fix; the other half is
  returning the same *shape* every other path returns.
- **Storing a different body than you return.** At one point the decline stored
  `{'detils': 'declined', 'code': 402}` and returned
  `{'detail': 'payment declined', 'code': 'insufficient_funds'}`. Both requests
  succeed, both look right in isolation, and the replay silently hands back a
  different receipt than the original. This is the one bug in the list that a
  green test suite would not catch unless the test compares the two responses to
  each other. **One dict per path — build it, store it, return it.**
- **A `SyntaxError` at import kills `runserver` and the container still reports
  healthy.** An indentation slip gave
  `File "/app/billing/services.py", line 147 / SyntaxError: 'return' outside
  function`, the reloader child died, and it **did not come back when the file
  was fixed**. Every curl returned `HTTP 000` (connection refused) while
  `docker compose ps` still said `Up 2 hours` and the port was still published.
  `000` plus a healthy-looking `ps` means **`docker compose restart web`**, not a
  bad request. Sibling of the stale-`COPY` and stale-bytecode traps already in
  this file: the container being up is not the same as the app being up.

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

**Sum-to-zero proves the pairs are well-formed. It never proves they should
exist.** Four separate bugs have passed it: a negative-fee plan, a stale-window
invoice, an overlapping-window invoice, and a concurrent double-charge (two
correct pairs, `CASH 60.00` on a `30.00` invoice, total `0.00`). A test asserting
only sum-to-zero goes green on all four. The invariant that catches three of them
is `sum(ACCOUNTS_RECEIVABLE)` equalling the tenant's outstanding `OPEN` invoices;
the fourth needs a row count.

Which movement writes which pair:
- **Invoicing** — `ACCOUNTS_RECEIVABLE +amount` / `REVENUE -amount`. You earned
  it, you are owed it.
- **Payment** — `CASH +amount` / `ACCOUNTS_RECEIVABLE -amount`. The same money
  moves from owed to held; AR nets to zero and the revenue stays booked.

Reusing `REVENUE` on the payment instead of `CASH` sums to zero and passes both
invariants, but cancels the revenue — the books then say you earned nothing, hold
nothing, and are owed nothing after a customer paid.

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

The Phase 3 deliverable is the two curl commands and their output, pasted in
`docs/journal/phase-3.md`. They are the demo.

**As of 2026-08-15 they are no longer the only proof.**
`IdempotentPaymentTests.test_replay_returns_identical_response` asserts the same
thing in code — two POSTs with one key, `response.content` compared as **bytes**
(not `.data`, whose dict equality passes even when the two responses serialize
differently), a `LedgerEntry` delta of exactly 2, and one `IdempotencyKey` row.
Verified red by making the `COMPLETED` replay arm at `services.py:178` return a
different body: both statuses stayed `200` while the contents diverged, which is
precisely the stored-vs-returned bug this file already warns about. That is the
assertion to keep if any get dropped.

## Testing conventions

Wrap each expected-failure insert in `with transaction.atomic():`. Postgres
poisons the surrounding transaction after an `IntegrityError`, so an unwrapped
assertion breaks every following query in the same test. This differs from
SQLite, so tests must be run against Postgres to be trustworthy.

The three required tests (Phase 5) map directly to Phase 1 shell checks:
double-pay charges once, tenant A cannot read or affect tenant B, ledger sums
to zero. **All three are done as of 2026-08-15** — `UsageIsolationTests`
(2026-08-14), `IdempotentPaymentTests` (`0965809`) and `LedgerInvariantTests`
(`98b9ca5`), all in `billing/tests.py`, sharing `BillingFixtureMixin` where the
fixture story matches.

The ledger test asserts **both** forms, before the payment and again after it:
`sum(all) == 0`, and `sum(ACCOUNTS_RECEIVABLE) == outstanding OPEN`. Proven
necessary rather than assumed — duplicating the invoicing pair in
`generate_invoice` leaves sum-to-zero green (`+20 -20 +20 -20`) while the second
assertion reads `Decimal('40.00') != Decimal('20.00')`.

`billing/tests.py` is still a single module. Splitting it into a package was the
plan once the ledger test landed; at four tests and ~150 lines it has not earned
the split yet.

Settled while writing that first test, and they apply to every test after it:

- **`rest_framework.test.APITestCase`, not `django.test.TestCase`.** The DRF
  class gives `self.client` an `APIClient` with `.credentials()`, which is how
  the `Authorization: Api-Key <key>` header gets set. **`APIRequestFactory`
  builds a request object and never sends it** — fixtures written against it
  resolve no URL and write no row.
- **Build fixtures with the ORM, never over HTTP.** Creating them through the
  endpoints makes every test depend on create-tenant, create-plan and
  create-subscription, so an unrelated break sends the wrong test red.
- **A method not named `test_*` is silently not collected.** The run reports
  `Ran 0 tests / OK` — a pass that proves nothing. Same failure mode as a test
  with no assertions.
- **Prove a new test can fail before trusting it.** Mutate the line under test,
  confirm red, revert. Four real bugs in this project have passed a green
  invariant; a test that has only ever been green is not yet evidence.
- **Use `reverse('name')`, not a literal path.** A missing trailing slash is an
  `APPEND_SLASH` 301, not a 404, and the test client does not follow redirects —
  the response has no `.data` at all. A renamed path silently returns 0 rows,
  which reads as perfect isolation.
- **Assert `status_code` before touching `response.data`.** An error body is a
  dict, and iterating a dict yields its keys, so the real failure surfaces as a
  `TypeError` two lines from its cause.

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
   `docs/journal/phase-2.md` for what `generate_invoice` does. Built and committed as
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
7. ~~**How a pay request names its invoice, and what the fingerprint covers.**~~
   **DECIDED 2026-08-09: pk in the URL, hash over pk + body.**
   `POST billing/invoices/<int:pk>/pay/` with `{"amount": "..."}`, scoped by
   `tenant=request.tenant` so another tenant's invoice is a 404 identical to a
   nonexistent one. `request_hash` is sha256 over
   `{'invoice': <pk>, **request.data}` with `sort_keys=True`.

   Rejected: **no id, pay the tenant's oldest OPEN invoice.** Preferred until the
   `base_fee` collision surfaced — with `amount` as the only client-supplied
   field, every zero-usage month bills exactly `20.00`, so two different payments
   fingerprint identically and a reused key silently swallows the second. Also
   rejected the repair of folding the server-resolved id into the hash: after 17
   is paid the resolution moves to 18, so a legitimate retry recomputes a
   different hash and is rejected as a client error. **Hash only client-supplied
   input** — server state moves between the original request and the retry.

   Full reasoning, including why the pk is not in `request.data`, in
   `docs/journal/phase-3.md`.
8. ~~**What happens to a tenant that does not pay.**~~ **DECIDED 2026-08-09:
   `PAST_DUE` at 7 days, `CANCELED` after — cancel rather than accrue.** Both
   flips need a scheduler, so this is Phase 4 work. `PAST_DUE` already exists in
   `SUBSCRIPTIONS_CHOICES` and has been unused since Phase 1.

   Chosen because a suspended tenant otherwise keeps accruing `base_fee` for
   months it was locked out of — `generate_invoice` and the usage endpoint both
   resolve the subscription with `get(status='ACTIVE')`, so `PAST_DUE` already
   blocks recording usage, and six months suspended would still owe `120.00` for
   zero access. `CANCELED` is not `ACTIVE`, so `NoActiveSubscription` is raised
   and accrual stops.

   Rejected: skipping `base_fee` for fully-suspended periods (needs a
   "suspended since" field `Subscription` does not have); jumping
   `current_period_start` past the gap on payment (loses usage in the gap, though
   there is none while locked out); accruing anyway as debt (defensible for
   contract billing, wrong for self-serve).

## Known open items

Carried out of Phase 1:
- ~~Dockerfile `CMD` chains `migrate && runserver` via `sh -c`~~ — **closed
  2026-08-15.** `ENTRYPOINT ["/app/entrypoint.sh"]` plus
  `CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000",
  "--access-logfile", "-"]`, both in JSON array form so Docker adds no shell.
  The script ends on `exec "$@"`, so gunicorn is PID 1 and `SIGTERM` lands —
  `docker compose stop web` returns in 0.925s. `migrate` is now its own one-shot
  Compose service with `web` gating on `service_completed_successfully`. That was
  named in `docs/journal/phase-7.md` as Phase 7's blocker, so **Phase 7 is no
  longer blocked by it.**
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

Deploy prerequisites — **all closed 2026-08-15**, kept for the mechanisms:
- ~~`config/settings.py` is stock `startproject`~~ — `SECRET_KEY`, `DEBUG` and
  `ALLOWED_HOSTS` now read from the environment. `DEBUG` is a **string
  comparison**, never a cast; `ALLOWED_HOSTS` filters empties before splitting.
- ~~A fresh clone has no database config~~ — `.env.example` carries all eight
  keys and the README documents them. `DB_HOST` and `CELERY_BROKER_URL` are
  deliberately **not** in `.env`: `docker-compose.yml` sets them directly,
  because they name services on the Compose network.
- ~~Static files~~ — whitenoise serves them, its middleware **immediately after**
  `SecurityMiddleware`, `STATIC_ROOT = /app/staticfiles`, `collectstatic` run
  from `entrypoint.sh` before gunicorn binds. **WhiteNoise indexes `STATIC_ROOT`
  once at startup**, so collecting after boot changes nothing until a restart.
- ~~Empty `ALLOWED_HOSTS`~~ — set from the environment, and the deployed server
  lists its public IP. It bit three times before that: `/api/schema/`
  unreachable by container hostname (`400`, no explanation), `APIClient` outside
  the test runner rejected with `DisallowedHost: Invalid HTTP_HOST header:
  'testserver'`, and the public IP rejected the same way. The Django test
  *runner* whitelists `testserver`; a plain script does not.

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
- ~~No `__str__` on any model~~ — **fully closed 2026-08-08.** `13f695e` did
  `Tenant`, `Plan`, `Subscription`; `be5aff4` added `Invoice`,
  `UsageEvent`, `LedgerEntry`, `IdempotencyKey` and put the row id into the
  first three. See the "`__str__` on every model" subsection in
  `docs/journal/phase-2.md`.
- ~~`billing/admin.py` registers nothing~~ — fixed in `c3086cb`, all seven
  registered.
- `Plan.clean()` guards with `if self.base_fee and self.unit_fee:`. `Decimal('0')`
  is falsy, so a free or flat-rate plan skips the whole block. Harmless right now
  (nothing in the block would reject zero) but it silently disables any check
  added there later. Guard on `is not None`.
- Consider migrating existing choice sets to `TextChoices`.
- ~~`unique_open_invoice_per_tenant` has no guard in `generate_invoice`~~ —
  **closed 2026-08-13.** Constraint applied as `0010`, guard and constraint-name
  discriminator built and verified live. Ignore the guard-placement rationale in
  `docs/journal/phase-3.md`; it was wrong and is corrected in "Phase 4 — the two
  prerequisites", item 2.
- **The bare `except IntegrityError` in `SubscriptionsCreateView.perform_create`
  is still bare** — oldest unstarted Phase 2 item, sixth session. The
  discriminator it was waiting on now exists in `services.py`, but do not
  copy-paste it: see "What is actually wrong with the subscription view" at the
  top of this file for why the payoff is smaller than the journal implies and
  why the `.diag` walk belongs in one shared helper.
- **`void_invoice` has no code path at all.** Acme's `18` and `19` were voided by
  hand in the shell on 2026-08-12; the service that would do it properly is still
  unwritten. Requirements are in "Phase 4 — the two prerequisites", item 2.
- `InvoicesPay` still does its own `Invoice.objects.get(id=pk, tenant=tenant)`
  before calling `pay_invoice`, which repeats the service's lookup and makes
  `except services.InvoiceNotFound` unreachable. Harmless (both scope by tenant)
  but it is duplicated logic in two files.
- `LedgerEntry.description` is still never set — both invoicing and payment write
  `''`. Now that there are two kinds of pair in the table, a readable line
  (`"payment for invoice 42"` vs the billing window) is worth more than it was.
- **`SPECTACULAR_SETTINGS` has no `TITLE`, `DESCRIPTION` or `VERSION`**, so the
  docs render as "Swagger" / `0.0.0`. Cosmetic, but it is the first thing anyone
  opening the deployed URL sees.
- **Only `pay` documents its `401`.** The other five endpoints sit behind the
  same authenticator and list no `401` response. The security scheme is declared
  globally so a reader can infer it; add `@extend_schema(responses={401: ...})`
  per view if the docs should be uniformly complete.
- **`RequestHashDiffers` is `422`, not `409`** (`exceptions.py:59`) — recorded
  because the first draft of the Swagger `responses` dict put the
  different-body case under 409. `409` on `pay` means "invoice is not OPEN" or
  "payment already processing". Reaching `422` at all requires an **extra field**
  in the body: with `amount` as the only real field, a different amount trips
  `AmountMismatch` (400) at `services.py:162` before the key is claimed at `:166`.
- **Orphaned `PROCESSING` idempotency rows.** Any raise after the claim commits
  strands a row that can never complete, and the `PROCESSING` arm then answers
  409 "still in flight" forever. See "Orphaned `PROCESSING` rows" in
  `docs/journal/phase-3.md`. Decide before Phase 5 tests that arm.

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

The API docs live at `http://localhost:8000/api/docs/` (UI) and
`http://localhost:8000/api/schema/` (raw OpenAPI). Authorize with the **whole**
header value — `Api-Key <key>`, prefix included — because the scheme is
`apiKey` on `Authorization`, so Swagger UI sends verbatim what is typed.

Check the schema without a browser:

```
docker compose exec web python manage.py spectacular --file /dev/null
```

Silence means zero warnings; it prints a summary only when there are warnings or
errors. It introspects `urlpatterns` directly, so it passes even when the docs
routes themselves are missing from `config/urls.py` — a clean run there is not
evidence that `/api/docs/` serves.

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

A requirements change needs `docker compose up -d --build` — a restart does
nothing — and the only check that counts is importing it **inside** the
container.

Generate deps with `.venv/bin/pip freeze`, **never** bare `pip freeze`. A bare
run with the venv deactivated once wrote the host's Ubuntu system packages into
`requirements.txt` and broke the image build on `bcc==0.29.1`.
