# Phase 4 — multi-tenancy + worker

Covers the worker half: the service loop, the management command, and the
Celery + Redis + beat stack, then the isolation test and the test scaffolding
it landed with. Both halves are done; Phase 4's "Done when" is met.

## Where the loop lives, and why not in the command

`generate_invoice_to_all()` is in `billing/services.py`, next to
`generate_invoice`. Three callers eventually want it — the management command,
the Celery task, and a test — so it is a plain function with no `BaseCommand`
and no Celery import. Same rule as decision 2 in `CLAUDE.md`: the service layer
takes a `Tenant` and plain values and knows nothing about HTTP or CLI.

Consequence that shaped the signature: **the service does not print.** It
returns a list of `(tenant_id, outcome, detail)` tuples and the caller formats
them. The command writes them with `self.stdout.write`; the Celery task writes
them to a logger. If the loop printed, the task's output would go to the
command's stdout, which under Celery is nowhere.

### The two-arm except, and why `BillingError` is the whole contract

```python
except BillingError as e:   -> ('skipped', ...)
except Exception as e:      -> ('errored', ...)
```

`except X` matches `X` and every subclass, tested with `isinstance`. Every
expected billing failure — `PeriodNotEnded`, `OpenInvoiceNotPaid`,
`NoActiveSubscription`, `InvoiceAlreadyExists` — subclasses `BillingError`, so
the first arm catches them all and the loop continues. A `TypeError` is not a
`BillingError`, so it falls through to the second.

**Clause order is load-bearing.** `BillingError` is itself an `Exception`, so
`except Exception` above it would swallow every skip and the specific arm would
be dead code. Specific first.

**Nothing automatic decides `PeriodNotEnded` is benign.** It is benign because
someone wrote `class PeriodNotEnded(BillingError)`. That base class *is* the
"caller may skip this tenant" contract. Two consequences:

- A new expected failure raised as a bare `ValueError` gets filed under
  `errored`. New expected failures must subclass `BillingError`.
- `Subscription.DoesNotExist` is not a `BillingError` and would be `errored` —
  it is not, only because `generate_invoice` catches it and re-raises
  `NoActiveSubscription`. That translation is what keeps the contract honest.

What legitimately lands in `errored` today: the bare `raise` in the
`except IntegrityError` discriminator, i.e. a constraint nobody anticipated.
Correct — that is a bug, not a skip.

### No `atomic()` around the loop

Each `generate_invoice` owns its own transaction. One block wrapping the whole
loop would roll back the invoices already written for tenants 1-39 when tenant
40 fails, and worse, the `IntegrityError` poisons the connection so every
tenant after it dies with `current transaction is aborted`. Each tenant is its
own unit of work.

### Which tenants

`Tenant.objects.filter(is_active = "True")`. The field existed since Phase 1 and
nothing had ever read it. Checked live: Django's `BooleanField` coerces the
string `'True'`, so it returns the same rows as `True` — not a bug, but there is
no reason to lean on the coercion.

## The management command

`billing/management/commands/generate_invoices.py`. Three files, two of them
empty:

```
billing/management/__init__.py            empty
billing/management/commands/__init__.py   empty
billing/management/commands/generate_invoices.py
```

Traps hit, in order:

- **The code went into `commands/__init__.py` first.** Django's loader lists the
  *modules inside* `management/commands/` and skips names starting with `_`, so
  `__init__.py` is the package marker and is never a command. Nothing there is
  reachable by `manage.py`, and nothing errors.
- **`def handlr`.** Django calls `handle`; the typo left
  `BaseCommand.handle`'s `NotImplementedError` in place.
- **`generated_invoices[outcome]`** after already unpacking `outcome` in the
  `for` — indexing a list with a string.
- **`raise CommandError(self.stdout.write(...))`.** `write()` returns `None`, so
  the error message printed as `None` and the text went to stdout while the
  error went to stderr.
- **Only errored tenants got a line at first.** A clean run printed "starting"
  and "finished" and nothing else.

`CommandError` is what makes the exit status non-zero. Without it a run where
every tenant blew up is indistinguishable from a clean one to a scheduler, CI,
or `docker compose`. Only `errored` triggers it; `skipped` is a normal run.

### Verified live, 2026-08-13

Baseline `4 tenants / 1 plan / 5 subs / 4 invoices / 12 ledger` before and after
every run below.

Real run through `call_command`, rolled back — all four tenants skip, nothing
written, exit clean:

```
tenant: 14, outcome: skipped, tenant 14 has an open invoice 20
tenant: 15, outcome: skipped, Cannot make a bill for 2026-09-01 ... period has not ended
tenant: 13, outcome: skipped, tenant 13 has an open invoice 17
tenant: 20, outcome: skipped, Cannot make a bill for 2026-09-01 ... period has not ended
```

Both guard messages name the invoice — last session's `OpenInvoiceNotPaid` doing
its job through the real command path.

Forced `billed` and `errored`, since nothing in the dev database is due. Two
throwaway tenants with ended periods, and `mock.patch.object(services,
'generate_invoice')` raising `TypeError` for one of them:

```
tenant: 48, outcome: billed,  details: 102
tenant: 49, outcome: errored, details: not billed, billing error: simulated worker bug
CommandError: 1 tenants errored
```

Invoice count `4 -> 5` and ledger `12 -> 14` during the run — one invoice, one
AR/REVENUE pair. `TypeError` filed as `errored`, not `skipped`, on a real
non-`BillingError`.

**Not proven by that run:** continuation *past* an error. Tenant 49 happened to
be last in the loop, so nothing came after it to be skipped. The code is right;
the run does not demonstrate it.

## Celery, Redis, beat

**Redis first, before any Celery.** A Celery worker with no broker does not
fail — it logs `Cannot connect to ... Connection refused. Trying again in 2.00
seconds` forever. So a Celery bug and a missing broker look nearly identical.
Redis is one compose service and zero Python, and `redis-cli ping` proves it
alone; after that any failure is Celery's.

Chosen over a compose service looping `sleep` + the management command. The loop
container is trivial and would have worked for the demo, but Phase 7 puts two
web replicas behind nginx and the loop container would have to be deleted then
anyway.

### The pieces, and what each one actually does

| File | Job |
|---|---|
| `config/celery.py` | sets `DJANGO_SETTINGS_MODULE`, builds the app, reads config from Django settings, autodiscovers tasks |
| `config/__init__.py` | imports that app so `shared_task` binds to it |
| `billing/tasks.py` | `@shared_task` wrapping the service loop, logging one line per tenant |
| `config/settings.py` | `CELERY_BROKER_URL`, `CELERY_BEAT_SCHEDULE` |
| `docker-compose.yml` | `redis`, `celery-worker`, `celery-beat` |

**`namespace='CELERY'`** is why `CELERY_BROKER_URL` in Django settings becomes
Celery's `broker_url`: it scans settings for the `CELERY_` prefix, strips it and
lowercases the rest. Drop the prefix and Celery never sees the key.

**Why the app import must be in `config/__init__.py`.** That file runs whenever
the `config` package is imported, and Django imports it at startup because
`DJANGO_SETTINGS_MODULE=config.settings` imports `config` first. It is the one
place guaranteed to run before app code. `@shared_task` does not attach to a
named app — it attaches to whatever the *current* app is when the decorator
runs, and creating `Celery('config')` sets that. Without the import,
`billing/tasks.py` binds to a default app with the default broker
(`amqp://guest@localhost//`) and `.delay()` from a view or the shell queues into
nothing. The worker itself is fine either way, since `celery -A config` imports
`config.celery` directly — this only affects the Django side.

**Broker URL, in compose, not `.env`.** `CELERY_BROKER_URL=redis://redis:6379/0`
is set literally on `web`, `celery-worker` and `celery-beat`, the same call
already made for `DB_HOST=db`: the hostname is a fact about the compose network,
not a per-developer secret. A `CELERY_BROKER_URL` line was briefly added to
`.env` too and removed — `settings.py` calls `load_dotenv('.env')`, which does
**not** overwrite variables that already exist, so the compose value wins and
the `.env` line was dead. By the `DB_HOST` pattern it would have to be
`redis://127.0.0.1:6379/0` for host-side runs, and Redis publishes no host port,
so it could never work either.

**No result backend.** Nothing asks whether a task finished. The worker banner
reads `results: disabled://` and the task's return value is discarded — which is
exactly why the task logs instead of returning.

### Traps hit here

- **A garbage broker URL is accepted silently.** `CELERY_BROKER_URL` was briefly
  set to the literal string `CELERY_BROKER_URL` (the name written twice). Celery
  parsed it as a *hostname* and fell back to the default transport:
  `broker_url: 'CELERY_BROKER_URL'`, `transport: py-amqp`. The worker would have
  retried RabbitMQ on a host by that name forever. Print
  `celery_app.conf.broker_url` and `.connection().transport.driver_name` after
  any broker change.
- **The file must be `tasks.py`.** It was `task.py` first.
  `autodiscover_tasks()` imports exactly `<app>.tasks`, so the module was never
  imported, the task never registered, and nothing errored.
- **The beat schedule names the full dotted path to the function.** Wrong twice:
  `billing.generate_invoices_task`, then `billing.tasks`. Neither is registered,
  and beat publishes happily either way — the worker answers
  `Received unregistered task of type ...` once per tick while beat's own log
  looks healthy. Check with `entry['task'] in set(celery_app.tasks)` after
  `celery_app.loader.import_default_modules()`.
- **A duplicate YAML key wins silently.** `celery-beat` briefly had two
  `command:` lines; YAML keeps the last, so the command became
  `--schedule=/tmp/celerybeat-schedule` on its own.
- **`command:` must be set on worker and beat.** The Dockerfile `CMD` is
  `migrate && runserver`. Without an override all three containers run `migrate`
  against one database on boot and then start web servers, and no Celery runs at
  all. Related to the Phase 7 `migrate`-in-`CMD` blocker.
- **Beat writes `celerybeat-schedule` into its working directory**, which the
  bind mount puts in the repo, root-owned, along with `-shm` and `-wal`. Fixed
  with `--schedule=/tmp/celerybeat-schedule`.
- **`depends_on` direction.** Redis depends on nothing; worker and beat depend on
  `redis` and `db` at `condition: service_healthy`. Making Redis depend on `web`
  produces `cycle found in dependencies` as soon as anything depends on Redis.
- **A `SyntaxError` in `config/__init__.py` killed the `web` container** and it
  did not come back when the file was fixed — `docker compose ps` showed
  `exited`. Same family as the traps already in `CLAUDE.md`.

### Verified end to end, 2026-08-13

Five services up, beat and worker healthy, one tick observed:

```
beat    Scheduler: Sending due task generate-invoices-every-minute
                   (billing.tasks.generate_invoices_task)
worker  Task billing.tasks.generate_invoices_task[4c68cbd9...] received
        tenant: 14, outcome: skipped, tenant 14 has an open invoice 20
        tenant: 15, outcome: skipped, ... period has not ended
        tenant: 13, outcome: skipped, tenant 13 has an open invoice 17
        tenant: 20, outcome: skipped, ... period has not ended
        succeeded in 0.103s: None
```

Worker banner confirms `transport: redis://redis:6379/0`,
`results: disabled://`, and `billing.tasks.generate_invoices_task` under
`[tasks]`.

`logger.info` lines only appear because the worker runs at `-l info`. At the
default level a fully successful run would look silent — only the `logger.error`
lines would show.

## The beat schedule, corrected 2026-08-14

`crontab(minute=5)` was set at the previous session's close believing it meant
"every five minutes". It means **once an hour, at :05**. Now
`crontab(minute='*/5')` at `config/settings.py:146`.

**Deliberately not the production value.** `crontab(hour=0, minute=0)` — daily
at midnight UTC — is what this becomes when the project is done; each
subscription carries its own window, so the worker only has to check often
enough to catch a window the day it ends. `*/5` stays until then because a
five-minute tick is observable in a demo and a daily one is not.

**A settings change does not reach a running beat process.** After the edit,
`docker compose restart celery-beat`. Confirmed from the log rather than from
the source:

```
beat: Starting...                          16:52:59
Scheduler: Sending due task generate-invoices-every-minute   16:55:00
```

`16:55:00` is a `*/5` boundary. `minute=5` would have sat silent until `17:05`.
The distinction is only visible in *when it fires* — both spellings are valid
crontabs, both log an identical healthy banner. Fourth silently-accepted config
string in this project; the rule from the Celery traps holds — **verify what the
system resolved it to, do not read the string back**.

## The isolation test, 2026-08-14

The project's first test, so it landed the scaffolding with it. Lives in
`billing/tests.py` — a single file, not a package, until Phase 5 adds the other
two required tests.

`UsageIsolationTests(APITestCase)`, from `rest_framework.test`, **not**
`django.test.TestCase`. The DRF class gives `self.client` an `APIClient`, which
has `.credentials()` for setting headers across requests. The proof:

```python
self.client.credentials(HTTP_AUTHORIZATION = f'API-Key {tenant_A.api_key}')
response = self.client.get('/billing/usage/')
self.assertEqual(response.status_code, 200)
return_ids = { ... event['id'] ... }
self.assertEqual(return_ids, {A_first.id, A_second.id})
self.assertNotIn(B_first.id, return_ids)
```

The line under test is one line — `billing/views.py:59`,
`UsageEvent.objects.filter(subscription__tenant = self.request.tenant)`.
`UsageEvent` reaches a tenant only through `subscription.tenant`; there is no
`tenant` FK on the model, which is why the filter spans a join and why it is
worth a test at all.

### Fixtures are ORM writes, not API calls

Three tenants' worth of setup — one shared `Plan`, `Tenant` A and B, one
`ACTIVE` `Subscription` each, two `UsageEvent` rows for A and one for B — all
via `.objects.create()`.

**Not built over HTTP on purpose.** Creating them through the endpoints makes
this test depend on create-tenant, create-plan and create-subscription all
working; when create-plan breaks, the *isolation* test goes red and points at
the wrong file. A test should fail for one reason.

Two tenants each holding an `ACTIVE` subscription is legal —
`unique_active_subscription_per_tenant` is scoped per tenant — and it is exactly
the shape the test needs.

`api_key` is never set; `default=make_key` generates one per row and the test
reads it back off the instance.

### Assertion shape

`assertEqual` on **sets**, not a count and not a containment check. Equality
catches both failure directions in one line: `{A1, A2, B1} != {A1, A2}` when B
leaks, and `{A1} != {A1, A2}` when the filter is too aggressive. A subset check
or an `assertIn` only catches the second. `len(...) == 2` catches neither
precisely — it also passes if A's first event came back twice.

Ids are read off the fixture objects, never hardcoded: Postgres sequences do not
reset between tests.

### Traps hit here

- **A test method not named `test_*` is never collected**, and the run reports
  `Ran 1 test`-less `Ran 0 tests / OK`. Green, proving nothing. Hit as
  `IsolationTest`.
- **`APIRequestFactory` builds a request and never sends it.** Fixtures written
  as `factory.post('/tenants/', {...})` resolve no URL, run no view and write no
  row. It exists for calling a view callable directly. `self.client` is what
  routes through the URLconf.
- **FK fields take instances, not ids.** `tenant = tenant_A.id` raises
  `ValueError: Cannot assign "1": "Subscription.tenant" must be a "Tenant"
  instance.` Either the instance, or `tenant_id = tenant_A.id`.
- **`UsageEvent.occurred_at` and both `Subscription.current_period_*` have no
  default.** Omitting them is a not-null `IntegrityError`, not a validation
  message. `USE_TZ = True`, so they must be aware datetimes.
- **A missing trailing slash is a 301, not a 404.** `'/billing/usage'` gets
  `APPEND_SLASH`'d by `CommonMiddleware`, the test client does not follow
  redirects by default, and a redirect response has no `.data` —
  `AttributeError: 'HttpResponsePermanentRedirect' object has no attribute
  'data'`, two concepts away from the actual typo. `reverse('usage')` makes it
  unrepresentable.
- **Accumulate before asserting.** `return_ids = event['id']` inside the loop is
  a scalar reassigned per row, so the set comparison fails on the first
  iteration (`1 != {1, 2}`) regardless of what the view returned. The assertion
  belongs after the loop, on the whole set.
- **`assertEqual(B_first.id, return_ids)` asserts the leak.** Written that way
  it goes green exactly when isolation is broken. The negative is
  `assertNotIn`.
- **Assert `status_code` before touching `response.data`.** On a 401 the body is
  `{'detail': ...}`, and iterating a dict yields its keys, so `event['id']`
  surfaces as `TypeError: string indices must be integers` with nothing pointing
  at auth.

### Two facts the test quietly depends on

- **`Api-Key` matching is case-insensitive.** `billing/authenticate.py:14`
  compares `auth[0].lower()` against `self.keyword.lower().encode()`, so
  `API-Key` in the test works identically. House style is still `Api-Key`.
- **No pagination is configured.** `REST_FRAMEWORK` sets no
  `DEFAULT_PAGINATION_CLASS`, so `response.data` is a plain list. Adding
  pagination in Phase 5 turns it into `{'results': [...]}` and breaks this loop
  — the fix is one line, but the failure will read as an isolation failure.

### Verified 2026-08-14, both directions

Green is not evidence on its own — four bugs in this project have passed a green
invariant. So the filter was mutated and the test rerun:

```
billing/views.py:59  ->  models.UsageEvent.objects.all()

FAIL: test_isolation (billing.tests.UsageIsolationTests.test_isolation)
AssertionError: Items in the first set but not the second:
3
```

`3` is tenant B's usage event id. Reverted, rerun, `Ran 1 test ... OK`. The test
detects a real cross-tenant leak and passes only because the filter is there.

**What it does not cover**, and none of it blocks Phase 4: write isolation (a
POST under A's key attaching to A's subscription — free, since
`UsageEventSerializer.subscription` is read-only and `perform_create` resolves
it from `request.tenant`), the 401 paths (no header, unknown key, inactive
tenant), and every endpoint other than `GET billing/usage/`.

## Rules that outlive this phase

- **Exactly one beat container, ever.** The worker scales; beat does not. Two
  beats is two billing runs per tick. The constraints would reject the second
  invoice, but the correct answer is not to fire it.
- **If beat double-fires or a task retries, nothing double-bills.**
  `unique_invoice_period` and `unique_open_invoice_per_tenant` reject it at the
  database and the guard turns it into a clean skip. The safety net was built
  before the thing that needs it, deliberately.

## Not started

1. **The production schedule.** `crontab(minute='*/5')` is a demo value, kept
   deliberately. Swap to `crontab(hour=0, minute=0)` when the project is done.
   `TIME_ZONE = 'UTC'` and `USE_TZ = True` are already set and Celery defaults to
   UTC, so no `CELERY_TIMEZONE` is needed.
2. **Continuation past an errored tenant** is untested — see above.
3. **The `errored` policy in the Celery task.** The command raises
   `CommandError`; the task only logs at error level. Whether a broken tenant
   should mark the whole task failed was never decided. There are no retries
   configured, so raising would mostly just colour the log.
