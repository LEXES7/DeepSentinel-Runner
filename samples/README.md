# Sample files

## `sample_50_transactions.csv`

50 real PaySim transactions for testing the Query Runner end to end.

```
rows          50
known fraud   12  (isFraud = 1)
types         39 CASH_OUT, 11 TRANSFER
```

### Why these particular rows

Every account in this file **exists in the trained graph**, so GraphSAGE can
actually score all 50. A file of invented account names would return 404 for
every row and demonstrate nothing except that the plumbing is connected.

The fraud rate here is 24%, far above PaySim's real 0.13%. That is deliberate:
at the true base rate a 50-row sample would contain no fraud at all and show
nothing happening. **Do not quote 24% as a detection rate** — it is a property
of this file, chosen so the demo has something to detect.

### What it produces

Scored against the graph detector:

```
scored          50/50      no 404s
risk levels     2 HIGH, 2 MEDIUM, 46 LOW
```

The four highest-scoring rows are all labelled fraud:

| score | level | isFraud | destination |
|---|---|---|---|
| 0.3360 | HIGH | 1 | C88316037 |
| 0.2857 | HIGH | 1 | C1345313578 |
| 0.1830 | MEDIUM | 1 | C183532439 |
| 0.1078 | MEDIUM | 1 | C290559240 |

Twelve rows are labelled fraud but only four rank near the top, which is what a
recall of ~0.4 looks like in practice. That is the honest picture, and a better
thing to show a panel than a cherry-picked file where everything works.

### Using it

1. Start the Query Runner: `poetry run python run.py`
2. Set the database and press **Create tables**
3. Drop this file on the upload area
4. Check the column mapping in the preview, then **Start replay**

At the default 5 rows/second it takes about 10 seconds on a local database.
Against a remote database such as Neon expect closer to a minute — each row
costs two network round trips.

Re-uploading it a second time inserts nothing and reports 50 duplicates. That
is the idempotency guard working, not a failure.

### The `isFraud` column

Ground truth, carried through to `transactions_archive.is_fraud` so detection
accuracy can be measured after the fact. It is **never** passed to a model and
never shown as a system output.
