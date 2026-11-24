"""Structured logs, request ID rules and build identity (P1 logging and build identity)."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from kisaniti_api.logs import JsonFormatter, build_commit

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def logged(caplog: pytest.LogCaptureFixture):  # type: ignore[no-untyped-def]
    """The app's loggers do not propagate to the root (they write JSON themselves), so attach the capture directly."""
    log = logging.getLogger("kisaniti_api")
    log.addHandler(caplog.handler)
    yield caplog
    log.removeHandler(caplog.handler)


def test_meta_reports_build_commit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KN_BUILD_COMMIT", SHA)
    assert client.get("/api/v1/meta").json()["build_commit"] == SHA


@pytest.mark.parametrize("value", ["", "dev", "0123abc", SHA.upper(), SHA + "0", "x" * 40])
def test_build_commit_is_unknown_unless_a_full_hash(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("KN_BUILD_COMMIT", value)
    assert build_commit() == "unknown"


def test_valid_request_id_is_kept(client: TestClient) -> None:
    r = client.get("/api/v1/meta", headers={"X-Request-ID": "abcd1234-feed"})
    assert r.headers["X-Request-ID"] == "abcd1234-feed"


@pytest.mark.parametrize("bad", ["short", "a" * 65, "has space inside", 'quote"injection1', "line\\nbreak123"])
def test_request_id_not_shaped_like_an_id_is_replaced(client: TestClient, bad: str) -> None:
    r = client.get("/api/v1/meta", headers={"X-Request-ID": bad})
    rid = r.headers["X-Request-ID"]
    assert rid != bad and len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)


def test_one_json_log_line_per_request_without_query(client: TestClient, logged: pytest.LogCaptureFixture) -> None:
    client.get("/api/v1/meta?search=Fictional%20Farmer", headers={"X-Request-ID": "trace-0000-0001"})
    lines = [
        r
        for r in logged.records
        if r.name == "kisaniti_api.access_log" and getattr(r, "request_id", "") == "trace-0000-0001"
    ]
    assert len(lines) == 1
    out = json.loads(JsonFormatter().format(lines[0]))
    assert out["request_id"] == "trace-0000-0001" and out["method"] == "GET" and out["path"] == "/api/v1/meta"
    assert out["status"] == 200 and isinstance(out["duration_ms"], float) and out["level"] == "INFO"
    assert "Fictional" not in json.dumps(out)  # the query string never reaches the log


def test_health_probes_are_not_logged(client: TestClient, logged: pytest.LogCaptureFixture) -> None:
    client.get("/health/live", headers={"X-Request-ID": "probe-0000-0001"})
    assert not [r for r in logged.records if getattr(r, "request_id", "") == "probe-0000-0001"]


def test_error_request_is_logged_with_its_status(client: TestClient, logged: pytest.LogCaptureFixture) -> None:
    client.get("/api/v1/no-such-thing", headers={"X-Request-ID": "miss-0000-0001"})
    rec = [r for r in logged.records if getattr(r, "request_id", "") == "miss-0000-0001"]
    assert len(rec) == 1 and rec[0].status == 404  # type: ignore[attr-defined]
