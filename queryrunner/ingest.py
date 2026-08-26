"""Reading a file and replaying it into the database as if it were live traffic.

Two things happen per row, and the order matters:

  1. it is appended to `transactions_archive` — the permanent evidence record;
  2. it is inserted into `transactions_live` as `pending`, where the detection
     models pick it up.

The archive write comes first. If the process dies between the two, the result
is a row that was received but not yet queued, which is recoverable. The other
order would give a screened transaction with no record of having arrived.

Replay runs on a background thread at a configurable rate so the monitor sees
arriving traffic rather than one bulk insert. A file loaded all at once would
show the pipeline working but would not demonstrate that it works
*continuously*, which is the claim being made.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from queryrunner import config
from queryrunner.db import get_engine, get_session
from queryrunner.models import TransactionArchive, TransactionLive

logger = logging.getLogger(__name__)


class _NullGuard:
    """No-op stand-in so the claim path reads identically for both engines."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

# PaySim's column names, and the aliases real extracts tend to use. Matching is
# case-insensitive and ignores spaces and underscores, so "Name Orig",
# "name_orig" and "nameOrig" all land in the same place.
COLUMN_ALIASES = {
    "step": ["step", "hour", "timestep"],
    "tx_type": ["type", "txtype", "transactiontype"],
    "amount": ["amount", "amt", "value"],
    "name_orig": ["nameorig", "origin", "originaccount", "fromaccount", "sender"],
    "name_dest": ["namedest", "destination", "destaccount", "toaccount", "receiver"],
    "old_balance_orig": ["oldbalanceorg", "oldbalanceorig", "originbalancebefore"],
    "new_balance_orig": ["newbalanceorig", "originbalanceafter"],
    "old_balance_dest": ["oldbalancedest", "destbalancebefore"],
    "new_balance_dest": ["newbalancedest", "destbalanceafter"],
    "is_fraud": ["isfraud", "fraud", "label"],
    "is_flagged_fraud": ["isflaggedfraud", "flagged"],
    "transaction_id": ["transactionid", "txid", "id", "reference"],
}

ACCEPTED_SUFFIXES = {".csv", ".xlsx", ".xls"}


def _norm(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def map_columns(headers: list[str]) -> dict[str, str]:
    """Map our field names onto whatever the file actually calls them."""
    lookup = {_norm(h): h for h in headers}
    mapping: dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapping[field] = lookup[alias]
                break
    return mapping


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        import math

        f = float(value)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _flag(value) -> Optional[bool]:
    if value is None or value == "":
        return None
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "t"}:
        return True
    if s in {"0", "false", "no", "n", "f"}:
        return False
    return None


def read_rows(path: Path) -> tuple[list[dict], list[str]]:
    """Read a CSV or Excel file into plain dicts, plus its headers."""
    suffix = path.suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES:
        raise ValueError(
            f"{suffix or 'that file type'} is not supported — upload a .csv, .xlsx or .xls file."
        )

    import pandas as pd

    if suffix == ".csv":
        df = pd.read_csv(path)
    else:
        try:
            df = pd.read_excel(path)
        except ImportError as exc:
            raise ValueError(
                "Reading Excel needs the openpyxl package:  poetry add openpyxl"
            ) from exc

    df = df.where(pd.notnull(df), None)
    return df.to_dict("records"), [str(c) for c in df.columns]


def to_records(
    rows: list[dict], mapping: dict[str, str], source_file: str, business_date: str
) -> Iterator[dict]:
    """Turn raw file rows into the shape both tables expect."""
    for i, row in enumerate(rows):
        def g(field):
            col = mapping.get(field)
            return row.get(col) if col else None

        # A file without its own identifier still needs one that is stable
        # across a re-run of the same file, so re-uploading is a no-op rather
        # than a duplicate load.
        tid = g("transaction_id")
        if tid in (None, ""):
            tid = f"{Path(source_file).stem}-{i:08d}"

        yield {
            "transaction_id": str(tid),
            "business_date": business_date,
            "step": int(_num(g("step")) or 0) or None,
            "tx_type": (str(g("tx_type")).upper() if g("tx_type") else None),
            "amount": _num(g("amount")),
            "name_orig": (str(g("name_orig")) if g("name_orig") else None),
            "name_dest": (str(g("name_dest")) if g("name_dest") else None),
            "old_balance_orig": _num(g("old_balance_orig")),
            "new_balance_orig": _num(g("new_balance_orig")),
            "old_balance_dest": _num(g("old_balance_dest")),
            "new_balance_dest": _num(g("new_balance_dest")),
            "is_fraud": _flag(g("is_fraud")),
            "is_flagged_fraud": _flag(g("is_flagged_fraud")),
            "raw": {str(k): (None if v is None else str(v)) for k, v in row.items()},
            "source_file": source_file,
        }


def _payload(rec: dict) -> dict:
    """The contract shape the detectors expect, built once at ingest."""
    return {
        "transaction_id": rec["transaction_id"],
        "step": rec.get("step") or 1,
        "type": rec.get("tx_type") or "TRANSFER",
        "amount": rec.get("amount") or 0.0,
        "nameOrig": rec.get("name_orig") or "",
        "nameDest": rec.get("name_dest") or "",
        "oldbalanceOrg": rec.get("old_balance_orig") or 0.0,
        "newbalanceOrig": rec.get("new_balance_orig") or 0.0,
        "oldbalanceDest": rec.get("old_balance_dest") or 0.0,
        "newbalanceDest": rec.get("new_balance_dest") or 0.0,
        "isFlaggedFraud": 1 if rec.get("is_flagged_fraud") else 0,
    }


# ── the replay job ────────────────────────────────────────────────────────────


class ReplayJob:
    """One file being replayed. Only one runs at a time, by design.

    Two concurrent replays into the same live table would interleave rows from
    different files and make throughput numbers meaningless, which is the
    opposite of what this tool is for.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.reset()

    def reset(self) -> None:
        self.state = "idle"          # idle | running | stopping | done | error
        self.source_file = None
        self.total = 0
        self.inserted = 0
        self.duplicates = 0
        self.errors = 0
        self.started_at = None
        self.finished_at = None
        self.message = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        elapsed = None
        if self.started_at:
            end = self.finished_at or time.time()
            elapsed = round(end - self.started_at, 1)
        rate = round(self.inserted / elapsed, 1) if elapsed and elapsed > 0 else None
        return {
            "state": self.state,
            "source_file": self.source_file,
            "total": self.total,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "elapsed_seconds": elapsed,
            "rows_per_second": rate,
            "percent": (
                round(100 * (self.inserted + self.duplicates + self.errors) / self.total)
                if self.total else 0
            ),
            "message": self.message,
        }

    def start(self, path: Path, business_date: str, uploaded_by: str) -> dict:
        with self._lock:
            if self.running:
                raise RuntimeError(
                    f"A replay of {self.source_file} is already running. "
                    "Stop it before starting another."
                )
            rows, headers = read_rows(path)
            mapping = map_columns(headers)

            missing = [f for f in ("tx_type", "amount", "name_orig", "name_dest")
                       if f not in mapping]
            if missing:
                raise ValueError(
                    "The file is missing columns the detectors need: "
                    + ", ".join(missing)
                    + f". Found: {', '.join(headers[:12])}"
                )

            self.reset()
            self.state = "running"
            self.source_file = path.name
            self.total = len(rows)
            self.started_at = time.time()
            self._stop.clear()

            self._thread = threading.Thread(
                target=self._run,
                args=(rows, mapping, path.name, business_date, uploaded_by),
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "total": self.total, "mapped_columns": mapping}

    def stop(self) -> None:
        self.state = "stopping"
        self._stop.set()

    def _run(self, rows, mapping, source_file, business_date, uploaded_by) -> None:
        cfg = config.load().replay
        delay = 1.0 / cfg.rows_per_second if cfg.rows_per_second > 0 else 0.0

        try:
            for rec in to_records(rows, mapping, source_file, business_date):
                if self._stop.is_set():
                    self.message = f"Stopped after {self.inserted} rows."
                    break
                self._insert_one(rec, uploaded_by)
                if delay:
                    time.sleep(delay)
            else:
                self.message = f"Replayed {self.inserted} rows from {source_file}."

            self.state = "done" if not self._stop.is_set() else "idle"
        except Exception as exc:                              # noqa: BLE001
            logger.exception("Replay failed")
            self.state = "error"
            self.message = f"{type(exc).__name__}: {exc}"
        finally:
            self.finished_at = time.time()

    def _insert_one(self, rec: dict, uploaded_by: str) -> None:
        session = get_session()
        try:
            # Archive first: a record of arrival must never depend on the queue
            # write succeeding.
            session.add(TransactionArchive(**{
                k: v for k, v in rec.items() if k != "payload"
            }, uploaded_by=uploaded_by))
            session.commit()
        except Exception as exc:                              # noqa: BLE001
            session.rollback()
            self.errors += 1
            logger.warning(f"Archive write failed: {exc}")
            session.close()
            return

        try:
            live = TransactionLive(
                transaction_id=rec["transaction_id"],
                business_date=rec["business_date"],
                step=rec.get("step"),
                tx_type=rec.get("tx_type"),
                amount=rec.get("amount"),
                name_orig=rec.get("name_orig"),
                name_dest=rec.get("name_dest"),
                old_balance_orig=rec.get("old_balance_orig"),
                new_balance_orig=rec.get("new_balance_orig"),
                old_balance_dest=rec.get("old_balance_dest"),
                new_balance_dest=rec.get("new_balance_dest"),
                payload=_payload(rec),
                status="pending",
            )
            session.add(live)
            session.commit()
            self.inserted += 1
        except IntegrityError:
            # Already queued. Re-uploading the same file is a no-op, which is
            # what makes the operation safe to retry.
            session.rollback()
            self.duplicates += 1
        except Exception as exc:                              # noqa: BLE001
            session.rollback()
            self.errors += 1
            logger.warning(f"Live insert failed: {exc}")
        finally:
            session.close()


JOB = ReplayJob()


# ── claiming, for when more than one worker exists ────────────────────────────

# Serialises the SQLite claim path within this process. SQLite cannot express
# SKIP LOCKED, so without this two threads read the same pending ids and both
# "claim" them — measured: four threads claiming 15 rows each returned 60 rows
# of which only 15 were distinct. The lock fixes that for threads in one
# process. It does nothing across processes, which is why several workers, or
# several machines, genuinely require PostgreSQL.
_CLAIM_LOCK = threading.Lock()


def claim_batch(worker_id: str, batch_size: int = 20) -> list[dict]:
    """Claim pending rows for exclusive processing.

    On PostgreSQL this uses `FOR UPDATE SKIP LOCKED`: a worker that meets a row
    another worker already holds steps over it rather than waiting, so two
    workers never take the same transaction and neither blocks.

    SQLite has no such clause and only one writer at a time, so the fallback is
    a plain UPDATE. That is correct for a single worker and *not* safe for
    several — which is the real reason a multi-worker deployment needs Postgres.
    """
    engine = get_engine()
    now = datetime.now(timezone.utc)
    is_pg = engine.dialect.name == "postgresql"

    # PostgreSQL serialises inside the database with SKIP LOCKED and needs no
    # help; SQLite needs the process-level lock above.
    guard = _CLAIM_LOCK if not is_pg else _NullGuard()
    with guard, engine.begin() as conn:
        if is_pg:
            rows = conn.execute(
                text("""
                    UPDATE transactions_live SET
                        status = 'claimed', claimed_by = :w, claimed_at = :now,
                        attempts = attempts + 1
                    WHERE id IN (
                        SELECT id FROM transactions_live
                        WHERE status = 'pending'
                        ORDER BY received_at
                        LIMIT :n
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, transaction_id, payload
                """),
                {"w": worker_id, "now": now, "n": batch_size},
            ).all()
        else:
            ids = [r[0] for r in conn.execute(
                text("SELECT id FROM transactions_live WHERE status='pending' "
                     "ORDER BY received_at LIMIT :n"),
                {"n": batch_size},
            ).all()]
            if not ids:
                return []
            placeholders = ",".join(str(int(i)) for i in ids)
            conn.execute(
                text(f"UPDATE transactions_live SET status='claimed', claimed_by=:w, "
                     f"claimed_at=:now, attempts=attempts+1 WHERE id IN ({placeholders})"),
                {"w": worker_id, "now": now},
            )
            rows = conn.execute(
                text(f"SELECT id, transaction_id, payload FROM transactions_live "
                     f"WHERE id IN ({placeholders})")
            ).all()

    # The claim uses raw SQL for SKIP LOCKED, which bypasses SQLAlchemy's JSON
    # column type — so `payload` arrives as a string on SQLite and as either on
    # PostgreSQL depending on the driver. Normalise here rather than making
    # every worker remember to.
    return [
        {"id": r[0], "transaction_id": r[1], "payload": _as_dict(r[2])} for r in rows
    ]


def release_stale(older_than_seconds: int = 120) -> int:
    """Return rows from a worker that died back to the queue.

    Without this a crash mid-batch leaves those transactions claimed forever —
    silently unscreened, which is the worst possible failure for this system.
    """
    engine = get_engine()
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE transactions_live SET status='pending', claimed_by=NULL "
                 "WHERE status='claimed' AND claimed_at < :cutoff"),
            {"cutoff": cutoff_dt},
        )
        return result.rowcount or 0


def mark_screened(row_id: int, escalated: bool = False, error: str | None = None) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE transactions_live SET status=:s, screened_at=:now, "
                 "escalated=:esc, last_error=:err WHERE id=:id"),
            {
                "s": "failed" if error else "screened",
                "now": datetime.now(timezone.utc),
                "esc": bool(escalated),
                "err": error,
                "id": row_id,
            },
        )


def _as_dict(value) -> dict:
    """Coerce a JSON column read through raw SQL back into a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        import json

        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def new_worker_id() -> str:
    import socket

    return f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
