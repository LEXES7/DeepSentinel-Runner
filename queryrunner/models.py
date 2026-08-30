"""The three tables this tool owns.

    transactions_archive   every row ever uploaded, never modified
    transactions_live      the replay queue the detection models consume
    fraud_cases            what the models caught, and why

Why the archive and the live table are separate
-----------------------------------------------
They answer different questions and have different lifetimes. The archive is
evidence: it records that a row was received, exactly as it arrived, and is
never updated afterwards. The live table is a work queue: rows change state as
they are claimed and screened, and old rows can be pruned once processed.

Keeping them in one table would mean the evidence record mutates every time a
worker touches it, which is precisely what evidence must not do.

Concurrency
-----------
`transactions_live` carries `status`, `claimed_by` and `claimed_at` so several
workers can draw from it without processing the same row twice. The claim
itself is in ingest.py — the columns are here because the schema has to support
it from the start, even while only one worker exists.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TransactionArchive(Base):
    """Immutable record of every row received. Written once, never updated.

    `source_file` and `uploaded_at` matter more than they look: when a result
    is questioned months later, the first question is which file it came from
    and when.

    `transaction_id` is unique, matching the live queue. Without that the two
    tables disagreed about what "already seen" means: replaying a file the
    queue had already accepted was a no-op there but appended a second full
    copy here, so the archive doubled while the run reported inserting nothing.
    """

    __tablename__ = "transactions_archive"

    id = Column(Integer, primary_key=True)
    transaction_id = Column(String(128), index=True, nullable=False)
    business_date = Column(String(10), index=True, nullable=True)  # YYYY-MM-DD

    step = Column(Integer, nullable=True)
    tx_type = Column(String(32), nullable=True)
    amount = Column(Float, nullable=True)
    name_orig = Column(String(64), index=True, nullable=True)
    name_dest = Column(String(64), index=True, nullable=True)
    old_balance_orig = Column(Float, nullable=True)
    new_balance_orig = Column(Float, nullable=True)
    old_balance_dest = Column(Float, nullable=True)
    new_balance_dest = Column(Float, nullable=True)

    # Ground truth, when the source file carries it. Kept for measuring the
    # system, and never served to a model.
    is_fraud = Column(Boolean, nullable=True)
    is_flagged_fraud = Column(Boolean, nullable=True)

    # The row exactly as it arrived, so a column we did not anticipate is not
    # lost on the way in.
    raw = Column(JSON, nullable=True)

    source_file = Column(String(255), index=True, nullable=True)
    uploaded_by = Column(String(64), nullable=True)
    uploaded_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_archive_date_type", "business_date", "tx_type"),
        UniqueConstraint("transaction_id", name="uq_archive_transaction_id"),
    )


class TransactionLive(Base):
    """The replay queue. Rows arrive pending and are claimed by a worker.

    `transaction_id` is unique: replaying the same file twice adds nothing the
    second time, rather than screening every row again and doubling the
    counters.
    """

    __tablename__ = "transactions_live"

    id = Column(Integer, primary_key=True)
    transaction_id = Column(String(128), nullable=False)
    business_date = Column(String(10), index=True, nullable=True)

    step = Column(Integer, nullable=True)
    tx_type = Column(String(32), nullable=True)
    amount = Column(Float, nullable=True)
    name_orig = Column(String(64), index=True, nullable=True)
    name_dest = Column(String(64), index=True, nullable=True)
    old_balance_orig = Column(Float, nullable=True)
    new_balance_orig = Column(Float, nullable=True)
    old_balance_dest = Column(Float, nullable=True)
    new_balance_dest = Column(Float, nullable=True)

    payload = Column(JSON, nullable=True)     # ready to POST to a detector

    # pending -> claimed -> screened | failed
    status = Column(String(16), nullable=False, default="pending", index=True)
    claimed_by = Column(String(64), nullable=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)

    received_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    screened_at = Column(DateTime(timezone=True), nullable=True)
    escalated = Column(Boolean, nullable=False, default=False)
    last_error = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_live_transaction_id"),
        # The claim query orders pending rows by arrival; this is the index
        # that keeps it from scanning the whole table as the queue grows.
        Index("ix_live_status_received", "status", "received_at"),
    )


class FraudCase(Base):
    """What the models caught: what, when, why, and on what evidence.

    Written by the detection platform rather than by this tool, but defined
    here because the schema belongs with the rest of the database.

    This is the table a stakeholder actually reads. It answers, for one alert:
    which models contributed and what each of them scored, which typology it
    matched, what structural or behavioural evidence was found, when it was
    detected, who was notified, and what a human decided afterwards.
    """

    __tablename__ = "fraud_cases"

    id = Column(Integer, primary_key=True)
    case_ref = Column(String(64), index=True, nullable=True)   # human-facing
    transaction_id = Column(String(128), index=True, nullable=False)
    business_date = Column(String(10), index=True, nullable=True)

    # ── What was caught ──
    detected_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    classification = Column(String(16), index=True, nullable=False)
    fused_score = Column(Float, nullable=False)

    # ── Why: each model's contribution, and whether it answered at all ──
    graph_score = Column(Float, nullable=True)
    behavioral_score = Column(Float, nullable=True)
    temporal_score = Column(Float, nullable=True)
    graph_available = Column(Boolean, nullable=True)
    behavioral_available = Column(Boolean, nullable=True)
    temporal_available = Column(Boolean, nullable=True)
    modalities_used = Column(Integer, nullable=True)

    # A confidence produced from one model is not the same claim as one
    # produced from three. Recording the penalty keeps that visible later.
    uncertainty_penalty_applied = Column(Boolean, nullable=True)

    # ── The evidence behind it ──
    typology_id = Column(String(32), nullable=True)
    typology_name = Column(String(128), nullable=True)
    typology_similarity = Column(Float, nullable=True)
    graph_pattern = Column(String(64), nullable=True)
    sink_account = Column(String(64), index=True, nullable=True)
    implicated_accounts = Column(JSON, nullable=True)
    graph_evidence = Column(JSON, nullable=True)
    behavioral_evidence = Column(JSON, nullable=True)
    temporal_evidence = Column(JSON, nullable=True)
    forensic_report = Column(Text, nullable=True)

    # ── Timing, for the latency claims ──
    screening_ms = Column(Integer, nullable=True)
    total_ms = Column(Integer, nullable=True)

    # ── What happened next ──
    alert_sent = Column(Boolean, nullable=False, default=False)
    alerted_at = Column(DateTime(timezone=True), nullable=True)
    recipients = Column(JSON, nullable=True)

    # open -> investigating -> confirmed_fraud | false_positive | closed
    review_status = Column(String(24), nullable=False, default="open", index=True)
    reviewed_by = Column(String(64), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    review_note = Column(Text, nullable=True)

    # Ground truth where the source file had it — for measuring precision after
    # the fact, never shown as a model output.
    label_is_fraud = Column(Boolean, nullable=True)

    model_versions = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_cases_date_class", "business_date", "classification"),
        Index("ix_cases_review", "review_status", "detected_at"),
    )


ALL_TABLES = (TransactionArchive, TransactionLive, FraudCase)
