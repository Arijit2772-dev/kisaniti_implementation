"""Database engine and the two readiness checks used by /health/ready."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text

BACKEND_DIR = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=4)
def get_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def head_revision(url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


def database_answers(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT 1")).scalar_one() == 1)


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")).scalar_one()
        if not exists:
            return None
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        return None if row is None else str(row[0])
