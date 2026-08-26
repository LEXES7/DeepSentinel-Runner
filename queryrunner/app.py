"""The Query Runner web app.

Deliberately a single HTML page served by FastAPI with no build step: it has to
run on four laptops with `poetry run python run.py` and nothing else. A React
front end would mean node, a lockfile and a build for a tool whose entire job
is to upload a file.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from queryrunner import config, db, ingest

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("queryrunner")

STATIC = Path(__file__).resolve().parent / "static"
UPLOADS = Path(__file__).resolve().parent.parent / "uploads"

app = FastAPI(title="DeepSentinel Query Runner", version="1.0.0")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ── configuration ─────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings() -> dict:
    s = config.load()
    return {
        **s.to_dict(),
        "config_path": str(config.CONFIG_PATH),
        "config_exists": config.CONFIG_PATH.exists(),
    }


@app.put("/api/settings")
def put_settings(body: dict) -> dict:
    changes = {k: v for k, v in body.items() if isinstance(v, dict)}
    if not changes:
        raise HTTPException(422, "Send at least one section, e.g. {\"database\": {...}}")
    config.save(changes)
    db.get_engine(force=True)          # pick up a new database immediately
    return get_settings()


@app.post("/api/database/test")
def test_database() -> dict:
    return db.test_connection()


@app.post("/api/database/create-tables")
def create_tables() -> dict:
    try:
        return db.create_tables()
    except Exception as exc:                                  # noqa: BLE001
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")


@app.get("/api/database/status")
def database_status() -> dict:
    cfg = config.load().database
    try:
        counts = db.table_counts()
        return {"ok": True, "describe": cfg.describe(), "counts": counts}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "describe": cfg.describe(), "error": str(exc)[:200]}


# ── files ─────────────────────────────────────────────────────────────────────

@app.get("/api/files")
def list_files() -> dict:
    """Files in the configured shared folder.

    Reading from a Drive for Desktop path in place means nobody has to upload a
    200 MB extract four times over.
    """
    folder = config.load().replay.watch_folder
    if not folder:
        return {"folder": None, "files": [], "message": "No shared folder configured."}

    p = Path(folder).expanduser()
    if not p.is_dir():
        return {"folder": str(p), "files": [], "message": f"{p} is not a folder."}

    files = []
    for f in sorted(p.iterdir()):
        if f.suffix.lower() in ingest.ACCEPTED_SUFFIXES and not f.name.startswith("~$"):
            files.append({
                "name": f.name,
                "path": str(f),
                "size_mb": round(f.stat().st_size / 1_048_576, 2),
            })
    return {"folder": str(p), "files": files, "message": ""}


@app.post("/api/preview")
async def preview(file: UploadFile | None = File(None), path: str = Form("")) -> dict:
    """Show what was found and how columns mapped, before anything is written."""
    tmp = None
    try:
        if file is not None:
            tmp = _save_upload(file)
            target = tmp
        elif path:
            target = Path(path).expanduser()
            if not target.is_file():
                raise HTTPException(400, f"No file at {target}")
        else:
            raise HTTPException(422, "Provide a file or a path.")

        rows, headers = ingest.read_rows(target)
        mapping = ingest.map_columns(headers)
        missing = [f for f in ("tx_type", "amount", "name_orig", "name_dest")
                   if f not in mapping]
        return {
            "name": target.name,
            "rows": len(rows),
            "headers": headers,
            "mapped": mapping,
            "unmapped": [h for h in headers if h not in mapping.values()],
            "missing_required": missing,
            "sample": rows[:5],
            "ready": not missing,
        }
    except HTTPException:
        raise
    except Exception as exc:                                  # noqa: BLE001
        raise HTTPException(400, str(exc))
    finally:
        if tmp and tmp.exists() and file is not None:
            pass      # kept: /api/replay/start may reuse it


def _save_upload(file: UploadFile) -> Path:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename or "upload.csv").name
    target = UPLOADS / safe
    with target.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return target


# ── replay ────────────────────────────────────────────────────────────────────

@app.post("/api/replay/start")
async def start_replay(
    file: UploadFile | None = File(None),
    path: str = Form(""),
    business_date: str = Form(""),
    uploaded_by: str = Form(""),
) -> dict:
    if file is not None:
        target = _save_upload(file)
    elif path:
        target = Path(path).expanduser()
        if not target.is_file():
            raise HTTPException(400, f"No file at {target}")
    else:
        raise HTTPException(422, "Provide a file or a path.")

    # Fail here rather than three seconds into a replay with half the rows in.
    status = db.test_connection()
    if not status["ok"]:
        raise HTTPException(400, f"Database not reachable. {status['message']}")

    try:
        result = ingest.JOB.start(
            target,
            business_date or date.today().isoformat(),
            uploaded_by or os.getenv("USER") or "unknown",
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result


@app.get("/api/replay/status")
def replay_status() -> dict:
    return ingest.JOB.status()


@app.post("/api/replay/stop")
def stop_replay() -> dict:
    ingest.JOB.stop()
    return ingest.JOB.status()


# ── queue ─────────────────────────────────────────────────────────────────────

@app.post("/api/queue/release-stale")
def release_stale(older_than_seconds: int = 120) -> dict:
    n = ingest.release_stale(older_than_seconds)
    return {"released": n, "message": f"Returned {n} stalled row(s) to the queue."}


@app.get("/api/cases")
def recent_cases(limit: int = 25) -> dict:
    """Recent fraud cases — written by the detection platform, read here."""
    from sqlalchemy import inspect, text

    engine = db.get_engine()
    if "fraud_cases" not in set(inspect(engine).get_table_names()):
        return {"cases": [], "message": "The fraud_cases table has not been created yet."}
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT case_ref, transaction_id, detected_at, classification, "
                 "fused_score, typology_name, review_status FROM fraud_cases "
                 "ORDER BY detected_at DESC LIMIT :n"),
            {"n": min(limit, 200)},
        ).all()
    return {
        "cases": [
            {
                "case_ref": r[0], "transaction_id": r[1],
                "detected_at": str(r[2]), "classification": r[3],
                "fused_score": r[4], "typology_name": r[5], "review_status": r[6],
            }
            for r in rows
        ],
        "message": "",
    }


@app.exception_handler(Exception)
async def unhandled(request, exc):                            # noqa: ANN001
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
