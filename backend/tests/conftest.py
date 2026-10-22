"""Tests run against a real PostgreSQL (CI service container, or `docker compose up db` locally).

DATABASE_URL must point at a database the tests may migrate, e.g.
postgresql+psycopg://kisaniti:kisaniti@localhost:5433/kisaniti_test
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from sqlalchemy import Engine, text

from kisaniti_api.db import alembic_config, get_engine
from kisaniti_api.main import app

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "shared" / "v1"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail("DATABASE_URL is not set; start PostgreSQL (docker compose up -d db) and export it")
    return url


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    eng = get_engine(database_url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(alembic_config(database_url), "head")
    yield eng


@pytest.fixture()
def client(engine: Engine) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def contract_validator(name: str) -> Draft202012Validator:
    reg: Registry = Registry()
    for f in CONTRACTS.glob("*.schema.json"):
        doc = json.loads(f.read_text())
        reg = reg.with_resource(doc["$id"], Resource.from_contents(doc))
    schema: dict[str, Any] = json.loads((CONTRACTS / name).read_text())
    return Draft202012Validator(schema, registry=reg)
