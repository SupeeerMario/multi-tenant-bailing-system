# Phase 7 — horizontal scale (nothing built)

Moved out of the root `CLAUDE.md` on 2026-08-11 so it stops loading into every
session. The "Traps hit" lists were removed at the same time — the ones that
kept repeating were promoted into `CLAUDE.md` under "Traps that repeat".

## Phase 7 — horizontal scale (added 2026-08-09, nothing built)

Scoped from a system-design discussion, not from the original six-phase spec.
Deliberately its own phase rather than folded into Phase 6: Phase 6 gets a
single container live and packaged, Phase 7 scales that. Splitting keeps Phase
6's "Done when" reachable without a load balancer in the way, and means a broken
LB cannot block having a live URL at all.

Target topology — one Postgres, deliberately:

```
nginx  ->  web1  \
       ->  web2  --> one Postgres
```

**Why it is worth doing at all.** The Phase 3 idempotency claim is a database
unique constraint on `(tenant, key)`, not a Python lock. Two separate containers
prove that distinction in a way a single process cannot: a `threading.Lock`, a
module-level set, or an in-process cache would all pass a single-server test and
break here. The one shared thing is the DB, which is exactly why the constraint
is the only mechanism that can work. Same reasoning as
`unique_active_subscription_per_tenant`.

**What it does not do: it does not create the double-charge.** The existing
two-thread repro (`CASH 60.00` on a `30.00` invoice, ledger sum `0.00`) is
already valid evidence of the bug and stays the Phase 3 deliverable's "before".
Phase 7 upgrades the "after" from *same process, monkeypatched sleep* to *two
containers, real HTTP*. Stronger demo, not a different bug — do not oversell it
as one in the README.

Cost that comes with that: the race window can no longer be widened by
monkeypatching `mock_payment_gateway` in a shell, because the two callers are in
different processes. The gateway needs a real configurable delay (an env var
like `GATEWAY_LATENCY_MS`, defaulting to 0) for the cross-container run to be
reproducible.

### The blocker: `migrate` in the Dockerfile `CMD`

**Two replicas of the current image both run `migrate` on boot, concurrently,
against the one database.** Django takes no lock around migrations, so both
processes read the same unapplied list and both try to apply it — best case one
dies on a duplicate `django_migrations` row, worst case half-applied DDL. This
is the PID-1/`sh -c` item under Known open items showing its teeth for the
second time (the first was a failed migration taking the web container down
with `Exited (1)`).

Migration has to leave the app container's start command before replicas > 1.

**Decided: option 1, a one-shot `migrate` service in compose.** It runs
`python manage.py migrate` and exits; both web services declare `depends_on`
with `condition: service_completed_successfully`. The boot chain becomes
`db healthy -> migrate exits 0 -> web1/web2 start`. Chosen because the `db`
healthcheck already exists so the chain is half-built, and because it draws
cleanly in the Phase 6 README architecture diagram. Side benefit: web's command
becomes a single exec'd gunicorn process instead of `sh -c "a && b"`, which
closes the PID-1 `SIGTERM` item at the same time.

Rejected: **migrate as a manual deploy step outside compose** — what most real
deploys do, and correct for backwards-incompatible migrations that need a human
picking the moment, but nothing to show and more to explain. Rejected:
**`pg_advisory_lock` in an entrypoint script** — works, but hand-rolls
coordination the orchestrator already provides.

### Build order inside the phase

1. Phase 3 done — the `Idempotency-Key` claim works and a retry returns the
   stored response on a single server. Landing the LB before this just produces
   a fatter double-charge with nothing to demonstrate.
2. Phase 5 green — tests passing single-server, so a test that fails after the
   LB lands is unambiguously the LB's fault.
3. gunicorn + real entrypoint, `migrate` split into its own service.
   `runserver` is a single-process autoreloading dev server; two replicas of it
   behind nginx is not a thing to put in a README.
4. Settings from env — `DEBUG=False`, `ALLOWED_HOSTS`, `SECRET_KEY`. Already a
   Known open item; nginx makes `ALLOWED_HOSTS` mandatory rather than optional,
   since the Host header now arrives through a proxy.
5. Scale to 2, put nginx in front, rerun the same-key retry proof across
   containers.

Two things that will bite at steps 3–4:

- **`DEBUG=False` kills admin CSS.** `runserver` serves staticfiles
  automatically only under `DEBUG`. Under gunicorn that stops, so `/admin/`
  returns 200 with no styling until whitenoise or nginx serves `/static/`.
- **Connection count multiplies.** 2 containers × gunicorn workers × Django's
  persistent connections, against Postgres `max_connections` (default 100). Not
  fatal at this size, but it is the honest reason PgBouncer exists and is worth
  a line in the decision note.

### Read replica and DB backups — considered and dropped

Both were raised alongside the LB on 2026-08-09 and **dropped, not deferred**.
Recorded so they are not re-proposed.

A **read replica is not a backup**: streaming replication copies mistakes at
wire speed, so `DELETE FROM billing_invoice` arrives on the replica in
milliseconds. It also actively endangers this domain — replication lag breaks
read-after-write, and `pay_invoice`'s `get()` is exactly such a path. A retry
reading a lagging replica sees `status='OPEN'` on an invoice already paid on the
primary and calls the gateway again, reintroducing the Phase 3 bug through
infrastructure, underneath the application fix. Read replicas earn their keep
when reads dominate and staleness is harmless; this API's read traffic is a
couple of list views.

**Backups were dropped by the author's call**, so no `pg_dump` schedule or
snapshot policy is planned. Do not re-raise either.
