"""Engine management and schema creation.

The engine is rebuilt whenever the database configuration changes, so switching
between a local SQLite file and Neon in the Settings panel takes effect without
restarting the process.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from queryrunner import config
from queryrunner.models import Base

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_engine: Optional[Engine] = None
_engine_url: Optional[str] = None
_SessionFactory: Optional[sessionmaker] = None


def _build(url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True, "future": True}

    if url.startswith("sqlite"):
        # The replay thread writes while the web request thread reads, and
        # SQLite objects are otherwise bound to the thread that made them.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Neon closes idle connections, and its pooler dislikes large pools
        # from a desktop tool that is mostly idle.
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 2
        kwargs["pool_recycle"] = 300
        kwargs["connect_args"] = {"ssl_context": True}

    return create_engine(url, **kwargs)


def get_engine(force: bool = False) -> Engine:
    global _engine, _engine_url, _SessionFactory
    url = config.load().database.url()
    with _LOCK:
        if force or _engine is None or url != _engine_url:
            if _engine is not None:
                _engine.dispose()
            _engine = _build(url)
            _engine_url = url
            _SessionFactory = sessionmaker(bind=_engine, future=True)
        return _engine


def get_session() -> Session:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory()


def test_connection() -> dict:
    """Try to connect, and report the result in words a user can act on."""
    cfg = config.load().database
    try:
        engine = get_engine(force=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "message": f"Connected — {cfg.describe()}"}
    except Exception as exc:                                 # noqa: BLE001
        detail = str(exc).splitlines()[0][:200]
        hint = ""
        low = detail.lower()
        if "password" in low or "authentication" in low:
            hint = " Check the user and password."
        elif "could not translate host" in low or "name or service" in low:
            hint = " The host name could not be resolved — check it for typos."
        elif "timeout" in low or "timed out" in low:
            hint = " The server did not answer. Check the host, port and any firewall."
        elif "pg8000" in low or "no module named" in low:
            hint = " Install the PostgreSQL driver:  pip install pg8000"
        return {"ok": False, "message": f"{type(exc).__name__}: {detail}{hint}"}


def create_tables() -> dict:
    """Create anything missing. Never alters or drops an existing table."""
    engine = get_engine()
    before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    after = set(inspect(engine).get_table_names())
    created = sorted(after - before)

    upgrade = ensure_archive_unique()

    message = f"Created {', '.join(created)}" if created else "All tables already present"
    if upgrade["changed"] or upgrade["blocked"]:
        message += f". {upgrade['message']}"

    return {
        "ok": True,
        "created": created,
        "existing": sorted(before & after),
        "upgrade": upgrade,
        "message": message,
    }


ARCHIVE_UNIQUE = "uq_archive_transaction_id"


def ensure_archive_unique() -> dict:
    """Add the archive's unique index to a database created before it existed.

    `create_all` only creates missing tables, so a database from before the
    constraint keeps appending a fresh copy of a file on every replay. Adding
    the index is additive and safe — but only when the table holds no
    duplicates already, so that is checked first and a table with duplicates is
    reported and left exactly as it is. Deciding what to delete is not this
    function's call to make.
    """
    engine = get_engine()
    insp = inspect(engine)

    if "transactions_archive" not in insp.get_table_names():
        return {"changed": False, "blocked": False, "message": "No archive table yet."}

    have = {ix["name"] for ix in insp.get_indexes("transactions_archive")}
    have |= {c["name"] for c in insp.get_unique_constraints("transactions_archive")}
    if ARCHIVE_UNIQUE in have:
        return {"changed": False, "blocked": False, "message": "Archive already unique."}

    with engine.connect() as conn:
        dupes = conn.execute(text(
            "SELECT COUNT(*) FROM (SELECT transaction_id FROM transactions_archive "
            "GROUP BY transaction_id HAVING COUNT(*) > 1) d"
        )).scalar_one()

        if dupes:
            return {
                "changed": False,
                "blocked": True,
                "duplicate_ids": int(dupes),
                "message": (
                    f"The archive holds {dupes} transaction ids more than once, "
                    "from replaying a file before duplicates were prevented. "
                    "Finish a test run to clear the tables and the constraint "
                    "will be added automatically."
                ),
            }

        try:
            conn.execute(text(
                f"CREATE UNIQUE INDEX {ARCHIVE_UNIQUE} "
                "ON transactions_archive (transaction_id)"
            ))
            conn.commit()
        except Exception as exc:                              # noqa: BLE001
            logger.warning(f"Could not add {ARCHIVE_UNIQUE}: {exc}")
            return {
                "changed": False, "blocked": True,
                "message": f"Could not add the archive constraint: {exc}",
            }

    logger.info(f"Added {ARCHIVE_UNIQUE} to transactions_archive.")
    return {
        "changed": True, "blocked": False,
        "message": "Added the archive's unique constraint — replaying a file "
                   "twice can no longer duplicate it.",
    }


def table_counts() -> dict:
    """Row counts, for the dashboard. Missing tables report as None."""
    engine = get_engine()
    names = set(inspect(engine).get_table_names())
    out: dict = {}
    with engine.connect() as conn:
        for table in ("transactions_archive", "transactions_live", "fraud_cases"):
            if table not in names:
                out[table] = None
                continue
            try:
                out[table] = conn.execute(
                    text(f"SELECT COUNT(*) FROM {table}")     # noqa: S608 — fixed names
                ).scalar_one()
            except Exception:                                 # noqa: BLE001
                out[table] = None

        if "transactions_live" in names:
            try:
                rows = conn.execute(
                    text(
                        "SELECT status, COUNT(*) FROM transactions_live GROUP BY status"
                    )
                ).all()
                out["live_by_status"] = {r[0]: r[1] for r in rows}
            except Exception:                                 # noqa: BLE001
                out["live_by_status"] = {}
    return out
