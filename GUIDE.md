# DeepSentinel Query Runner — complete guide

For team members and for agents working on this repository.

**Repository:** `github.com/LEXES7/DeepSentinel-Runner`
**Runs locally.** No server to deploy, no build step, no Node.

---

## Contents

1. [What this is and why it exists](#1-what-this-is-and-why-it-exists)
2. [Install and run](#2-install-and-run)
3. [Configuration](#3-configuration)
4. [The critical requirement: one shared database](#4-the-critical-requirement-one-shared-database)
5. [The three tables](#5-the-three-tables)
6. [Using the interface](#6-using-the-interface)
7. [How column matching works](#7-how-column-matching-works)
8. [How replay works](#8-how-replay-works)
9. [Concurrency and claiming](#9-concurrency-and-claiming)
10. [How it connects to the detection platform](#10-how-it-connects-to-the-detection-platform)
11. [API reference](#11-api-reference)
12. [Troubleshooting](#12-troubleshooting)
13. [Design decisions](#13-design-decisions)

---

## 1. What this is and why it exists

DeepSentinel monitors transactions continuously. In production a bank would push
transactions to us over an API. **We have no bank and no client API**, so we need
something that stands in for that feed using our own dataset.

The Query Runner is that stand-in. You give it a CSV or Excel file of
transactions and it writes them into the platform database **one row at a time,
at a controlled rate**, exactly as if they were arriving from a live source.

```
CSV / Excel  →  Query Runner  →  database  →  monitor screens each row
                                              →  models score it
                                              →  suspicious ones escalate
```

### Why not just bulk-load the file

A bulk `INSERT` of 50,000 rows would prove the pipeline *works*. It would not
demonstrate that it works **continuously**, which is the claim the monitoring
dashboard makes. Rows arriving at a steady rate is what makes the live monitor
meaningful rather than decorative.

---

## 2. Install and run

Requires **Python 3.10+** and **Poetry**.

```bash
git clone https://github.com/LEXES7/DeepSentinel-Runner.git
cd DeepSentinel-Runner

poetry install
cp config.example.ini config.ini        # then edit, or use the Settings panel

poetry run python run.py
```

It prints a URL and opens your browser at **http://127.0.0.1:8600**.

```
  DeepSentinel Query Runner  →  http://127.0.0.1:8600
```

### Notes

- **Poetry only.** There is no `requirements.txt` by design — one dependency
  source avoids the two drifting apart.
- The PostgreSQL driver is **pg8000**, which is pure Python. `poetry install`
  never needs a C compiler, which matters across Windows, macOS and Linux.
- It binds to `127.0.0.1` only. It holds a database password and writes to a
  shared database, so it is not reachable from the network by default.
- To change the port: `QUERY_RUNNER_PORT=8700 poetry run python run.py`
- To stop it opening a browser: `QUERY_RUNNER_NO_BROWSER=1`

---

## 3. Configuration

Everything lives in **`config.ini`** in the repository root. It is **gitignored**
because it holds a database password — never commit it.

The Settings panel in the interface writes to the same file, so you can use
either.

```ini
[DATABASE]
kind = sqlite                    ; sqlite | postgres

; used when kind = sqlite
sqlite_path = ./deepsentinel_runner.db

; used when kind = postgres
host =
port = 5432
name =
user =
password =
sslmode = require

[REPLAY]
rows_per_second = 5
batch_size = 50
watch_folder =
```

### `[DATABASE]`

| Key | Meaning |
|---|---|
| `kind` | `sqlite` for a local file, `postgres` for Neon or any server |
| `sqlite_path` | Path to the database file. **Point this at the platform's database** — see §4 |
| `host` / `port` / `name` | Server connection details |
| `user` / `password` | Credentials. The password is never sent back to the browser |

**Editing the password from the interface:** leaving the field blank means
"leave it unchanged", not "clear it". A blank submit would otherwise wipe a
working credential every time you changed an unrelated setting.

### `[REPLAY]`

| Key | Meaning |
|---|---|
| `rows_per_second` | How fast rows enter the queue. 5 is a good demo pace |
| `batch_size` | Rows read from the file at a time |
| `watch_folder` | A shared folder — typically Drive for Desktop. Files there are read **in place**, so a large extract does not have to be uploaded by each person |

**Drive for Desktop paths:**

```
macOS    /Users/you/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive/DeepSentinel
Windows  G:\My Drive\DeepSentinel
```

---

## 4. The critical requirement: one shared database

> **The Query Runner and the fusion engine must point at the same database.**

The Query Runner writes transactions into `transactions_live`. The monitor in
the fusion engine reads from that same table. If they point at different
databases, the monitor finds nothing, logs that the table is missing, and falls
back to its sample source — so it *looks* like it is working while screening
nothing you ingested.

### Local development

Point the Query Runner at the fusion engine's SQLite file:

```ini
[DATABASE]
kind = sqlite
sqlite_path = /path/to/R26-IT-121/fusion_engine/DeepSentinel/deepsentinel.db
```

The fusion engine's `.env` should have:

```bash
DATABASE_URL=sqlite+aiosqlite:///./deepsentinel.db
```

### Shared / deployed

Both point at Neon:

```ini
[DATABASE]
kind = postgres
host = ep-xxxx-pooler.<region>.aws.neon.tech
name = neondb
user = neondb_owner
password = <ask the team lead>
```

```bash
DATABASE_URL=postgresql+asyncpg://neondb_owner:<pw>@ep-xxxx-pooler.../neondb
```

### How to check they match

In the monitor's state (`GET /api/monitor/state` on the fusion engine):

```json
{ "source": "queue",  "queue": { "available": true, "pending": 42 } }
```

- `"available": true` — the monitor can see the queue. Correct.
- `"available": false` — different databases, or tables not created.
- `"source": "sample"` — the queue is empty or unreachable; it is replaying
  samples, not your data.

---

## 5. The three tables

Press **Create tables** in the interface, or `POST /api/database/create-tables`.
It only ever creates what is missing — it never alters or drops an existing
table.

### `transactions_archive` — the evidence record

Every row ever received, **written once and never modified**.

| Column | Purpose |
|---|---|
| `transaction_id`, `business_date` | Identity and the day it belongs to |
| `step`, `tx_type`, `amount`, `name_orig`, `name_dest`, balances | The transaction |
| `is_fraud`, `is_flagged_fraud` | Ground truth, when the file carried it |
| `raw` | The original row whole, as JSON |
| `source_file`, `uploaded_by`, `uploaded_at` | Provenance |

**Why `raw` exists:** a column nobody anticipated is not lost on the way in.

**Why provenance matters:** when a result is questioned months later, the first
question is which file it came from and when.

### `transactions_live` — the work queue

Rows the detectors consume. **Rows change state here.**

| Column | Purpose |
|---|---|
| `transaction_id` | **UNIQUE** — this is what makes re-ingestion idempotent |
| `payload` | The transaction already shaped for the detector APIs |
| `status` | `pending` → `claimed` → `screened` \| `failed` |
| `claimed_by`, `claimed_at`, `attempts` | Which worker holds it, and since when |
| `escalated`, `screened_at`, `last_error` | Outcome |

Indexed on `(status, received_at)` — the ordering the claim query uses.

### `fraud_cases` — what was caught, and why

Written by the **fusion engine**, not by this tool. Defined here because the
schema belongs with the rest of the database.

This is the table a stakeholder reads. For one alert:

| Group | Columns |
|---|---|
| What | `case_ref`, `detected_at`, `classification`, `fused_score` |
| Why | `graph_score`, `behavioral_score`, `temporal_score`, and **`*_available`** for each |
| Honesty | `modalities_used`, `uncertainty_penalty_applied` |
| Evidence | `typology_name`, `graph_pattern`, `sink_account`, `implicated_accounts`, `graph_evidence`, `forensic_report` |
| Timing | `screening_ms`, `total_ms` |
| Follow-up | `alert_sent`, `recipients`, `review_status`, `reviewed_by`, `review_note` |
| Measurement | `label_is_fraud` — ground truth, **never** shown as a model output |

**Why `*_available` matters:** a confidence built from one detector is not the
same claim as one built from three. Without this the number is uninterpretable
six months later.

### Why archive and queue are separate tables

They answer different questions and have different lifetimes. The archive
records that a row *arrived* and never changes. The queue changes constantly as
workers claim and screen rows.

Combining them would mean the evidence record mutates every time a worker
touches it — which is exactly what evidence must not do.

---

## 6. Using the interface

### Panel 1 — Database

1. Choose **SQLite** or **PostgreSQL**
2. Fill in the details
3. **Save** → **Test connection** → **Create tables**

The pill at the top right shows `connected` or `not connected` at all times.

### Panel 2 — Replay

Set `rows_per_second`, the business date, and optionally a shared folder.
**List files in folder** shows spreadsheets in that folder with a **Use** button
each.

### Panel 3 — Transaction file

Drag a file onto the drop area, or click to browse. A **preview** appears
immediately:

```
rows  50     columns 12     mapped 11     missing 0
```

Expand **Column mapping** to see exactly which file column became which field.
**Nothing has been written at this point.** If required columns are missing the
Start button stays disabled and the preview says which ones.

Press **Start replay**.

### Panel 4 — Progress

Live counters — inserted, duplicates, errors, rows/sec — with a progress bar.
**Stop** halts cleanly; rows already inserted stay.

### Panel 5 — Tables

Row counts for all three tables plus the live queue broken down by status.
**Recent cases** lists what the models have caught.

---

## 7. How column matching works

Column names are matched **case-insensitively, ignoring spaces and underscores**.
So `nameOrig`, `name_orig`, `Name Orig` and `NAMEORIG` all land in the same
field.

| Field | Recognised names |
|---|---|
| `step` | step, hour, timestep |
| `tx_type` | type, txType, transactionType |
| `amount` | amount, amt, value |
| `name_orig` | nameOrig, origin, originAccount, fromAccount, sender |
| `name_dest` | nameDest, destination, destAccount, toAccount, receiver |
| `old_balance_orig` | oldbalanceOrg, oldBalanceOrig, originBalanceBefore |
| `new_balance_orig` | newbalanceOrig, originBalanceAfter |
| `old_balance_dest` | oldbalanceDest, destBalanceBefore |
| `new_balance_dest` | newbalanceDest, destBalanceAfter |
| `is_fraud` | isFraud, fraud, label |
| `transaction_id` | transactionId, txId, id, reference |

**Four are required:** `tx_type`, `amount`, `name_orig`, `name_dest`. Without
them the detectors cannot score anything, so the runner refuses to start.

### If the file has no identifier column

One is derived from the filename and row number, e.g.
`sample_50_transactions-00000017`. That identifier is **stable across re-runs**,
which is what makes re-uploading the same file a no-op rather than a double
load.

---

## 8. How replay works

For each row, in this order:

1. **Write to `transactions_archive`** — the permanent evidence record
2. **Insert into `transactions_live`** as `pending` — the work queue
3. **Wait** `1 / rows_per_second` seconds

### Why the archive is written first

If the process dies between the two writes, the result is a row that was
*received but not yet queued* — recoverable. The other order would give a
screened transaction with **no record of it having arrived**, which is worse.

### Re-uploading the same file

`transaction_id` is UNIQUE in the live table, so:

```
inserted 0    duplicates 200    errors 0
```

The archive records **both** uploads (it is a record of receipt), the queue
records the transaction **once** (it is a record of work). That asymmetry is
deliberate.

### Only one replay at a time

Two concurrent replays would interleave rows from different files and make
throughput numbers meaningless. Starting a second returns `409`.

### Expected rates

| Database | Observed |
|---|---|
| Local SQLite | ~25 rows/sec |
| Neon (ap-southeast-1) | **~1.2 rows/sec** |

Each row costs two round trips, so a remote database is dominated by network
latency. Fine for a demo — arriving traffic *should* be a trickle — but loading
a large extract into Neon is slow.

---

## 9. Concurrency and claiming

The problem: two workers both `SELECT` the pending rows and both process them.

### On PostgreSQL — solved properly

```sql
UPDATE transactions_live SET status='claimed', claimed_by=:w, claimed_at=now()
WHERE id IN (
    SELECT id FROM transactions_live WHERE status='pending'
    ORDER BY received_at LIMIT :n
    FOR UPDATE SKIP LOCKED
)
RETURNING id, transaction_id, payload;
```

`SKIP LOCKED` means a worker that meets a row another worker already holds
**steps over it** rather than waiting. Two workers get disjoint batches and
neither blocks.

**Measured:** 5 workers × 10 rows → 50 claimed, 50 unique, **0 collisions**.

### On SQLite — partially

SQLite has no `SKIP LOCKED` and allows one writer at a time. A process-level
lock makes claiming safe **for threads within one process**.

**Measured without the lock:** 4 workers × 15 rows → 60 claimed, **only 15
unique**. Every row was claimed twice.

**With the lock:** 60 claimed, 60 unique, 0 collisions.

> The lock does nothing across *processes*. Two workers, or two machines, or two
> Azure replicas genuinely require PostgreSQL. This is not a preference — SQLite
> cannot express the operation.

### If a worker dies

Its rows stay `claimed` forever — silently never screened, the worst failure
this system can have. **Release stalled rows** returns anything claimed longer
than the timeout back to `pending`.

The fusion engine's monitor does this automatically.

---

## 10. How it connects to the detection platform

```
   Query Runner                          Fusion engine (monitor)
   ────────────                          ───────────────────────
   read CSV/Excel
        ↓
   transactions_archive  ← evidence
        ↓
   transactions_live  ─────────────────→  claim(batch)
        (pending)                              ↓
                                          GraphSAGE screens  :8002
                                               ↓
                                          above threshold?
                                               ↓ yes
                                          Behavioural :8001
                                          Temporal    :8003
                                               ↓
                                          Fusion → typology → report
                                               ↓
   fraud_cases  ←───────────────────────  record the case
                                               ↓
                                          email + web app
```

The monitor publishes which source it is using:

```json
{ "source": "queue" }      // screening your ingested transactions
{ "source": "sample" }     // queue empty — replaying samples
```

A dashboard showing throughput without saying which would invite the reader to
assume the former.

### Service ports

| Port | Service |
|---|---|
| 8001 | Behavioural detector (VAE + DSAA) |
| 8002 | GraphSAGE relational detector |
| 8003 | Temporal detector (TS-TCN) |
| 8090 | Fusion engine backend |
| 8600 | **Query Runner** |
| 5173 | Web app |

---

## 11. API reference

Everything the interface does is available over HTTP. Useful for scripting and
for agents.

Base URL: `http://127.0.0.1:8600`

### Settings

```http
GET  /api/settings
PUT  /api/settings
     { "database": { "kind": "postgres", "host": "...", ... },
       "replay":   { "rows_per_second": 5 } }
```

The password is never returned. Sending an empty password leaves it unchanged.

### Database

```http
POST /api/database/test              → { ok, message }
POST /api/database/create-tables     → { created: [...], existing: [...] }
GET  /api/database/status            → { ok, describe, counts }
```

### Files

```http
GET  /api/files                      → files in the configured shared folder
POST /api/preview                    multipart: file=@x.csv  OR  path=/abs/path
     → { rows, headers, mapped, missing_required, sample, ready }
```

`preview` writes nothing. Always call it before `replay/start`.

### Replay

```http
POST /api/replay/start               multipart: file=@x.csv | path=...
                                                business_date=YYYY-MM-DD
GET  /api/replay/status              → { state, inserted, duplicates, errors,
                                          percent, rows_per_second }
POST /api/replay/stop
```

`state` is one of `idle`, `running`, `stopping`, `done`, `error`.

### Queue and cases

```http
POST /api/queue/release-stale?older_than_seconds=120
GET  /api/cases?limit=25
```

### Example — scripted ingest

```bash
BASE=http://127.0.0.1:8600
curl -s -X POST $BASE/api/database/create-tables
curl -s -X POST $BASE/api/preview -F "file=@data.csv" | jq '{rows, ready, missing_required}'
curl -s -X POST $BASE/api/replay/start -F "file=@data.csv" -F "business_date=2026-08-26"
until curl -s $BASE/api/replay/status | jq -e '.state=="done"' >/dev/null; do sleep 2; done
curl -s $BASE/api/database/status | jq '.counts'
```

---

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `not connected` pill | Press **Test connection** — the message names the cause. Common: wrong password, host typo, firewall |
| `Install the PostgreSQL driver` | `poetry install` did not complete, or you ran outside Poetry. `poetry add pg8000` |
| `Reading Excel needs openpyxl` | `poetry add openpyxl`, or save the file as CSV |
| `The file is missing columns` | The preview lists which. Rename them in the file, or check §7 for accepted aliases |
| Replay starts then errors immediately | Tables not created. Press **Create tables** |
| `409 A replay is already running` | One at a time. Press **Stop** first |
| All rows come back as duplicates | Correct — this file was already ingested. Idempotency working |
| Replay very slow | Remote database. ~1.2 rows/sec on Neon is expected — see §8 |
| Monitor shows `source: sample` | The queue is empty, or the two are on different databases — see §4 |
| Monitor shows `queue.available: false` | Tables not created in the database the *fusion engine* uses |
| Rows stuck in `claimed` | A worker died. **Release stalled rows** |

### Reading the log

The app logs to the terminal it was started from. Useful lines:

```
Ready in 0.04s - strata [...]            a detector started
Ingestion queue found                    the monitor can see transactions_live
No transactions_live table in this database   the two are not sharing
```

---

## 13. Design decisions

| Decision | Reason |
|---|---|
| Rows at a rate, not a bulk insert | Proves continuous operation, not just that a load works |
| Archive written before queue | A crash leaves a received-but-unqueued row, which is recoverable. The reverse loses the record of arrival |
| Separate archive and queue tables | Evidence must not mutate; a queue must |
| `UNIQUE(transaction_id)` | Makes re-ingestion a no-op, so the operation is safe to retry |
| Ground truth never in `payload` | A detector must never see the label. It stays in the archive for measuring afterwards |
| Single HTML page, no build step | Four people on three operating systems must be able to run it with one command |
| pg8000 over psycopg2 | Pure Python — `poetry install` never needs a compiler |
| Bind to 127.0.0.1 | It holds credentials and writes to a shared database |
| Only one replay at a time | Concurrent replays make throughput meaningless |
| `config.ini` gitignored, `.example` committed | The shape is shared; the password is not |

---

## Quick reference

```bash
# run
poetry run python run.py                   # → http://127.0.0.1:8600

# a 50-row test file lives in the repo
samples/sample_50_transactions.csv         # 50 rows, 12 labelled fraud

# check the monitor is reading your data (fusion engine)
curl -s localhost:8090/api/monitor/state | jq '{source, queue}'
```

**If you change one thing, make it §4** — both sides must share a database, or
the monitor screens samples while looking like it is screening your file.
