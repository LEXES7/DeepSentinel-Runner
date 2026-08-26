# DeepSentinel Query Runner

Replays a transaction file into the DeepSentinel platform database so the
detection models see arriving traffic rather than a bulk load.

Runs locally on any laptop. No build step, no server to deploy.

> **New here? Read [GUIDE.md](GUIDE.md)** — the complete A-to-Z, including the
> one requirement that catches everyone: the Query Runner and the fusion engine
> must point at the same database.

```bash
poetry install
cp config.example.ini config.ini      # then edit, or use the Settings panel
poetry run python run.py              # opens http://127.0.0.1:8600
```

---

## What it does

Upload a CSV or Excel file — or point it at a shared Drive folder — and each
row is written to two places:

| Table | Purpose |
|---|---|
| `transactions_archive` | Every row ever received, exactly as it arrived. Written once, never updated. |
| `transactions_live` | The queue the detection models consume. Rows change state as they are screened. |
| `fraud_cases` | What the models caught, and why. Written by the platform, readable here. |

### Why the archive and the queue are separate

They answer different questions and have different lifetimes. The archive is
evidence: it records that a row was received and is never modified afterwards.
The live table is a work queue — rows change state as workers claim and screen
them, and processed rows can eventually be pruned.

Keeping both in one table would mean the evidence record mutates every time a
worker touches it, which is exactly what evidence must not do.

### Rows arrive at a rate, not all at once

`rows_per_second` controls how fast rows enter the live queue. A file loaded in
one transaction would prove the pipeline *works*; it would not demonstrate that
it works *continuously*, which is the claim the monitoring dashboard makes.

---

## Configuration

Everything lives in `config.ini`, which is gitignored because it holds a
database password. The Settings panel writes to the same file.

```ini
[DATABASE]
kind = sqlite            ; or: postgres
sqlite_path = ./deepsentinel_runner.db

host =                   ; for postgres — e.g. ep-xxx.aws.neon.tech
port = 5432
name =
user =
password =

[REPLAY]
rows_per_second = 5
batch_size = 50
watch_folder =           ; a Drive for Desktop path, read in place
```

**SQLite** is fine for one person trying things out. **PostgreSQL is required**
if more than one person, or more than one worker, uses the database at the same
time — see Concurrency below.

---

## Column matching

Column names are matched case-insensitively, ignoring spaces and underscores,
so `nameOrig`, `name_orig` and `Name Orig` all land in the same field. PaySim's
own names work as-is, and common alternatives (`sender`, `from_account`,
`amt`, …) are recognised too.

Four columns are required: transaction type, amount, originating account and
destination account. The preview shows exactly what matched before anything is
written, and refuses to start if any are missing.

If the file has no identifier column, one is derived from the filename and row
number. That identifier is stable across re-runs, which is what makes
re-uploading the same file a no-op instead of a double load.

---

## Concurrency

`transactions_live` carries `status`, `claimed_by` and `claimed_at` so several
workers can draw from the same queue without processing a row twice.

On PostgreSQL the claim uses `FOR UPDATE SKIP LOCKED`: a worker that meets a row
another worker already holds steps over it rather than waiting, so two workers
never take the same transaction and neither blocks.

```sql
UPDATE transactions_live SET status='claimed', claimed_by=:w, claimed_at=now()
WHERE id IN (
    SELECT id FROM transactions_live WHERE status='pending'
    ORDER BY received_at LIMIT :n
    FOR UPDATE SKIP LOCKED
)
RETURNING id, transaction_id, payload;
```

**SQLite has no such clause** and allows only one writer at a time, so the
fallback there is a plain `UPDATE`. That is correct for a single worker and is
*not* safe for several — which is the real reason a multi-worker deployment
needs PostgreSQL.

If a worker dies mid-batch its rows would stay `claimed` forever — silently
unscreened, the worst failure this system can have. **Release stalled rows**
returns anything claimed longer than two minutes ago back to `pending`.

---

## Table reference

### `transactions_archive`
Immutable. Carries `source_file` and `uploaded_at` because when a result is
questioned later, the first question is which file it came from and when. The
original row is also kept whole in `raw`, so a column nobody anticipated is not
lost on the way in.

### `transactions_live`
The queue. `UNIQUE(transaction_id)` makes re-ingestion idempotent. Indexed on
`(status, received_at)` — the ordering the claim query uses.

### `fraud_cases`
The table a stakeholder actually reads. For one alert it records: which models
contributed and what each scored, whether any were unavailable and whether an
uncertainty penalty was applied, the matched typology, the structural and
behavioural evidence, detection and total latency, who was notified, and what a
human decided afterwards.

`label_is_fraud` holds ground truth when the source file had it — for measuring
precision after the fact. It is never shown as a model output.

---

## Notes

- Binds to `127.0.0.1` only. It holds credentials and writes to a shared
  database, so it is not reachable from the network by default.
- Only one replay runs at a time. Two concurrent replays would interleave rows
  from different files and make throughput meaningless.
- The archive write happens before the queue write. If the process dies between
  them the result is a row received but not yet queued, which is recoverable —
  the other order would give a screened transaction with no record of arrival.
