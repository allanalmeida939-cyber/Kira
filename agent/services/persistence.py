from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base


logger = logging.getLogger(__name__)

_ENGINE = None
_SESSION_FACTORY = None


def _database_path() -> Path:
    configured = str(os.getenv("KIRA_RUNTIME_DB_PATH", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "runtime" / "kira_runtime.sqlite3"


def get_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    db_path = _database_path().expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _ENGINE = create_engine(
        URL.create("sqlite", database=str(db_path)),
        future=True,
        connect_args={"check_same_thread": False},
    )
    return _ENGINE


def get_session_factory() -> sessionmaker[Session]:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is not None:
        return _SESSION_FACTORY
    Base.metadata.create_all(get_engine())
    _SESSION_FACTORY = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SESSION_FACTORY


def init_database() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA synchronous=NORMAL;"))
    except Exception:
        logger.exception("Falha ao aplicar pragmas no SQLite de runtime.")
