---
name: project-journal
description: Phase-by-phase build history for this multi-tenant billing API — what was built, what was verified live, and why each design call went the way it did. Read BEFORE editing billing/services.py, billing/views.py, billing/models.py, billing/serializers.py, billing/exceptions.py, or a migration, and before designing a new endpoint, constraint, or ledger write. Covers the double-charge repro, the Idempotency-Key claim, the two-curl proof, and the horizontal-scale plan.
---

# Project journal

The build record was moved out of the root `CLAUDE.md` on 2026-08-11 so it stops
loading into every session. Read the one file that matches what you are about to
touch — do not read all three.

| Read this | When you are working on |
|---|---|
| `docs/journal/phase-2.md` | `generate_invoice`, the auth layer, any of the five Phase 2 endpoints, serializers, model constraints, migrations, `__str__`, queryset ordering |
| `docs/journal/phase-3.md` | `pay_invoice`, `InvoicesPay`, the `Idempotency-Key` claim, the mock gateway, ledger pairs on payment, the decline/TTL contract, where a check goes relative to the claim |
| `docs/journal/phase-7.md` | Docker `CMD`, gunicorn, nginx, running more than one web container |

## What these files are good for

Live verification tables (real curls, real amounts, real row counts), the
reasoning behind rejected alternatives, and the exact shape of code that already
exists. When the root `CLAUDE.md` says a thing was "verified live", the evidence
is here.

## What is NOT here

The per-phase **"Traps hit, worth not re-learning"** lists were deleted on
2026-08-11. The ones that kept repeating — `filter()` where `get()` was meant, a
branch that neither returns nor raises, check-then-write, unpacking a dict into
two names, two `atomic()` blocks being two transactions — were promoted into the
root `CLAUDE.md` under **"Traps that repeat"**, which is always loaded. Do not
re-add trap lists here; put a repeating one in the root file instead.

Also always loaded, so do not duplicate: the phase gating table, "How to work
with me on this", the locked conventions (money, choices tuples, `related_name`,
`on_delete`), the ledger invariants, the idempotency contract, testing
conventions, the open design decisions, and the known open items.

The dev-database seed snapshot lives in `docs/journal/phase-2.md`. It drifted
from reality four separate times — query the database rather than trusting the
written row counts.
