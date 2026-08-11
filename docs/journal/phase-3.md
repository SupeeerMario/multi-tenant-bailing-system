# Phase 3 — build journal (idempotent payments)

Moved out of the root `CLAUDE.md` on 2026-08-11 so it stops loading into every
session. The "Traps hit" lists were removed at the same time — the ones that
kept repeating were promoted into `CLAUDE.md` under "Traps that repeat".

## Phase 3 — where it stands (2026-08-09)

Built and verified: `mock_payment_gateway` (`e5dd899`), then `pay_invoice`,
`InvoicesPay`, `PaymentSerializer` and the URL rename. Not built: the
`Idempotency-Key` claim, which is the whole point of the phase.

**Superseded 2026-08-10** — the claim was built the following session. Everything
below this line describes the state *before* it and is kept for the reasoning
(why a DB constraint rather than an `if`, why the pk is in the URL, the
double-charge repro that is the deliverable's "before"). For what the code
actually does now, read "Phase 3 — the claim, session 2" further down; where the
two disagree, session 2 wins.

| Route | View | Permission | Body |
|---|---|---|---|
| `POST billing/invoices/generate/` | `InvoiceAPIView` | `IsAuthenticated` | none |
| `POST billing/invoices/<int:pk>/pay/` | `InvoicesPay` | `IsAuthenticated` | `{"amount": "30.00"}` |

`pay_invoice(tenant, invoice_id, amount)` in `services.py`: resolve the invoice
with `get(tenant=tenant, id=invoice_id)` → refuse if not `OPEN` → refuse if
`invoice.amount != amount` → call the gateway → raise `PaymentDeclined` and write
**nothing** on a decline → otherwise, in one `transaction.atomic()`, set
`status='PAID'` and `paid_at`, save, and write the `CASH +amount` /
`ACCOUNTS_RECEIVABLE -amount` pair sharing one `transaction_id` → `return
invoice`. Four new `BillingError` subclasses (`InvoiceNotFound`,
`InvoiceAlreadyPaid`, `AmountMismatch`, `PaymentDeclined`) with `APIException`
twins in `views.py` at 404 / 409 / 400 / 402.

**Verified live 2026-08-09** on throwaway tenant `38`, invoices `42` (`30.00`)
and `43` (`66.66`):

| Case | Result |
|---|---|
| pay 42 `"30.00"` | 201→**200**, `status PAID`, `paid_at` set |
| pay 42 again | **409** `invoice already paid` |
| pay 43 `"66.66"` | **402** `payment declined`, invoice still `OPEN`, zero rows written |
| pay 43 `"10.00"` | **400** `amount mismatch` |

Ledger for invoice 42: exactly 2 rows, 1 distinct `transaction_id`, `AR -30.00` /
`CASH +30.00`, sum `0.00`. Earlier round against the real seed data also
confirmed Globex asking for Acme's invoice 17 gets **404**, byte-identical to a
nonexistent invoice — the response leaks nothing about whether the row exists.

### The double-charge is real, and it is the deliverable's "before"

`if invoice.status != 'OPEN'` already blocks a **sequential** retry, so the naive
version does not double-charge when you simply call it twice — it answers 409.
Four concurrent curls against the running stack also produced one 200 and three
409s, because the mock gateway returns instantly and the read-to-write window is
microseconds.

Widening that window to what a real gateway costs — monkeypatching a `0.3s` sleep
around `mock_payment_gateway` in a shell, two threads calling `pay_invoice` —
reproduces it immediately:

```
[(0, 'CHARGED'), (1, 'CHARGED')]
ledger rows 4   distinct txn 2
CASH total 60.00      <- on a 30.00 invoice
sum 0.00
```

Both charged. **The ledger still sums to `0.00`** — fourth time in this project
that the sum-to-zero invariant has gone green on corrupt data (after negative
fees, the stale-window invoice, and the overlapping-window invoice). The lesson
repeats: sum-to-zero proves the pairs are well-formed, never that they should
exist.

The race window spans from the invoice read to the ledger write, and the gateway
round trip sits inside it — so in production it is hundreds of milliseconds wide,
not microseconds. A double-clicked Pay button or a client retrying on a 300ms
timeout hits it.

### The claim, and why the order is the whole point

1. Read the `Idempotency-Key` header; missing → 400, it is required.
2. `request_hash` = sha256 hexdigest over `json.dumps({'invoice': <pk>,
   **request.data}, sort_keys=True)`.
3. `IdempotencyKey.objects.create(..., state='PROCESSING')`.
4. On `IntegrityError` the key is already claimed — read the existing row and
   branch: hash differs → **422**; `state='PROCESSING'` → **409** in flight;
   `state='COMPLETED'` → return the stored `response_status` / `response_body`.
5. Only then the gateway and the writes, finishing by stamping the row
   `COMPLETED` with the response.

**The claim must commit before the gateway call.** Putting step 3 inside the same
`atomic()` block as the ledger writes means a rollback erases the claim and the
gate never existed. `ATOMIC_REQUESTS` is not set so autocommit gets this right by
default — do not "tidy" it into the block later.

Why the constraint and not an `if`: both requests `INSERT` the same
`(tenant, key)`, Postgres serializes them, one wins and the other gets
`IntegrityError`. There is no interleaving where both pass, because the unique
index fuses the check and the write. `if invoice.status != 'OPEN'` is a `SELECT`
and a later `UPDATE` with a gap between them. Same reasoning as
`unique_active_subscription_per_tenant`.

**Limit worth knowing:** this only works when both requests carry the *same* key.
A double-click that generates two different keys still double-charges — two
different rows, no collision. The contract is the client's: one key per logical
payment, generated before the first attempt, reused on every retry. A server-side
backstop would be `UniqueConstraint(fields=['invoice', 'account'])` on
`LedgerEntry`, which blocks a second `CASH` row outright — but it also blocks a
refund's reversing pair, so it is a real tradeoff and not Phase 3 work.

### Decisions made this session

- **Routing: the invoice pk goes in the URL** (`invoices/<pk>/pay/`), not the
  body and not implied. Rejected "pay the tenant's oldest OPEN invoice with no
  id" — which was the preferred option until the author spotted that it makes
  `request_hash` useless. With no pk, the only client-supplied field is `amount`,
  and `base_fee` guarantees repeated amounts: every zero-usage month bills
  exactly `20.00` (invoice 19 already does). Two genuinely different payments
  then fingerprint identically, so a reused key looks like a retry and the second
  invoice is silently never paid.
- **Hash only what the client sent.** Folding the server-resolved invoice id into
  the fingerprint does not rescue the no-pk design either: after invoice 17 is
  paid, "oldest OPEN" resolves to 18, the recomputed hash differs from the stored
  one, and a *legitimate* retry is rejected as a client error. Server state moves
  between the original request and the retry; client input does not.
- **The pk lives in the URL, so it is not in `request.data`.** Hashing
  `request.data` alone rebuilds the collision above with code that looks correct.
  The hash input has to merge `self.kwargs['pk']` with the body.
- **The gateway takes no card number.** A `cardnumber` argument was written and
  removed: card numbers reaching your API puts you in PCI scope, and they would
  land in `request_hash` input and in every traceback. A real gateway already
  holds the card. If a card-shaped input is ever wanted, it is an opaque token
  (`tok_visa`), not a PAN.
- **A decline is the gateway's answer, not an error** — it must be stored and
  replayed, so a retry of a declined payment returns the same decline rather than
  re-attempting the card. Currently raised as `PaymentDeclined` for simplicity;
  when the key lands, the receipt has to survive into `response_body`, so either
  the exception carries it or the service returns it.
- **Payment logic lives in `services.py`, not the view.** Briefly decided the
  other way and reversed. The deciding argument is CLAUDE.md's own requirement
  that the key row, the ledger pair and the invoice update land in one atomic
  transaction — impossible if the key is managed in the view while the ledger is
  written in the service. Consequence: `key` and `request_hash` should be
  parameters of `pay_invoice`, with the view only reading the header and
  computing the hash.
- **`pay_invoice` takes `invoice_id`, not an `Invoice`.** Same move as dropping
  `generate_invoice`'s window parameters: hand the service a resolved object and
  a caller can hand it one belonging to another tenant. Resolving inside with
  `get(tenant=tenant, id=invoice_id)` makes that unrepresentable.
- **Past-due policy: cancel rather than accrue.** `PAST_DUE` at 7 days unpaid,
  `CANCELED` after. Chosen over three alternatives once the author noticed that a
  suspended tenant keeps accruing `base_fee` for months it was locked out of —
  six months away would owe `120.00` for zero access. `CANCELED` is not `ACTIVE`,
  so `generate_invoice` raises `NoActiveSubscription` and accrual stops. Both
  status flips need a scheduler, so this is **Phase 4 work**; `PAST_DUE` already
  exists in `SUBSCRIPTIONS_CHOICES`, unused since Phase 1.
- **One OPEN invoice per tenant** — agreed, not built. `UniqueConstraint(fields=
  ['tenant'], condition=Q(status='OPEN'), name='unique_open_invoice_per_tenant')`
  is `unique_active_subscription_per_tenant` with two words changed. It cannot be
  added yet: `AddConstraint` validates existing rows and Acme has **three** OPEN
  invoices (`17`, `18`, `19`), so it fails exactly like the `-99.00` plan did.
  Pay or void two first. Note the guard belongs at the top of `generate_invoice`,
  before the `atomic()` block — placed there the period advance never runs, so
  the cycle *pauses* on an unpaid invoice rather than sliding past unbilled usage.


## Phase 3 — the claim, session 2 (2026-08-10, uncommitted)

Built in five steps, each verified live against the running stack before the
next started. Steps 1 through 5a landed in session 2; **5b landed in session 3
on 2026-08-10** and all five steps now work. The "5b, what is wrong right now"
list below is kept as a record of what was broken and how each failure
presented — every item on it is fixed.

### What landed

**Step 1 — the header.** `InvoicesPay.post()` reads
`request.headers.get('Idempotency-Key')` at the top, before the invoice lookup.
Falsy → `IdempotencyKeyMissing` (400). Length > 512 → `IdempotencyKeyTooLong`
(400), matching `IdempotencyKey.key`'s `max_length`.

`request.headers` is case-insensitive and returns **str**, not bytes — the
opposite of `get_authorization_header()` in the auth layer, which needed
`.decode()`. Guarding on falsy is right here because `''` (from a header sent
with no value) is genuinely invalid; note this is the opposite call from
`Decimal('0')`, where falsy is a legal value. The rule is not "never use
truthiness", it is "decide whether falsy is legal for this type".

A `len(key) < 10` floor was written first and removed: the server never parses
this string, only compares it, so it has no standing to demand a format.
`Idempotency-Key: banana` is a valid key and returns the same result as a uuid.

**Step 2 — `request_hash`.**

```python
request_hash = hashlib.sha256(
    json.dumps({"invoice": pk, **self.request.data}, sort_keys=True).encode()
).hexdigest()
```

Verified all three required properties live:

| Input | Digest |
|---|---|
| `{'invoice': 17, 'amount': '30.00'}` | `8bb8c9ae…` |
| same, recomputed | `8bb8c9ae…` — deterministic |
| `{'note':'x','amount':'30.00'}` vs same keys reordered | identical — `sort_keys` works |
| `{'invoice': 19, 'amount': '20.00'}` vs `{'invoice': 43, 'amount': '20.00'}` | different — the `base_fee` collision decision 7 exists to kill |

**Step 3 — the claim.** `pay_invoice` gained `idempotency_key` and
`request_hash` parameters, `IdempotencyKey` was added to the `services.py` model
import, and the `create(..., state='PROCESSING')` sits **above** the gateway
call and **outside** the `atomic()` block. First run behaved exactly as intended:
first payment 200, second with the same key a 500 carrying

```
IntegrityError: duplicate key value violates unique constraint "unique_key_per_tenant"
DETAIL:  Key (tenant_id, key)=(40, demo-key-001) already exists.
```

and **two** ledger rows on that invoice, not four. The double-charge is dead for
the same-key case.

**Step 4 — the branch.** `try/except IntegrityError` around the claim, and
inside the `except`, read the row back with `get(tenant=..., key=...)` and
decide:

| Existing row | Answer | Verified |
|---|---|---|
| `request_hash` differs | `RequestHashDiffers` → 422 | yes |
| `state='PROCESSING'` | `PaymentAlreadyProcessing` → 409 | yes |
| `state='COMPLETED'` | return the stored body | yes (5a/5b) |

Hash check goes first: a mismatched hash is a client bug whatever state the row
is in, and answering "still processing" to a wrong payload misleads.

**Step 5a — build the body, stamp the row.** The `create()` return value is now
bound (`idem_key_object`). Inside the existing `atomic()` block, after the
ledger pair, a hand-built `invoice_dict` mirroring `InvoiceSerializer`'s nine
fields is assigned to `response_body`, with `response_status` and
`state='COMPLETED'`, then saved. Verified live on invoice `80`:

```
state: COMPLETED | response_status: 200
body: {"id": 80, "amount": "30.00", "status": "PAID", "tenant": 40, ...}
inv80: PAID | ledger rows: 2
```

### `exceptions.py` — the API exceptions moved out (this session)

New file `billing/exceptions.py` holds all ten `APIException` subclasses;
`views.py` imports it and every raise site is now `exceptions.X`. `views.py` was
196 lines with over half the top being exception declarations.

**The `BillingError` tree deliberately stayed in `services.py`.** One file
holding both would mean `services.py` imports a module that imports
`rest_framework` at module level — decision 2 broken through the back door, and
invisibly, since nothing fails until the Phase 4 worker runs somewhere DRF is
not configured. The check that the split held:

```bash
grep -c "rest_framework" billing/services.py     # must be 0
```

It is 0. Import direction is one-way: `views.py` imports both trees,
`services.py` imports only its own.

The `services.` prefix on every `except` clause is still mandatory — moving the
API classes to another module does not fix the shadowing, since
`InvoiceAlreadyPaid` still exists in both trees.

Renamed while moving: `WrongIdempotencyKey` → `IdempotencyKeyTooLong`, because
step 4's 422 ("same key, different hash") is the error a reader would actually
call a wrong key. Naming the length check that first would have produced two
plausibly-named classes and the same failure mode as the duplicate
`NoActiveSubscription`.

Parked, not done: 7 service errors each with a hand-written HTTP twin is 14
classes to say 7 things. A dict at the boundary (`{services.AmountMismatch: 400,
…}`) collapses it, at the cost of grep-ability. Not while the hero feature is
half-built.

### 5b — what was wrong (all fixed 2026-08-10)

**Every item below is closed.** Kept because each one failed in a different way
and none of them failed loudly; the list is more useful as a catalogue of how
these break than as a to-do.

The intent: `pay_invoice` returns a `(body, status)` pair on **every** path, the
view unpacks and builds the `Response`, and the decline stops being an exception
and becomes a stored, replayable answer. Note the final order is
`(body, status)`, not the `(response_status, response_body)` written when this
was planned — the view's names settled it.

What was in the file:

1. **The decline does not `return`.** An `if gateway_res['status'] ==
   'succeeded': … else: …` picks a `body` dict and a number, then execution
   continues into the `atomic()` block regardless. A declined payment marks the
   invoice `PAID` and writes the `CASH`/`AR` pair. Live proof on invoice `81`
   above. **Fix this first.** The branch must `return` and leave the function;
   no `else` is needed, the success path is everything after it.
2. **The `body` dict is never used as a body.** It is built, then `invoice_dict`
   is stored and returned in both cases. `body` is read only for `body['code']`.
3. **`'code'` holds an HTTP status (`200`/`402`).** Two different things merged.
   The HTTP status belongs in the returned tuple; `gateway_res['code']` — the
   gateway's reason string, e.g. `'insufficient_funds'` — belongs in the body.
4. **The `COMPLETED` arm returns one value**, `exist.response_body`, while every
   other path returns a tuple. Needs `return exist.response_status,
   exist.response_body`. A dict is iterable, so a two-name unpack of a 9-key
   dict fails on count rather than silently binding keys — but a 2-key body
   would bind key *names* and look plausible.
5. **`'detils'`** → `'detail'`, which is what DRF uses everywhere else in this
   API.

Plus in `views.py`: the unpack is backwards —
`invoice_dict, invoice_dict_status = services.pay_invoice(...)` against a service
returning `(code, dict)`. Names and return order must agree.

Target shape:

```
gateway_res = mock_payment_gateway(...)

if gateway_res['status'] == 'declined':
    body = {'detail': 'payment declined', 'code': gateway_res['code']}
    stamp row: COMPLETED, 402, body        # not in atomic() — no other writes
    return 402, body

with transaction.atomic():
    invoice PAID, ledger pair, invoice_dict, stamp row COMPLETED/200
return 200, invoice_dict
```

`PaymentDeclined` and `PaymentAlreadyPaid` both get deleted from `services.py`,
`exceptions.py` and the view's `except` chain — both stop being errors. A stale
`except services.PaymentDeclined` after the class is gone does **not** fail at
import: Python evaluates that expression only when an exception propagates, so
it sits quiet until some unrelated error hits and then masks it with an
`AttributeError`.

### Decisions made this session

- **The claim goes below `amount mismatch`, above `status != 'OPEN'`.** They are
  different kinds of check. A bad amount is client input and nothing has
  happened, so it must not burn the key — the client can resend with the same
  one. The status check is server state, and on a retry of a *successful*
  payment it is `PAID` precisely because this key already paid it; leaving it
  above the claim means the retry returns 409 and never reaches the stored
  receipt, which is the exact behaviour Phase 3 exists to remove.
- **Consequence, accepted:** with the amount check above the claim, a *different*
  amount on the same key returns 400 before the hash is ever compared, so the
  422 arm is reachable only through non-`amount` body drift or key reuse across
  invoices. Both are real. Verified live: paying invoice `80` (also `30.00`) with
  invoice `78`'s key returns 422 — amount is correct for `80`, only the `pk` in
  the hash catches it. Without `pk` in the hash that request would have replayed
  invoice `78`'s receipt while `80` stayed `OPEN`.
- **Option A for the response body: hand-build the dict in `services.py`**,
  rather than importing `InvoiceSerializer`. Keeps the DRF-free property
  measurable at 0. Cost is a second list of invoice fields that can drift from
  the serializer; comment it as mirroring `InvoiceSerializer`.
- **The stamp lives inside the same `atomic()` block as the ledger pair.** Split
  them and a crash between commit and stamp leaves a `PROCESSING` row on a `PAID`
  invoice — every retry 409s forever. The claim `INSERT` stays outside; only the
  `UPDATE` is inside.
- **`(status, body)` tuple rather than a bare dict**, so `response_status` drives
  the response instead of a hardcoded 200. Only justified once declines are
  stored — hence doing the decline now rather than deferring it.
- **Decline body is mapped, not forwarded.** `{'detail': 'payment declined',
  'code': gateway_res['code']}` — the client sees this API's shape, not the
  gateway's. Storing `gateway_res` verbatim was the simpler option and was
  rejected: a real gateway returns issuer text and internal reason codes, and
  forwarding them makes the API contract whatever the provider decides to put in
  a string. Whatever is chosen, **store exactly what is returned** — if the
  stored body and the fresh body differ, the replay proof fails by construction.


### The two-curl proof — Phase 3's deliverable (captured 2026-08-10)

Captured after 5b landed, against the running stack, on throwaway tenant `40`.
This is the artifact the phase exists to produce; the Phase 6 README quotes it.
Reproduce it after any change to `pay_invoice` — the curls are the test until
Phase 5 writes a real one.

```
==============================================================
 PHASE 3 PROOF — same payment request twice, charged once
 captured 2026-08-10, tenant 40, invoice 85 (USD 30.00, OPEN)
==============================================================

--- ledger for invoice 85 BEFORE any request ---
rows: 0

$ curl -i -X POST http://localhost:8000/billing/invoices/85/pay/ \
    -H 'Authorization: Api-Key <api-key>' \
    -H 'Idempotency-Key: demo-pay-85' \
    -H 'Content-Type: application/json' \
    -d '{"amount": "30.00"}'

--- REQUEST 1 ---
HTTP/1.1 200 OK
Content-Type: application/json
X-Content-Type-Options: nosniff

{"id":85,"amount":"30.00","status":"PAID","tenant":40,"paid_at":"2026-08-09T23:57:07.308490+00:00","currency":"USD","created_at":"2026-08-09T23:56:45.967324+00:00","period_end":"2026-09-19T23:56:45.966625+00:00","period_start":"2026-09-18T23:56:45.966625+00:00"}

--- REQUEST 2 (identical, same Idempotency-Key) ---
HTTP/1.1 200 OK
Content-Type: application/json
X-Content-Type-Options: nosniff

{"id":85,"amount":"30.00","status":"PAID","tenant":40,"paid_at":"2026-08-09T23:57:07.308490+00:00","currency":"USD","created_at":"2026-08-09T23:56:45.967324+00:00","period_end":"2026-09-19T23:56:45.966625+00:00","period_start":"2026-09-18T23:56:45.966625+00:00"}

--- ledger for invoice 85 AFTER both requests ---
invoice status      : PAID
invoice paid_at     : 2026-08-09 23:57:07.308490+00:00
ledger rows         : 2
distinct txn ids    : 1
    ACCOUNTS_RECEIVABLE -30.00
    CASH 30.00
sum                 : 0.00
idempotency state   : COMPLETED 200
```

The two bodies are **byte-identical**, confirmed by `diff` on a separate run of
the same pair (invoice `84`, key `pay-live-002`) — empty output. That only holds
because both paths return `idem_key_object.response_body` after
`refresh_from_db()`; see "Byte-identical replay" below.

The sharpest line in the block is `paid_at`, identical in both responses. A
second charge would have stamped a new one. `ledger rows 2` with `distinct txn
ids 1` is the other half: exactly one pair, so the money moved once. Compare
against the two-thread repro in the session-1 section, which produced `ledger
rows 4`, `distinct txn 2`, `CASH total 60.00` on the same `30.00` invoice — and
still summed to `0.00`.

**A decline is also stored and replayed**, invoice `86` (`66.66`, which the mock
always declines):

```
--- REQUEST 1 ---                    --- REQUEST 2 (same key) ---
HTTP/1.1 402 Payment Required        HTTP/1.1 402 Payment Required
{"code":"insufficient_funds",        {"code":"insufficient_funds",
 "detail":"payment declined"}         "detail":"payment declined"}

invoice status      : OPEN
invoice paid_at     : None
ledger rows         : 0
idempotency state   : COMPLETED 402
stored body         : {'code': 'insufficient_funds', 'detail': 'payment declined'}
```

`OPEN` / `paid_at None` / **0 ledger rows** is the fix to the bug that opened
this session — invoice `81` was marked `PAID` with a full `CASH` pair by a
declined payment. The retry replays the stored decline rather than re-attempting
the card.

**Every other arm, run live the same day, output verbatim:**

| Case | Response | Status |
|---|---|---|
| same key, different `request_hash` (key `demo-pay-85` reused on invoice `87`, amount still `30.00`) | `{"detail":"request hash differs"}` | 422 |
| same key, still in flight (key `dec-live-001`, state `PROCESSING`) | `{"detail":"payment is already processing"}` | 409 |
| no `Idempotency-Key` header | `{"detail":"Idempotency Key Missing"}` | 400 |
| amount does not match (sent `10.00` for a `30.00` invoice) | `{"detail":"amount mismatch"}` | 400 |
| another tenant asking for invoice `87` (Acme's key) | `{"detail":"no invoice to pay"}` | 404 |
| no `Authorization` header | `{"detail":"Authentication credentials were not provided."}` | 401 |

The 422 row is worth keeping in the README. Same key, same `30.00` amount, a
different invoice — **only the `pk` inside `request_hash` catches it**. Without
it, that request replays invoice `85`'s receipt while `87` stays `OPEN` and
unpaid, silently. It is decision 7's `base_fee` collision, reproduced on demand.

The 404 is byte-identical to a nonexistent invoice, so the response leaks
nothing about whether the row exists.

### Byte-identical replay — why `refresh_from_db()` is there

`response_body` is a `JSONField`, which is `jsonb` on Postgres, and **`jsonb`
does not preserve key order** — it re-sorts by key length, then alphabetically.
Before this was handled, the first response carried the literal order of
`invoice_dict` while the replay came back
`id(2) amount(6) status(6) tenant(6) paid_at(7) currency(8) created_at(10)
period_end(10) period_start(12)`. Same content, different bytes.

Both paths now end:

```python
idem_key_object.save()
idem_key_object.refresh_from_db()
return idem_key_object.response_body, <status>
```

so the fresh response comes out of `jsonb` too. Cost is one extra `SELECT` per
payment. The ordering was the visible symptom; the reason to keep it is that the
returned body **is** the stored body by construction, not by two lines being kept
in sync. Note `refresh_from_db()` alone changes nothing — it was added once and
the returns still handed back the local dict, so it was a wasted query until the
`return` lines were changed too.


### A decline burns the key — the client contract, and a 5-minute TTL

**Current behaviour, verified live:** a retry carrying the same key as a declined
payment replays the stored `402` forever. The gateway is never called again, the
invoice stays `OPEN`, and no ledger row is ever written. `demo-dec-86` twice:
both `402`, identical body, `ledger rows 0`.

That is correct and it is what real gateways do, but it has a sharp edge. A
customer declined for `insufficient_funds` who tops up and presses Pay again
gets the stored `402` back and **can never pay that invoice** if the client
reused the key. The invoice then sits `OPEN`, goes `PAST_DUE` at 7 days and
`CANCELED` after (decision 8) — for a customer who was trying to pay.

**The contract is one key per *attempt*, not per invoice.** Reusing a key is
only correct when the client does not know whether the first request landed: a
timeout, a dropped connection, a double-clicked button. A retry after a decline
is a new attempt and needs a new key. This has to be stated in the Phase 6
README, because nothing in the API can enforce it and the failure is silent.

Why not just re-attempt the card on a replayed decline: a client retrying on a
300ms timeout after a *successful* charge would then hit the gateway twice —
the original double-charge wearing a different hat. Secondary reason, real in
production: repeatedly re-attempting a declined card gets a merchant flagged by
the issuer, and card networks fine retry storms.

**Agreed, not built: expire a `COMPLETED` key whose `response_status` is `402`
after 5 minutes.** Scoped deliberately narrow — only declines, only 5 minutes.
It is safe in a way a general TTL is not: the first attempt moved **no money**,
so letting the key re-attempt cannot double-charge. A TTL on a `COMPLETED 200`
key would be exactly the opposite and must not be added by symmetry.

Shape, when it is built:

- In the collision branch, before the `state == 'COMPLETED'` replay, test
  "declined **and** older than 5 minutes". If so, re-attempt; otherwise replay.
- **Do not delete the row and re-`INSERT`.** Flip the existing row back to
  `PROCESSING` with a conditional update and check the row count:
  `IdempotencyKey.objects.filter(id=..., state='COMPLETED').update(state='PROCESSING')`
  returns `1` for the winner and `0` for everyone else. Two concurrent retries
  arriving after expiry would otherwise both read `COMPLETED`-and-stale and both
  call the gateway — the check-then-write bug again, in the one branch that
  exists to prevent it. Let the `UPDATE` decide, same reasoning as the claim
  `INSERT` itself.
- The loser of that race gets the `PROCESSING` 409, which is honest — someone
  really is in flight.
- **`created_at` is `auto_now_add`, so it is the claim time, not the completion
  time.** For a decline the two are milliseconds apart so it is usable, but a
  `completed_at` column would be the honest field and would also serve the
  stale-`PROCESSING` question below. Worth adding both at once.

Open and undecided: whether 5 minutes is right. It is short enough that a
timeout retry (seconds) still replays, but topping up a card can take less than
5 minutes, so a customer can still land inside the window and be told `402` for
a card that now works. Shorter is friendlier and weaker; longer is safer and
more annoying. Do not tune this before Phase 5 can test it.

Note also that `COMPLETED 200` keys currently **never** expire — the row lives
forever. Real gateways age keys out at 24h. Not wrong here, and it is the same
family of question as the stale-`PROCESSING` rows below; decide all three
together rather than one at a time.

### Where a check goes relative to the claim — the rule

Settled 2026-08-10, after trying to move the status check and watching it break
the feature. Three checks now sit in three deliberate places, and the rule that
puts them there is worth more than the individual placements:

- **Client-fixable errors go *above* the claim.** `amount mismatch` is the case.
  Nothing has happened, the client corrects the body and resends **with the same
  key**, so the key must not be burned.
- **Terminal server-state answers go *below* the claim and are *stored*, not
  raised.** `payment declined` and `invoice already paid`. The answer will not
  change on a retry, so it is a receipt rather than an error.
- **Crashes are covered by neither.** That is the staleness rule's only remaining
  job.

Why the status check cannot move above the claim, which is the non-obvious half.
**A retry always arrives at a `PAID` invoice** — request 1 paid it; that is what
a retry is. Proved live on invoice `89`:

```
STEP 1   invoice 89 status: OPEN
STEP 2   request 1, key r1   -> 200, the receipt
STEP 3   invoice 89 status: PAID
STEP 4   retry,     key r1   -> 200, the stored receipt
```

With `if invoice.status != 'OPEN': raise` above the claim, STEP 4 raises 409 and
never reaches the collision branch — the `COMPLETED` arm becomes unreachable code
and "same request twice returns the same result" becomes `200` then `409`.

The reason underneath: **`invoice.status == 'PAID'` is ambiguous.** It means
either "someone else paid this, you are too late" (409 is right) or "**this very
key** paid it and your connection dropped" (replay is right). The invoice row
cannot tell those apart; the idempotency row can. So the key must be consulted
first, and the status check must sit after it.

The trade, before and after storing the 409:

| Order | Retry of a successful payment | New key at a paid invoice |
|---|---|---|
| check **above** claim | 409 — feature dead | 409, no orphan |
| check **below**, 409 **raised** | 200, stored receipt | 409 + orphan row |
| check **below**, 409 **stored** (current) | 200, stored receipt | 409 stored and replayable, no orphan |

**Verified live 2026-08-10**, one invoice, two keys:

```
k1 #1  200  {"id":90,...,"status":"PAID"}
k2 #1  409  {"detail":"invoice already paid"}
k2 #2  409  same body          <- was "payment is already processing" before
k1 #2  200  same receipt

invoice 90: PAID | ledger rows 2
  k1 | COMPLETED | 200 | {...invoice...}
  k2 | COMPLETED | 409 | {'detail': 'invoice already paid'}
```

Note `k2`'s old behaviour was itself a replayability bug: attempt 1 answered
"invoice already paid" and every attempt after answered "payment is already
processing" — same key, same request, two different bodies.

How a client legitimately reaches the 409 at all: by paying with a **different
key**. A double-click where the client mints a key per click, two tabs, two
devices, or a client that lost its key. The constraint only fuses requests
carrying the *same* key — that limit is recorded in the session-1 section.

Rejected: answering `200` to the second key because the invoice is settled
anyway. It hands back a receipt for a charge that never happened on that
request, and hides the fact that two keys raced for one invoice.

`InvoiceAlreadyPaid` was deleted from `services.py`, `exceptions.py` and the
view's `except` chain as part of this, exactly like `PaymentDeclined`.

### Orphaned `PROCESSING` rows — narrowed, not closed

Originally: any exception raised *after* the claim commits left a row stuck at
`PROCESSING` forever — the decline (until 5b), and the status check (until the
409 was stored). Both of those are now terminal, stored answers.

**What remains is only the genuine crash**: process killed, connection dropped,
or an unhandled 500 between the claim `INSERT` and the stamp. No reordering
touches that class, which is why the two fixes were never really alternatives —
cleaning up on known raise paths covers no crash at all.

The upside of narrowing it: **`PROCESSING` now has a single meaning** — in
flight, or died mid-flight. Before, it was polluted by rows that were never in
flight for a second, so no rule could distinguish them. Now age is a usable
signal, because a payment does not take 30 seconds.

Still undecided, and it is the same question as the decline TTL: what the
threshold is, whether a stale row is taken over or deleted, and whether the
takeover uses the conditional-`UPDATE` guard described under "A decline burns
the key". `created_at` is `auto_now_add` and is the *claim* time, which is the
right clock for this one. Decide before Phase 5 writes a test against the 409
`PROCESSING` arm.

Do **not** "fix" this by deleting the claim row before raising. It opens a race:
a concurrent request that already hit `IntegrityError` then runs
`IdempotencyKey.objects.get(...)` and finds nothing — `DoesNotExist`, 500.
Storing the answer has no such window.
