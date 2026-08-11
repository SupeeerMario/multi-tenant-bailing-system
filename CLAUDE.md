# CLAUDE.md

Guidance for Claude when working on this repository.

## What this project is

A multi-tenant billing API built in Django. It charges companies monthly, keeps
each company's data isolated, and never double-charges. The full spec is a
six-phase build (data model + Docker, core endpoints, idempotent payments,
multi-tenancy + worker, tests + docs, deploy).

## Start here next session

Updated 2026-08-10, end of the third Phase 3 session. **Phase 3 is done, its
"Done when" is met, and everything is committed and pushed.** Step 5b landed —
`pay_invoice` returns `(body, status)` on every path, a decline is stored and
replayed instead of raised, and both paths return `idem_key_object.response_body`
after `refresh_from_db()` so a replay is byte-identical. A second commit made the
already-paid `409` a stored answer too. The two-curl proof is pasted into this
file — see "The two-curl proof" in `docs/journal/phase-3.md`.

**Next action: Phase 4, but two prerequisites first — the author chose to do
both before the worker.** See "Phase 4 — the two prerequisites" below for the
decision each one needs. Neither is started.

Then Phase 4 proper, in this order:

1. **The worker as a management command, no scheduler.**
   `manage.py generate_invoices` loops every tenant, calls
   `services.generate_invoice(tenant)`, catches `BillingError` and continues.
   That loop is already designed for: the service raises rather than returns, the
   window comes from the subscription, and a tenant that is not due raises
   `PeriodNotEnded` and is skipped.
2. **Then the schedule.** Undecided: Celery + beat (heavier, real, another
   compose service) versus a loop container running the command on an interval
   (trivial, honest for a demo).
3. **Then the isolation test** — the project's **first test**, so it lands test
   infrastructure too. Target is `UsageEventListCreateView`; `GET billing/usage/`
   is the only proven read-isolation path.

**Dev database is on baseline, verified 2026-08-10 after three rounds of demo
rows were deleted** (tenants `40 KeyTest`, `41 OrphanDemo`, `42
AlreadyPaidDemo`, with their invoices `78`–`90`, ledger rows and 18
`IdempotencyKey` rows). Deletes ran in `PROTECT` order, and a pre-delete check
confirmed no row outside the throwaway tenant referenced its invoices.

Counts read **`4 tenants / 1 plan / 5 subscriptions / 6 usage events / 4
invoices / 8 ledger rows`**, ledger sum `0.00`, `0` idempotency keys. Invoices
are `17` (`30.00`), `18` (`39.44`), `19` (`20.00`) for Acme and `20` (`36.67`)
for Globex, all still `OPEN`. No demo payment ever touched real seed data,
deliberately.

The stronger invariant passes per tenant — `sum(ACCOUNTS_RECEIVABLE)` equals the
tenant's outstanding `OPEN` invoices: Acme `89.44`, Globex `36.67`, Initech and
`test name` `0`. That is the check that catches what sum-to-zero misses.

**Note this baseline is about to change by design.** Item 2 below cannot be
built while Acme has three `OPEN` invoices, so whichever way that is resolved,
these numbers move and this section must be re-read afterwards rather than
trusted.

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

Still the oldest unstarted Phase 2 item, untouched for a fifth session:
narrowing the bare `except IntegrityError` in
`SubscriptionsCreateView.perform_create`. The discriminator is already confirmed
on this stack — see item 1 under "Still not started" in
`docs/journal/phase-2.md`.

### Phase 4 — the two prerequisites

Both were raised as things that bite the moment an unattended worker exists, and
the author chose to do them **before** step 1. Neither is started; each needs a
decision before any code.

**1. Nothing checks `plan.is_active` before subscribing.** Decide what
`is_active` *means* first, because it changes where the check goes:

- **"Not available for new subscriptions"** — existing subscribers are
  grandfathered and keep being billed. The fix is then **only** in
  `SubscriptionsSerializer` and `generate_invoice` needs no check at all.
- **"Stop billing entirely"** — the worker has to check too, and that needs an
  answer for a plan deactivated mid-cycle: bill the partial period, skip
  silently, or cancel the subscription.

The first reading is the recommendation. Deactivating a price should not stop
revenue from customers already on it, and the second reading hands the worker a
silent skip path, which is the failure mode this project keeps hitting (the
stale-window invoice, the `relativedelta(month=1)` cascade, the behind-by-a-cycle
skip — none of them raised anything).

Under the first reading it is one line on `SubscriptionsSerializer`:

```python
plan = serializers.PrimaryKeyRelatedField(queryset = models.Plan.objects.filter(is_active = True))
```

An inactive plan then 400s with `Invalid pk "N" - object does not exist.`, the
same message as a nonexistent plan, which leaks nothing.

**This one cannot have the usual DB guarantee.** A Postgres `CHECK` cannot
reference another table, so no `CheckConstraint` can say "the plan I point at is
active". The validation-layering rule from the negative-money work does not fully
apply — there is a serializer layer and, under the second reading, a service
layer, and that is all. Do not go looking for the constraint later.

**2. `unique_open_invoice_per_tenant` — agreed since 2026-08-09, still unbuilt,
and blocked on data.** The constraint is two lines on `Invoice.Meta`:

```python
models.UniqueConstraint(fields = ['tenant'], condition = Q(status = 'OPEN'),
                        name = 'unique_open_invoice_per_tenant')
```

plus the matching guard at the **top of `generate_invoice`, before the
`atomic()` block** — placed there the period advance never runs, so an unpaid
invoice *pauses* the cycle instead of sliding past unbilled usage.

`AddConstraint` validates existing rows, and Acme has three `OPEN` invoices
(`17`, `18`, `19`), so it fails exactly like the `-99.00` plan did. Two ways to
clear it, and **neither is free**:

- **Pay two through the API.** Real code path, AR drops correctly, every
  invariant holds. Cost: the documented baseline moves — ledger `8` → `12` rows,
  Acme AR `89.44` → `30.00`, two invoices become `PAID`. This section has to be
  rewritten afterwards.
- **Void two.** `VOID` already exists in `INVOICES_CHOICES`, so no migration.
  But setting `status='VOID'` by hand **breaks the ledger invariant**: AR was
  booked `+39.44` and `+20.00` when those invoices were generated, and voiding
  does not unbook it, so Acme AR stays `89.44` while `OPEN` sums to `30.00`. The
  ledger is append-only, so a correct void writes a **reversing pair**
  (`ACCOUNTS_RECEIVABLE -amount` / `REVENUE +amount`) — which means writing a
  `void_invoice` service.

Paying is the cheap one. Voiding is the more useful one — `void_invoice` will be
wanted eventually and is currently the only operation in the domain with no code
path at all.

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
| 4 — Multi-tenancy + worker | Worker generates invoices on a schedule; tenant isolation proven by a test | **Unblocked** (2026-08-10) — not started |
| 5 — Tests + docs + CI | CI green on push (lint + tests + Docker build), Swagger lists every endpoint | Blocked on 4 |
| 6 — Deploy + package | Live URL, README with architecture diagram + no-double-charge proof, decision note | Blocked on 5 |
| 7 — Horizontal scale | nginx in front of 2 identical web containers, one Postgres; the same-key retry proof rerun across containers, not threads | Blocked on 6 (2026-08-09) |

## Build journal — not loaded by default

The phase-by-phase build record lives in `docs/journal/` and is **not** in
context unless you load it. Invoke the `project-journal` skill, or read the file
directly:

| File | Covers |
|---|---|
| `docs/journal/phase-2.md` | Phase 1 verification, `generate_invoice`, all five Phase 2 endpoints, negative money, zero-length periods, ordering, `__str__` |
| `docs/journal/phase-3.md` | The double-charge repro, the `Idempotency-Key` claim, the two-curl proof, the decline/TTL contract, check ordering |
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
- **The `get()` inside `except IntegrityError` only works in autocommit.** Inside
  a `transaction.atomic()` block Postgres marks the transaction aborted and every
  later query fails with `current transaction is aborted, commands ignored until
  end of transaction block`. `ATOMIC_REQUESTS` is unset so this is currently
  safe; do not wrap the claim in `atomic()` and do not enable `ATOMIC_REQUESTS`
  without revisiting it.
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
- Dockerfile `CMD` chains `migrate && runserver` via `sh -c`, so the shell is
  PID 1 and swallows `SIGTERM`. Fine for dev; Phase 6 wants a real entrypoint
  script and a WSGI server instead of `runserver`. **Upgraded from cosmetic to
  blocking by Phase 7** — two web replicas would both run `migrate` on boot
  against the one database, and Django takes no lock around it. Splitting
  `migrate` into its own one-shot compose service fixes both problems at once;
  see `docs/journal/phase-7.md`.
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
- `.env` holds real credentials and is gitignored, but `docker-compose.yml`
  references it through `${...}` interpolation, so a fresh clone has no database
  config. Phase 6's README needs to say which keys are required.

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
- `unique_open_invoice_per_tenant` is agreed and unbuilt. `AddConstraint` will
  fail until Acme is down to one OPEN invoice — it currently has `17`, `18`, `19`.
  See `docs/journal/phase-3.md` for the shape and for why the matching guard belongs at
  the top of `generate_invoice`, before the `atomic()` block.
- `InvoicesPay` still does its own `Invoice.objects.get(id=pk, tenant=tenant)`
  before calling `pay_invoice`, which repeats the service's lookup and makes
  `except services.InvoiceNotFound` unreachable. Harmless (both scope by tenant)
  but it is duplicated logic in two files.
- `LedgerEntry.description` is still never set — both invoicing and payment write
  `''`. Now that there are two kinds of pair in the table, a readable line
  (`"payment for invoice 42"` vs the billing window) is worth more than it was.
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
