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
| 1 — Data model + skeleton | `docker compose up` starts API + Postgres, migrations create all tables | **In progress** |
| 2 — Core endpoints | Create tenant → assign plan → record usage → generate correct invoice, all via API | Blocked on 1 |
| 3 — Idempotent payments | Same payment request twice returns same result, charges once (save both curl commands + output) | Blocked on 2 |
| 4 — Multi-tenancy + worker | Worker generates invoices on a schedule; tenant isolation proven by a test | Blocked on 3 |
| 5 — Tests + docs + CI | CI green on push (lint + tests + Docker build), Swagger lists every endpoint | Blocked on 4 |
| 6 — Deploy + package | Live URL, README with architecture diagram + no-double-charge proof, decision note | Blocked on 5 |

### What Phase 1 still needs

The **data model half is done and verified**: all seven tables exist, both
migrations apply, `manage.py check` is clean, and shell checks pass for choices
validity, reverse accessors, both unique constraints, ledger balancing, tenant
isolation, and PROTECT behavior. `0001_initial.py` confirmed to contain
`unique_invoice_period`, `unique_key_per_tenant`, and the `LedgerEntry` index.

The **infrastructure half has not started**:

- No `Dockerfile`, no `docker-compose.yml`, no `.dockerignore`. The Done-when is
  literally `docker compose up` — that command does not exist yet.
- No `requirements.txt`. Needed by both the Docker build and Phase 5 CI. The
  venv currently holds only Django 6.0.7, asgiref, sqlparse.
- Still on SQLite. The Postgres transition is the author's declared next task.

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

1. **How a request identifies its tenant.** `Tenant` has no API key, no token,
   and no link to `django.contrib.auth.User`. There is currently no way to
   answer "who is calling?", and Phases 2, 3, and 4 all depend on that answer.
   This is the largest open gap. Settle it before writing Phase 2 endpoints.
2. **Where business logic lives.** Invoice generation is called from two places:
   the Phase 2 API endpoint and the Phase 4 background worker (which has no HTTP
   request). Same for the payment/ledger atomic block. If that logic goes inside
   a view, Phase 4 forces a rewrite. A `billing/services.py` that both views and
   tasks call avoids it. Decide before writing views, not after.
3. **One invoice per tenant, or per subscription?** `Invoice` links to `Tenant`
   only, but amount derives from `Plan` via `Subscription`. With one active
   subscription per tenant this is unambiguous; with several it is not.

## Known open items

Blocking Phase 1:
- No Docker Compose stack (API + Postgres). This is the phase's Done-when.
- No `requirements.txt`.
- Still on SQLite (`db.sqlite3`); the Postgres transition is planned and is the
  author's declared next task. Until it happens, the expected-failure tests
  described under "Testing conventions" pass for the wrong reason, and `Decimal`
  trailing-zeros noise is expected.

Needed before Phase 6 deploy, cheapest to do alongside the Docker work:
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
- `billing/url.py` is empty and misnamed; Django and `config/urls.py` expect
  `urls.py`. Rename while it is still empty.
- `Subscription.status` has no default, while `Plan.interval` and
  `IdempotencyKey.state` do.
- No `__str__` on any model yet. Add short ones to make shell debugging
  readable.
- `billing/admin.py` registers nothing. Registering the seven models gives a
  clickable UI for inspecting tenants, invoices, and ledger rows during Phases
  2–3.
- Consider migrating existing choice sets to `TextChoices`.
- DRF is not installed. Phase 2 endpoints and Phase 5 Swagger both need it.

## Commands

The venv is at `.venv/` and is not activated automatically:

```
.venv/bin/python manage.py makemigrations
.venv/bin/python manage.py migrate
.venv/bin/python manage.py check
.venv/bin/python manage.py test
```

After generating a migration, confirm it contains the two `AddConstraint`
operations (`unique_invoice_period`, `unique_key_per_tenant`) and the
`LedgerEntry` index before applying.

## Repository hygiene

`.gitignore` covers `.venv/`, `__pycache__/`, `*.pyc`, `db.sqlite3`, `.env`.
Tracked file count should stay around 18 — if it jumps, something got committed
that shouldn't have been.

Note that `.gitignore` only affects **untracked** files; anything already in the
index keeps being tracked until `git rm -r --cached` removes it. The `.venv` and
`db.sqlite3` blobs still exist in the older commits `e8166d4` and `41fb8c3`.
Purging them would mean rewriting all history — not worth it here, and nothing
new is being added.
