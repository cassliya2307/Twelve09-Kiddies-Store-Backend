# Twelve'09 Kiddies Store — PostgreSQL Import Rehearsal Runbook (Phase 5D/5E)

> **Status — READ THIS FIRST (Phase 5E.2, implementation only).**
> `tools/pg_import.py` **has no database execution path in this build.**
> `EXECUTION_ENABLED = False` and nothing can flip it from the CLI or the
> environment. Every stage below that touches a database is therefore
> **aspirational**: the commands are recorded so they are ready for Phase 5E,
> but today **every `--dry-run`, `--apply`, `--validate-only` and
> `--reset-sequences` invocation stops at the execution gate and exits `4`
> (GATE)**, even with perfect configuration. No import, no reconciliation, no
> transaction, and no sequence reset has been written, run, or verified.
> **Nothing below is executed until Phase 5E is explicitly authorized.**
>
> Phase 5E.2 added a **read-only** layer that is also unreachable while the
> guard is False: source charset contract, MySQL `information_schema`
> schema-drift guard, allowlisted schema-qualified extraction ordered by
> primary key, and a text-vs-bytes audit. It opens **no target connection** and
> issues **no writes of any kind**.
>
> Phase 5E.3 added the **read-only pre-import compatibility checks** over the
> extracted rows. Also unreachable while the guard is False, and also writes
> nothing: they classify the source as import-compatible or not, and report
> business-rule anomalies without blocking.

## Fixed decisions

- **Canonical source:** the verified MySQL backup
  `backups/twelve09_kiddies_store_backup_20261014_155033.sql`
  (SHA-256 `9DAADE2C747A31112281972E7EDB64EA41E57DB11B6C01357D4D0EE34CCFC76D`,
  27,186 bytes), **restored into a newly created disposable MySQL instance.**
  Never import from the live local database.
- **Target:** a disposable PostgreSQL 16 instance. Never Supabase/production.
- **Timestamp policy — Outcome A:** MySQL `DATETIME` values are copied
  **unchanged** into `TIMESTAMP WITHOUT TIME ZONE`. No timezone inference, no
  offset. Historical timezone provenance is **unverified**; demo timestamps are
  **not audit-grade**.
- **Alembic history:** PostgreSQL migrates via `alembic_pg` and creates its own
  `alembic_version` (`pg_baseline`). The MySQL `alembic_version` row is **never**
  imported.
- Frontend stays on Vercel. MySQL Alembic chain is **not** modified.

## Run-ID convention (enforced in code)

`--run-id` must match `^[a-z0-9]{3,32}$` — **lowercase alphanumeric characters
only, 3–32 characters**. Uppercase letters, `-`, `_`, `.`, spaces, and
non-ASCII are rejected at USAGE (exit `2`) before any gate runs.

Both disposable database names are derived from it by code, and
`resolve_config()` re-checks the derived names against the identity gates so
the convention and the enforcement cannot drift apart:

- source MySQL database: `twelve09_src_<run-id>`   (must also match `SOURCE_DB_RE`)
- target PostgreSQL database: `twelve09_rehearsal_<run-id>` (must also match `TARGET_DB_RE`)

Because the run-id is lowercase alphanumeric, both derived names are valid
MySQL/PostgreSQL identifiers that **need no backtick or double-quote escaping**
in the bootstrap and restore commands below.

### Legacy enum values can abort an import

The reconciled MySQL enum (see
`alembic/versions/f1a2b3c4d5e6_reconcile_address_order_schema.py`) backfills the
legacy labels `SHIPPED` → `OUT_FOR_DELIVERY` and `DELIVERED` → `COMPLETED`.
The importer's enum allow-lists accept only the current labels, so **any source
row still holding `SHIPPED` or `DELIVERED` is refused** with exit code `5`
(MAPPING) rather than silently remapped. If the restored source reports such
rows, the values must be normalized through an **explicitly approved, separately
reviewed process before any Phase 5E execution** — do not "fix" them ad hoc.

## Placeholders & secret handling

- Connection URLs live only in environment variables for the duration of a
  stage and are **never** written to files, shell history, logs, or reports:
  - `IMPORT_SOURCE_URL`   (disposable MySQL, `mysql+pymysql://…?charset=utf8mb4`)
  - `IMPORT_TARGET_URL`   (disposable PostgreSQL, `postgresql+psycopg2://…`)
  - `SOURCE_IS_DISPOSABLE_MYSQL=1`, `CONFIRM_DISPOSABLE_TARGET=yes`
  - `IMPORT_TEST_TARGET_URL` (only for the PG regression/smoke tests)
- The utility prints only sanitized identities (scheme + host + db), never
  credentials or rows. Below, `${…}` denotes "set from the variable," never a
  literal secret.

### Source charset contract (enforced in code)

`IMPORT_SOURCE_URL` **must** carry `charset=utf8mb4` exactly once in its query
string (case-insensitive). Missing, empty, duplicated, or any other charset is
refused with exit `3` (CONFIG). Without it pyMySQL inherits the server default,
which is how text columns come back as `bytes` and then fail on a PostgreSQL
`VARCHAR` insert.

A configured charset is **not** treated as proof: every extracted text value is
re-checked and a `bytes` value in an ordinary text column aborts the read with
exit `5` (MAPPING), naming only the table and column. Enum and JSON columns keep
their existing byte-tolerant conversions.

### Remote-endpoint overrides — do not set them

- `IMPORT_ALLOW_REMOTE_SOURCE=1` relaxes the loopback requirement on the
  **source**.
- `IMPORT_DISPOSABLE_TARGET_HOST=<host>` allow-lists a non-loopback **target**
  host.

Both must be **absent from the rehearsal shell** unless a separately reviewed
and explicitly authorized need arises. They are not removed, but they are not
part of the normal flow either.

**Name-pattern validation does not prove an endpoint is disposable.** What the
gates prove is: scheme, loopback (or the explicit override), absence of a known
production host/db marker, and an exact `twelve09_src_<run-id>` /
`twelve09_rehearsal_<run-id>` name match. What they **cannot** prove is that
the host and port actually resolve to a throwaway container. That identity must
be verified **out of band** by the operator before execution — confirm the
container was created for this run (`docker ps`), that no other service is
listening on the published port, and that `DATABASE_URL` in the working `backend/.env`
still points at the application's own database and was never edited.

## Prerequisites

- `pip install -r requirements-dev.txt` (adds `pytest`, `httpx`, and pulls the
  production deps incl. `pymysql` + `psycopg2-binary`).
- A container runtime (Docker) for the disposable MySQL + PostgreSQL instances.
- Run Python commands from `backend/` so the `tools` and `app` packages import.

---

## Stage 0 — Identity & integrity (no connections)

1. Record `--run-id` = a unique token matching `^[a-z0-9]{3,32}$`, e.g.
   `RUN=5e20261015a1b2` (date + short suffix, lowercase, no separators).
   The disposable names are derived from it by the tool: source DB
   `twelve09_src_${RUN}`, target DB `twelve09_rehearsal_${RUN}`.
2. Re-verify the backup **without opening it**: size `27186` and SHA-256 match.
3. Confirm the protected backup copy is untouched.

## Stage A — Implementation + unit tests (already done in 5D)

```
cd backend
python -m pytest tests/test_pg_import_unit.py -q          # isolated, no DB
python -m pytest tests/test_pg_target_smoke.py -q         # pure refusal tests run; PG tests skip until IMPORT_TEST_TARGET_URL is set
```

Gates G0–G3, run-id/derived-name validation, Outcome-A mapping (including the
nullable `products.cost_price` and `payments.payment_metadata` cases),
sequence math, the pure reconciliation helpers, and the execution-disabled
safeguards are unit-tested without any database or driver. These suites must not
be pointed at a live database.

Phase 5E.2 adds `tests/test_pg_source_extraction_unit.py`, also database-free
(mocked cursors + synthetic `information_schema` metadata).
Phase 5E.3 adds `tests/test_pg_compat_unit.py`, also database-free (synthetic
rows only; no cursor, driver, or socket).

## Stage B — Disposable MySQL **source** (restore backup)

```
# B1. start throwaway MySQL 8 (example; do NOT touch the live local MySQL)
docker run --rm -d --name t09_src_<RUN> \
  -e MYSQL_ROOT_PASSWORD=<choose> -e MYSQL_DATABASE=twelve09_src_<RUN> \
  -p 127.0.0.1:33060:3306 mysql:8

# B2. restore the approved backup into that database
#     The database name is backtick-quoted so the identifier is passed to the
#     server verbatim; the run-id convention keeps it separator-free anyway.
docker exec -i t09_src_<RUN> mysql -uroot -p<choose> \
  'twelve09_src_<RUN>' < "backups/twelve09_kiddies_store_backup_20261014_155033.sql"

# B3. set the SOURCE config (shell-local only)
#     charset=utf8mb4 is MANDATORY -- it is enforced, not advisory.
export IMPORT_SOURCE_URL="mysql+pymysql://root:<choose>@127.0.0.1:33060/twelve09_src_<RUN>?charset=utf8mb4"
export SOURCE_IS_DISPOSABLE_MYSQL=1
```

Then verify restoration **without printing PII**: compare `SELECT COUNT(*)`
per table against the previously reported reference counts
(users 43, categories 4, delivery_config 1, products 12, addresses 4,
orders 8, order_items 9, payments 5, expenses 2). These are *reference* values;
treat the actual restored source as authoritative.

**Also check for legacy enum values** before going further (see the warning
above): rows in `orders` still holding `SHIPPED` or `DELIVERED` will abort the
import with exit code `5`.

## Stage C — Disposable PostgreSQL 16 **target**

```
docker run --rm -d --name t09_pg_<RUN> \
  -e POSTGRES_PASSWORD=<choose> -e POSTGRES_DB=twelve09_rehearsal_<RUN> \
  -p 127.0.0.1:54321:5432 postgres:16

export IMPORT_TARGET_URL="postgresql+psycopg2://postgres:<choose>@127.0.0.1:54321/twelve09_rehearsal_<RUN>"
export CONFIRM_DISPOSABLE_TARGET=yes
```

## Stage D — Migrate target (PostgreSQL env only)

```
# Override the app's DATABASE_URL for this subprocess only (do NOT edit .env).
DATABASE_URL="$IMPORT_TARGET_URL" alembic -c alembic_pg.ini upgrade head
DATABASE_URL="$IMPORT_TARGET_URL" alembic -c alembic_pg.ini current   # expect: pg_baseline (head)
```

Confirm exactly the nine application tables plus `alembic_version=pg_baseline`.

**This schema migration is a separate operation from any future data-import
transaction.** `pg_baseline` is one `CREATE TABLE`/`CREATE INDEX` migration, so
it is atomic in its own right, but it is **not** part of the import transaction
and is **not** reversed by an import rollback. If it fails, drop/recreate the
disposable PG container and retry.

## Stage E — Import (NOT IMPLEMENTED — execution-disabled build)

```
cd backend
python -m tools.pg_import --run-id <RUN> --dry-run
python -m tools.pg_import --run-id <RUN> --apply
```

> **In the current build both commands exit `4` (GATE) after passing the gates
> and print `[abort] database execution is disabled in this build
> (EXECUTION_ENABLED=False)`. They do not connect, read, write, or truncate
> anything. No import, no transaction, and no sequence reset exists.**

Planned behaviour once Phase 5E is implemented and authorized (do **not** treat
as current facts):

- import order `users → categories → delivery_config → products → addresses →
  orders → order_items → payments → expenses` (parents before children);
- IDs and FK values preserved; the MySQL `alembic_version` table never imported;
- all inserts plus all `setval(...)` inside **one** transaction, so any failure
  rolls the whole load back and a partial import can never be mistaken for
  success;
- sequences set to `max(id)+1` **after** a successful load (helper
  `compute_next_value()` already exists and is unit-tested);
- `--apply --truncate-target` empties the disposable target first. PostgreSQL
  `TRUNCATE ... RESTART IDENTITY CASCADE` is **transactional**: it resets the
  identities immediately and is itself rolled back if the enclosing transaction
  aborts. `TRUNCATE_SQL` exists as a builder only — no code issues it yet.

### What Phase 5E.2 actually added (source side only)

The read-only layer exists and is unit-tested, but is unreachable from the CLI
until the guard is flipped:

- **Charset contract** — `charset=utf8mb4` required exactly once; anything else
  exits `3`.
- **Schema-drift guard** — `assert_source_schema_matches()` reads MySQL
  `information_schema.COLUMNS` with a fully parameterized query over the nine
  approved tables and compares against `TABLES`. It reports missing tables,
  missing columns, unexpected columns/tables, nullability mismatches, wrong
  logical type, wrong `enum(...)` label set, wrong `NUMERIC` precision/scale,
  wrong `VARCHAR(n)` length, and a primary key without `auto_increment`.
  `alembic_version` is the only tolerated extra table. A mismatch exits `3`.
- **Extraction** — `extract_source_rows()` reads only the nine allowlisted
  tables, selects only the approved columns, qualifies them with the validated
  schema name, orders by the primary key, and closes every cursor and the
  connection on success **and** on failure.
- **Text-type audit** — `assert_source_text_types()` rejects `bytes` in ordinary
  text columns (exit `5`); enum/JSON columns keep their byte tolerance.

### What Phase 5E.3 added (pre-import compatibility checks)

`run_compatibility_checks()` runs ten checks over the extracted rows and returns
a deterministic `CompatibilityReport` of `ERROR` (blocking), `WARNING`
(report-only), or `PASS`. Nothing is written, repaired, truncated, or
normalized. Blocking issues exit `5` (MAPPING); warnings exit `0`.

| Check | Blocking? | What it does |
|---|---|---|
| `check_enum_values` | **ERROR** | Rejects `orders.status`, `payments.status`, `orders.fulfillment_method`, `users.role` values the target enum does not accept. `SHIPPED`/`DELIVERED` are reported as *legacy*. Only an approved manifest can import them (decision D-4). |
| `check_string_lengths` | **ERROR** | Rejects values over the target `VARCHAR(n)` limit. Never truncates. |
| `check_unique_duplicates` | **ERROR** | Rejects duplicates on `users.email` / `categories.name`. Never exposes the value. |
| `check_permissions` | **ERROR** | Rejects `users.permissions` members outside the application `Permission` enum (`app/models/user.py:18-25`). Never discards entries. |
| `check_decimal_precision` | **ERROR** | Rejects values that would overflow `NUMERIC(10,2)` or require rounding, reusing `map_decimal()`. Never rounds. |
| `check_required_columns` | **ERROR** | Rejects NULLs in target-NOT-NULL columns, derived from `TableSpec.not_null`. |
| `check_foreign_keys` | **ERROR** | Rejects orphans for every FK in `FK_COLUMNS`. Never repairs. |
| `check_datetimes` | **ERROR** | Rejects aware datetimes, `date` objects, zero dates, and non-datetime values. Outcome A is preserved exactly. |
| `check_non_negative` | **ERROR** | Applies the real target CHECK constraints on `products.stock_quantity` and `products.cost_price`. `expenses.amount` has **no** CHECK, so a negative expense is not an error. |
| `check_business_invariants` | **WARNING** | Report-only per decision D-1: payment amount vs order total, multiple payments per order, item subtotal vs `unit_price × quantity`, order total vs `Σ(subtotals) + delivery_fee`. Decimal arithmetic only; nullable values are skipped, not coerced. |

Every finding carries only a check id, a row count, and at most 20 surrogate
primary-key ids. No monetary value, name, email, address, payment reference, or
metadata appears in any report line, exception message, or log.

**Legacy enum values.** `SHIPPED` → `OUT_FOR_DELIVERY` and
`DELIVERED` → `COMPLETED` are *not* automatic. An approved manifest may be
nominated with `IMPORT_ENUM_NORMALIZATION_MANIFEST=<path-to-json>`; it must map
only real enum columns and only to labels the target enum already accepts. No
manifest ships with this tool, and none exists in the repository — so today
legacy values block the import and must be normalized through an explicitly
approved, separately reviewed process first.

## Stage F — Reconciliation / validation (NOT IMPLEMENTED)

```
python -m tools.pg_import --run-id <RUN> --validate-only
```

> **Also exits `4` (GATE) in this build. No reconciliation query has been
> written.** The pure helpers
> `diff_counts()`, `enum_domain_violations()`, `check_order_amounts()` and
> `check_stock_cost()` exist and are unit-tested, but **they are not connected
> to any execution path** — nothing calls them against a database today.
> Phases 5E.2/5E.3 added only the **source-side** checks listed above; no
> target-side check of any kind exists.

Checks that must exist before Phase 5E can report success (prerequisites, not
current facts): nine-table counts vs the **actual restored source**; PK
uniqueness + preserved IDs; enum-domain membership; FK orphan count = 0;
`stock_quantity >= 0`; `cost_price IS NULL OR >= 0`; NUMERIC(10,2) precision /
no rounding; order rule `total_amount == Σ(order_items.subtotal) +
delivery_fee`; payment/order state consistency; JSON validity/shape; boolean
source domain; NULL/NOT NULL; **timestamp values preserved unchanged**;
sequence state set to `max(id)+1`. All reporting must remain aggregate-only
(counts + surrogate row IDs) — no emails, addresses, payment metadata, or raw
rows.

## Stage G — Regression tests against the disposable PostgreSQL target

```
IMPORT_TEST_TARGET_URL="$IMPORT_TARGET_URL" python -m pytest tests/test_pg_target_smoke.py -q
# and, where pointed at the disposable target, the broader backend suite with
# Paystack mocked (no real transactions).
```

The smoke suite self-guards: if `IMPORT_TEST_TARGET_URL` is unset it skips; if it
is set but not loopback/`twelve09_rehearsal*` it **fails**.

**The six PostgreSQL-native tests (`test_pgnative_*`) require a deliberately
provisioned disposable PostgreSQL 16 instance with
`alembic -c alembic_pg.ini upgrade head` already applied** (the native-ENUM
catalog test reads the real enum types). They exercise behaviour the SQLite
suite cannot: native ENUM labels, NUMERIC(10,2) overflow, FK enforcement,
CHECK enforcement, JSON round-trip, and the post-import SERIAL `setval`
strategy. They run **only** when the variable is set to a disposable target and
must never point anywhere else.

## Stage H — Record results

Persist an aggregate-only report: per-table counts, validation pass/fail counts,
invariant results, sequence values, timestamps preserved = yes/no. No emails,
addresses, payment metadata, or raw rows.

## Stage I — Cleanup (only after results reviewed AND authorized)

```
docker rm -f t09_src_<RUN> t09_pg_<RUN>    # dispose ONLY the rehearsal resources
```

Never target the live local DB, the protected backup, or any production
resource.

## Failure / restart / rollback

- **Gate failure (G0–G3):** abort before any write; fix config/identity.
- **Migration failure:** `pg_baseline` is a single migration (atomic create);
  on failure drop/recreate the disposable PG container and retry. This is
  unrelated to the import transaction.
- **Import/validation failure:** *planned only.* The data path is intended to
  run in ONE transaction (ordered inserts + all `setval` + validation) so that
  any failure rolls it back; **this has not been implemented or verified**.
  Restart by re-running.
- **Repeat run on a non-empty target:** *planned only.* Refused unless
  `--apply --truncate-target` (which still requires every gate + the explicit
  target confirmation). No such pre-check exists yet.
- **DDL is not reversed by a data rollback:** the `alembic_pg` migration
  happens outside the import transaction, so rolling the import back leaves the
  schema in place. To fully reset, drop/recreate the disposable PG container,
  re-run `alembic_pg upgrade head`, then re-import. (PostgreSQL `TRUNCATE ...
  RESTART IDENTITY`, by contrast, *is* transactional and is rolled back with the
  transaction that issued it.)

## Phase 5E.4B transaction design requirements (audited in 5E.4A)

Phase 5E.4A audited the design and added mocked tests; **no transaction logic
exists today**. A future `--apply` implementation MUST satisfy all of the
following before it may be enabled.

1. **One explicit target transaction.** Every `INSERT`, and every `setval(...)`,
   runs inside a single transaction opened on the disposable target connection.
   No autocommit, no per-table commits, no implicit engine-level isolation.
2. **Single rollback path.** Any failure — a `MappingError`, a blocking
   compatibility check, a driver error, a reconciliation mismatch — must raise
   out of the transaction block so the whole load, including all `setval`
   changes and any `TRUNCATE`, is rolled back together. A partial import must
   be impossible.
3. **Commit is last.** The commit happens only after the source-side schema
   guard, the full extraction, every Phase 5E.3 compatibility check, and the
   target-side reconciliation all pass. Reconciliation must run on the same
   connection *before* the commit so its failures are still rolled back.
4. **Resource cleanup is unconditional.** Connection and cursors close in
   `finally` blocks on both success and failure, mirroring
   `extract_source_rows()`.
5. **Sequence resets.** `setval(pg_get_serial_sequence(t,'id'),
   compute_next_value(MAX(id)), false)` per table, executed **inside** the
   transaction after the inserts. Because explicit IDs are inserted and
   `nextval` is never called during the load, the sequence position is fully
   determined by the reset, which is why this is idempotent: re-running the
   whole import on an identical target produces identical sequence state.
6. **Non-empty-target guard before any destructive step.** A second run on a
   non-empty target must be refused unless `--apply --truncate-target` is given.
   The target-emptiness check and the truncate confirmation must both be
   evaluated **before** the `TRUNCATE` executes, never after.
7. **Separate truncate confirmation.** `CONFIRM_DISPOSABLE_TARGET=yes` must not
   be the only token authorizing a destructive `TRUNCATE`. A distinct, explicit
   confirmation is required (decision D-2 — see below).
8. **`--reset-sequences` writes nothing but `setval`.** It must re-verify target
   identity (`target_identity`) on the same invocation before issuing any
   `setval`, and must never insert, truncate, or otherwise mutate data.
9. **Target identity and separation.** `source_identity`, `target_identity`,
   `validate_source_charset`, and the `EXECUTION_ENABLED` guard are all enforced
   in `main()` **before** dispatch, so no future write path can be reached
   without them having passed.
10. **JSON adaptation.** **BLOCKER for 5E.4B.** `users.permissions` and
    `payments.payment_metadata` are PostgreSQL `JSON` columns whose mapped
    values are Python `list`/`dict`. psycopg2's default parameter adaptation
    turns a Python `list` into a PostgreSQL `ARRAY` literal, which fails
    against a `JSON` column. `insert_sql()` currently emits bare `%s`
    placeholders with no JSON handling. The implementation MUST wrap every
    `JSON_COLUMNS` value in `psycopg2.extras.Json(...)` at execution time
    (keeping `map_row()` driver-agnostic). This cannot be verified in the
    current workspace because `psycopg2` is not installed here — it must be
    covered by a disposable-PostgreSQL integration test, not by a mocked one.
11. **Numeric shape.** All nine decimal columns are `NUMERIC(10,2)` in the
    baseline, the models, and the importer's `TableSpec.max_decimal`, and this
    agreement is enforced by an AST-based test (`test_pg_dispatcher_audit_unit.py`)
    so a future per-column change cannot drift silently.
12. **`TRUNCATE_SQL` identifier quoting.** `TRUNCATE_SQL` currently interpolates
    table names unquoted. They come only from `TABLE_ORDER`, so it is safe
    today, but the implementation should build it through `quote_ident()` to
    match `insert_sql()`'s defence-in-depth.

## Phase 5E.4B-1 — mocked transaction harness (what exists, and what it proves)

Phase 5E.4B-1 added the smallest safe foundation for the transaction layer. **It
does not import anything, execute any target SQL, or connect to any database.**

### What exists

| Name | Purpose |
|---|---|
| `open_target_connection(url, *, schema=None)` | The only sanctioned way to obtain a live target connection. **Not implemented** — fails closed while `EXECUTION_ENABLED` is False *and* unconditionally until a reviewed phase implements it. |
| `target_connection_factory(cfg, schema=None)` | Symmetric with `source_connection_factory()`; refuses through `open_target_connection()`. |
| `TargetTransaction(connect, begin, *, label="target")` | A narrow transaction-boundary context manager delegating to an injected connection and an injected `begin` callable. Contains no table names and no SQL text. |

### Guarantees proven **with fakes** (`tests/test_pg_transaction_harness_unit.py`)

- the caller's work runs inside the transaction, so a commit happens only after
  the work and any pre-commit checks succeed;
- `commit()` is called exactly once, and only on the success path;
- `rollback()` is called exactly once on any failure, and only when the
  transaction reports itself active;
- a commit failure **surfaces** as a `Fail(RUNTIME)` and is followed by a
  rollback attempt when the transaction is still active;
- no exception is swallowed or converted into a success;
- the connection is closed on every path, including a failure while opening;
- a cleanup failure never replaces the original failure — the original
  propagates and the cleanup problem is retained as sanitized text on
  `cleanup_errors`;
- a finished or already-open transaction cannot be reused.

### What the harness does **NOT** prove

A mocked fake cannot demonstrate PostgreSQL's real atomicity, durability, or
isolation. The stateful fake in the harness **simulates** the transaction
*contract* — pending writes become visible only on a successful fake commit and
a rollback discards them. Tests that rely on that simulation are labelled
`SIMULATION` in the test file.

**Nothing in this phase is evidence that a real import would be atomic.** Before
any real import is trusted, a disposable-PostgreSQL integration test must
demonstrate, against a real server: a successful load commits; an injected
failure at each stage leaves zero rows; and `setval` leaves the expected
next value.

### Fail-closed properties

- There is **no CLI flag and no environment variable** that activates the seam;
  eight hostile variables were tested and none enable it.
- No CLI mode routes to `TargetTransaction` or `open_target_connection`.
- Construction without a `begin` implementation is inert; opening it raises
  `Fail(GATE)` and still closes the connection.
- Identity gates and the `EXECUTION_ENABLED` guard remain in `main()`, upstream
  of every dispatch path.

### Mandatory future integration test (JSON adaptation)

`users.permissions` and `payments.payment_metadata` are PostgreSQL `JSON`
columns whose mapped values are Python `list`/`dict`. psycopg2's default
parameter adaptation turns a `list` into a PostgreSQL `ARRAY` literal, which
fails against a `JSON` column. `insert_sql()` emits bare `%s` placeholders with
no JSON handling, so a future implementation **must** wrap every
`JSON_COLUMNS` value in `psycopg2.extras.Json(...)` at execution time.

This cannot be verified here because `psycopg2` is not installed in this
workspace. The required disposable-PostgreSQL integration test is: insert
representative `list` and `dict` values into `users.permissions` and
`payments.payment_metadata`, read them back, and assert both the Python types
and the contents. Until that passes, JSON adaptation is an **unverified
prerequisite**, not a solved problem.

## Phase 5E.4B-2A — disposable PostgreSQL verification (evidence obtained)

Phase 5E.4B-2A stood up a disposable local PostgreSQL 16 server and collected
**real** server evidence. The container was disposed of afterwards; nothing
below remains running.

**Server actually used:** PostgreSQL **16.15** (`server_version_num` = `160015`),
image `postgres:16-alpine` — chosen because it was already present locally, so
no image download was needed. Docker Desktop was installed but its daemon was
stopped; it was started for this phase and stopped again afterwards.

**How to reproduce (loopback-only, disposable):**

```
# pick a port OUTSIDE the Windows reserved ranges (50000-50059, 55308-55807);
# a port inside those ranges fails with "socket access permissions forbidden"
PORT=44532
DB=twelve09_rehearsal_<runid>            # must match TARGET_DB_RE
docker run -d --name t09_pg_<runid> \
  -e POSTGRES_DB="$DB" -e POSTGRES_PASSWORD="<random>" \
  -p 127.0.0.1:$PORT:5432 postgres:16-alpine
# verify inside the container (no host package needed):
docker exec -i t09_pg_<runid> psql -U postgres -d "$DB"
```

**Verified results (actual, not assertions):**

| Check | Result |
|---|---|
| Server version | `16.15` / `160015` |
| Intended database | `twelve09_rehearsal_p5e4b2a` (the only user DB on that server) |
| Host isolation | published as `127.0.0.1:44532` only; unreachable from `172.25.48.1` and the `169.254.*` interfaces |
| Transaction commit | `BEGIN` → insert 1 row → `COMMIT` → row present |
| Transaction rollback | `BEGIN` → insert 1 row → `ROLLBACK` → row **absent**; committed rows unaffected |
| JSON list round-trip | `json_typeof` = `array`, `json_array_length` = `2`, value returned as `["MANAGE_ORDERS","VIEW_REPORTS"]` — structure preserved |
| JSON dict round-trip | `json_typeof` = `object`, keys `channel,retry`, `channel` = `card`, `retry` = `3` — structure preserved |
| Sequence, non-empty | `MAX(id)` = 4 → `setval(..., 5, false)` → `nextval` = `5` |
| Sequence, empty | `TRUNCATE ... RESTART IDENTITY` → `MAX(id)` = none → `setval(..., 1, false)` → `nextval` = `1` |
| Synthetic data only | the database contained exactly one table (`p5e4b2a_items`) with **0 rows** at teardown |

These results confirm the *server-side* contract the mocked harness simulates.
They do **not** confirm the importer: `psycopg2` is not installed in this
workspace, so no importer code has ever connected to a real PostgreSQL. That
remains Phase 5E.4B-2.

Also verified with that live server present: `open_target_connection()` refused
with `Fail(GATE)` and attempted **zero** network connections; all four CLI modes
exited `4` with a real target URL; `sanitize_url()` redacted the credential; the
disposable name was accepted by `target_identity()` while
`twelve09_rehearsal_prod`, `twelve09_rehearsal_live`, `twelve09_kiddies_store`,
and `some_random_db` were all refused.

## Phase 5E prerequisites (do not skip)

Status as of Phase 5E.4A:

| # | Prerequisite | Status |
|---|---|---|
| 1 | Source charset contract (`charset=utf8mb4`) | **done** — Phase 5E.2 |
| 2 | Source schema-drift guard over `information_schema` | **done** — Phase 5E.2 |
| 3 | Allowlisted schema-qualified extraction, ordered by PK | **done** — Phase 5E.2 |
| 4 | Text-vs-bytes audit on extracted values | **done** — Phase 5E.2 |
| 5 | Pre-import compatibility checks (legacy enums, `VARCHAR(n)`, unique duplicates, permissions members, decimal scale, NULL in required columns, FK orphans, datetimes, target CHECKs) | **done** — Phase 5E.3 |
| 6 | Report-only business invariants (payment/order consistency) | **done** — Phase 5E.3 |
| 7 | Dispatcher routing, CLI safety, and target-schema agreement verified by audit | **done** — Phase 5E.4A |
| 8 | Injectable target-connection seam + narrow transaction boundary, proven with fakes | **done** — Phase 5E.4B-1 |
| 9 | Real disposable-PostgreSQL transaction evidence (success commits; each injected failure stage leaves zero rows; `setval` next value) | **outstanding** |
| 10 | JSON adaptation (`psycopg2.extras.Json` for `users.permissions`, `payments.payment_metadata`) + its integration test | **outstanding** |
| 7 | `_dispatch_db_mode()`: read source → ordered insert → `setval` → commit, in one transaction, with a single rollback on any failure | outstanding |
| 8 | The reconciliation **queries** themselves (counts, PK uniqueness, FK orphans, order-total rule, payment/order consistency, JSON shape, NULL/NOT NULL, timestamp equality, sequence state) — today only the pure helpers exist | outstanding |
| 9 | `--truncate-target` repeat-run pre-check (refuse a non-empty target unless truncation is explicitly confirmed) | outstanding (decision D-2) |
| 10 | Connection of the existing pure helpers to that execution path | outstanding |
| 11 | An explicit authorization step that flips `EXECUTION_ENABLED` in source | outstanding |

## Explicitly out of scope

No Supabase/Render provisioning, no committing/pushing, no editing the MySQL
Alembic chain, no reading/altering the backups, no executing `audit_query.py`,
and no enabling `EXECUTION_ENABLED` in this phase.
