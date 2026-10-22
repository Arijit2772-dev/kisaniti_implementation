from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from kisaniti_core.errors import ContractError, ErrorCode
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from kisaniti_api.main import app

from .conftest import contract_validator


def test_live(client: TestClient) -> None:
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "UP"}


def test_ready_matches_contract(client: TestClient) -> None:
    r = client.get("/health/ready")
    body = r.json()
    contract_validator("readiness.schema.json").validate(body)
    assert r.status_code == 200 and body["status"] == "READY", body
    assert body["components"] == {"database": "READY", "migrations": "READY"}


def test_ready_blocked_when_database_unreachable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://nobody:wrong@127.0.0.1:1/none")
    r = client.get("/health/ready")
    body = r.json()
    contract_validator("readiness.schema.json").validate(body)
    assert r.status_code == 503 and body["status"] == "BLOCKED"
    assert body["issues"][0]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_unknown_route_is_problem(client: TestClient) -> None:
    r = client.get("/no/such/thing")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    contract_validator("problem.schema.json").validate(r.json())
    assert r.json()["code"] == "NOT_FOUND" and r.headers["X-Request-ID"]


def test_contract_error_is_problem(client: TestClient) -> None:
    @app.get("/_test/contract-error")
    def boom() -> None:
        raise ContractError(ErrorCode.PRECISION_EXCEEDED, "too many digits", "/quantity_kg")

    r = client.get("/_test/contract-error", headers={"X-Request-ID": "req-1"})
    body = r.json()
    contract_validator("problem.schema.json").validate(body)
    assert r.status_code == 422 and body["code"] == "PRECISION_EXCEEDED" and body["request_id"] == "req-1"
    assert body["issues"][0]["path"] == "/quantity_kg"


def test_unexpected_error_is_internal_problem(client: TestClient) -> None:
    @app.get("/_test/crash")
    def crash() -> None:
        raise RuntimeError("secret detail must not leak")

    r = client.get("/_test/crash")
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL"
    assert "secret" not in r.text


def test_meta(client: TestClient) -> None:
    body = client.get("/api/v1/meta").json()
    assert body["contracts"] == ["shared/v1"] and body["mode"] in {"RESEARCH", "DEMO", "LIVE"}


def test_fpo_table_enforces_contract(engine: Engine) -> None:
    ok = "fpo_0000000000951YC3CSVS81Y403"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fpo"))
        conn.execute(
            text(
                "INSERT INTO fpo (id, name, timezone, origin, provenance) VALUES (:i, 'Demo', 'Asia/Kolkata', "
                "'SYNTHETIC', CAST(:p AS jsonb))"
            ),
            {"i": ok, "p": '{"generator": {"script": "t", "version": "1", "seed": 0}}'},
        )
    bad_rows = [
        ("fpo_lowercase0000000000000000", "Asia/Kolkata", "REAL", "{}"),
        ("fpo_0000000000VAK62YP256AHCH2D", "Europe/London", "REAL", "{}"),
        ("fpo_0000000000VAK62YP256AHCH2D", "Asia/Kolkata", "SYNTHETIC", "{}"),
    ]
    for i, tz, origin, prov in bad_rows:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO fpo (id, name, timezone, origin, provenance) "
                    "VALUES (:i, 'x', :tz, :o, CAST(:p AS jsonb))"
                ),
                {"i": i, "tz": tz, "o": origin, "p": prov},
            )
