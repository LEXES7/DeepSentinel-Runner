"""Configuration, read from config.ini next to the project root.

One file, edited by hand or through the Settings panel in the interface. The
whole point of this tool is that four people on four laptops can run it without
a setup ritual, so configuration is a single readable file and nothing else.
"""

from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.ini"
EXAMPLE_PATH = ROOT / "config.example.ini"


@dataclass
class DatabaseConfig:
    # "sqlite" for a local file, "postgres" for Neon or any other server.
    kind: str = "sqlite"
    sqlite_path: str = "./deepsentinel_runner.db"
    host: str = ""
    port: int = 5432
    name: str = ""
    user: str = ""
    password: str = ""
    sslmode: str = "require"

    def url(self) -> str:
        """SQLAlchemy URL.

        pg8000 rather than psycopg2: it is pure Python, so `pip install` never
        needs a compiler. On four different laptops that difference is the gap
        between "it runs" and an afternoon of build errors.
        """
        if self.kind == "sqlite":
            return f"sqlite:///{self.sqlite_path}"
        from urllib.parse import quote_plus

        pw = quote_plus(self.password)
        user = quote_plus(self.user)
        return (
            f"postgresql+pg8000://{user}:{pw}@{self.host}:{self.port}/{self.name}"
        )

    def describe(self) -> str:
        """Human-readable, and never includes the password."""
        if self.kind == "sqlite":
            return f"SQLite · {self.sqlite_path}"
        return f"PostgreSQL · {self.user}@{self.host}:{self.port}/{self.name}"


@dataclass
class ReplayConfig:
    # Rows per second pushed into the live table. The point is to look like
    # arriving traffic rather than a bulk load, so the monitor has something
    # to react to.
    rows_per_second: float = 5.0
    # Rows read from the file at a time. Larger is faster but less smooth.
    batch_size: int = 50
    # Where Drive for Desktop (or any shared folder) is mounted. A file picked
    # from here is read in place rather than uploaded.
    watch_folder: str = ""


@dataclass
class Settings:
    database: DatabaseConfig
    replay: ReplayConfig

    def to_dict(self) -> dict:
        d = asdict(self)
        d["database"].pop("password", None)      # never leaves the process
        d["database"]["configured"] = bool(
            self.database.kind == "sqlite" or self.database.host
        )
        d["database"]["describe"] = self.database.describe()
        return d


def _parser() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        cp.read(CONFIG_PATH, encoding="utf-8")
    elif EXAMPLE_PATH.exists():
        cp.read(EXAMPLE_PATH, encoding="utf-8")
    return cp


def load() -> Settings:
    cp = _parser()

    def get(section: str, key: str, default: str = "") -> str:
        try:
            return cp.get(section, key, fallback=default).strip()
        except Exception:                                    # noqa: BLE001
            return default

    db = DatabaseConfig(
        kind=(get("DATABASE", "kind", "sqlite") or "sqlite").lower(),
        sqlite_path=get("DATABASE", "sqlite_path", "./deepsentinel_runner.db"),
        host=get("DATABASE", "host"),
        port=int(get("DATABASE", "port", "5432") or 5432),
        name=get("DATABASE", "name"),
        user=get("DATABASE", "user"),
        password=get("DATABASE", "password"),
        sslmode=get("DATABASE", "sslmode", "require"),
    )
    rp = ReplayConfig(
        rows_per_second=float(get("REPLAY", "rows_per_second", "5") or 5),
        batch_size=int(get("REPLAY", "batch_size", "50") or 50),
        watch_folder=get("REPLAY", "watch_folder"),
    )
    return Settings(database=db, replay=rp)


def save(changes: dict) -> Settings:
    """Write a partial update back to config.ini.

    Reads the existing file first so hand-written comments and any section this
    tool does not know about survive being edited from the interface.
    """
    cp = _parser()
    for section, values in changes.items():
        sect = section.upper()
        if not cp.has_section(sect):
            cp.add_section(sect)
        for key, value in values.items():
            if value is None:
                continue
            # An empty password from the UI means "leave it alone", not "clear
            # it" — the field is never populated on load, so a blank submit
            # would otherwise wipe a working credential.
            if key == "password" and value == "":
                continue
            cp.set(sect, key, str(value))

    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        cp.write(fh)
    return load()
