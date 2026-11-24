# ruff: noqa: F811, F401  (fixtures imported from test_access by name: pytest uses them as arguments)
"""models/v1: the forecasting-model registry served to signed-in users, exactly as built from the frozen result."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from kisaniti_api.main import app
from kisaniti_api.models.routes import REGISTRY
from tests.test_access import Fpo, access_env, clean_up_access_data, two

SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "models" / "v1" / "registry.schema.json"


def validate(doc: Any) -> None:
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator(
        schema, registry=Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    ).validate(doc)


def test_registry_needs_sign_in(two: tuple[Fpo, Fpo]) -> None:
    r = TestClient(app, raise_server_exceptions=False).get("/api/v1/models/registry")
    assert r.status_code == 401


def test_registry_is_the_committed_document_and_matches_the_contract(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    doc = a.admin.get("/models/registry").json()
    validate(doc)
    assert doc == json.loads(REGISTRY.read_text())
    assert b.admin.get("/models/registry").json() == doc  # holds no FPO data: the same for every FPO


def test_frozen_c0_result_keeps_naive_everywhere() -> None:
    """The C.0 test season: 42 comparisons, none BETTER, so section 9 keeps naive at every variety and horizon."""
    doc = json.loads(REGISTRY.read_text())
    models = [m for e in doc["entries"] for m in e["models"]]
    assert len(models) == 42 and len(doc["entries"]) == 6
    assert {m["verdict"] for m in models} == {"NO_DETECTABLE_DIFFERENCE"}
    assert {m["status"] for m in models} == {"NOT_PROMOTED"}
    assert {(e["in_use"], e["in_use_reason"]) for e in doc["entries"]} == {("naive", "BASELINE_ENOUGH")}
    assert {e["horizon_days"] for e in doc["entries"]} == {1, 7, 14}
    assert doc["untested_horizons"] == [
        {"horizon_days": 28, "treatment": "SECONDARY"},
        {"horizon_days": 60, "treatment": "DESCRIPTIVE"},
    ]
    assert doc["evidence"]["file_sha256"] == "0a16b16f7e66f421d7ff8350ee0f3a0585b72e5c2d84d8938d2d541a923b63ce"
