"""Access slice (contract access/v1) on real PostgreSQL: passwords, sign-in, sessions, CSRF, roles, FPO isolation.

Every user and FPO here is fictional (SYNTHETIC). Scrypt cost is lowered for speed through KISANITI_SCRYPT_N; one test
checks the production cost. Responses are validated against the JSON Schemas of contracts/access/v1.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from kisaniti_api.access import cli
from kisaniti_api.access.passwords import (
    generate_temporary_password,
    hash_password,
    password_problems,
    verify_password,
)
from kisaniti_api.main import app

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
GOOD_PW = "correct horse battery staple 42"


@pytest.fixture(autouse=True, scope="module")
def clean_up_access_data(engine: Engine) -> Any:
    """These tests create fictional FPOs and users; remove them so other test modules start from their own state."""
    yield
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit_log, sessions, login_attempts, users, fpo CASCADE"))


@pytest.fixture(autouse=True)
def access_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KISANITI_SCRYPT_N", "1024")
    monkeypatch.setenv("KISANITI_COOKIE_SECURE", "false")
    monkeypatch.setenv("KISANITI_ALLOWED_ORIGINS", "http://localhost:3000")


def validator(name: str) -> Draft202012Validator:
    reg: Registry = Registry()
    for d in (CONTRACTS / "access" / "v1", CONTRACTS / "shared" / "v1"):
        for f in d.glob("*.schema.json"):
            doc = json.loads(f.read_text())
            reg = reg.with_resource(doc["$id"], Resource.from_contents(doc))
    sid = json.loads((CONTRACTS / "access" / "v1" / f"{name}.schema.json").read_text())["$id"]
    return Draft202012Validator({"$ref": sid}, registry=reg)


def assert_valid(name: str, doc: Any) -> None:
    errors = [e.message for e in validator(name).iter_errors(doc)]
    assert errors == [], errors


def strip(r: Any) -> dict[str, Any]:
    """An error body without the fields that legitimately differ per request."""
    return {k: v for k, v in r.json().items() if k not in ("request_id", "instance")}


def uname(prefix: str) -> str:
    return f"{prefix}.{uuid.uuid4().hex[:8]}"


class Session:
    """A signed-in browser: keeps the cookie, adds the CSRF header to state-changing calls."""

    def __init__(self, username: str, password: str = GOOD_PW) -> None:
        self.client = TestClient(app, raise_server_exceptions=False)
        self.username = username
        r = self.client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        self.principal: dict[str, Any] = r.json()

    @property
    def csrf(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.principal["csrf_token"]}

    def get(self, path: str) -> Any:
        return self.client.get(f"/api/v1{path}")

    def post(self, path: str, body: Any = None) -> Any:
        return self.client.post(f"/api/v1{path}", json=body, headers=self.csrf)

    def put(self, path: str, body: Any = None) -> Any:
        return self.client.put(f"/api/v1{path}", json=body, headers=self.csrf)


@dataclass
class Fpo:
    fpo_id: str
    admin: Session
    admin_id: str
    admin_name: str


def first_login_and_set_password(username: str, temporary: str) -> None:
    s = Session(username, temporary)
    assert s.principal["must_change_password"] is True
    r = s.post("/auth/change-password", {"current_password": temporary, "new_password": GOOD_PW})
    assert r.status_code == 204, r.text


def make_fpo(label: str) -> Fpo:
    admin_name = uname(f"admin.{label}")
    fpo_id, uid, temp = cli.create_fpo(f"Fictional FPO {label} {uuid.uuid4().hex[:6]}", admin_name, "Admin (fictional)")
    first_login_and_set_password(admin_name, temp)
    return Fpo(fpo_id, Session(admin_name), uid, admin_name)


@pytest.fixture()
def two(engine: Engine) -> tuple[Fpo, Fpo]:
    return make_fpo("a"), make_fpo("b")


def add_user(
    f: Fpo, role: str = "STAFF", extra: list[str] | None = None, signed_in: bool = True
) -> tuple[str, str, Any]:
    name = uname(role.lower())
    r = f.admin.post(
        "/users",
        {"username": name, "display_name": "Fictional person", "role": role, "extra_capabilities": extra or []},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    if signed_in:
        first_login_and_set_password(name, body["temporary_password"])
        return body["user"]["id"], name, Session(name)
    return body["user"]["id"], name, body["temporary_password"]


def audit_events(engine: Engine, **where: str) -> list[str]:
    clause = " AND ".join(f"{k} = :{k}" for k in where) or "true"
    with engine.connect() as c:
        return [r.event for r in c.execute(text(f"SELECT event FROM audit_log WHERE {clause} ORDER BY id"), where)]


# ---- passwords ---------------------------------------------------------------------------------------------------
def test_hash_roundtrip_with_parameters_stored_in_the_record() -> None:
    rec = hash_password("a long enough passphrase", 1024)
    assert rec.startswith("scrypt$1024$8$1$")
    assert verify_password("a long enough passphrase", rec)
    assert not verify_password("another passphrase", rec)
    assert hash_password("a long enough passphrase", 1024) != rec  # fresh salt every time


def test_production_scrypt_cost_works() -> None:
    rec = hash_password("production cost check", 2**17)
    assert verify_password("production cost check", rec)


@pytest.mark.parametrize(
    "record",
    [
        "",
        "garbage",
        "scrypt$1000$8$1$AA$AA",
        "scrypt$1024$8$1$!!$!!",
        "argon2$1$2$3$4$5",
        f"scrypt${2**30}$8$1$AAAA$AAAA",
    ],
)
def test_bad_or_hostile_hash_records_never_verify(record: str) -> None:
    assert verify_password("anything at all", record) is False


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short", "TOO_SHORT"),
        ("x" * 257, "TOO_LONG"),
        ("aaaaaaaaaaaa", "REPEATED_CHARACTER"),
        ("Password123", "COMMON_PASSWORD"),
        ("my-demo.user-pass1", "CONTAINS_USERNAME"),
    ],
)
def test_password_policy_refusals(password: str, problem: str) -> None:
    assert problem in password_problems(password, "demo.user")


def test_password_policy_accepts_unicode_passphrases_without_composition_rules() -> None:
    assert password_problems("ਮੇਰਾ ਪੰਜਾਬੀ ਪਾਸਵਰਡ ਹੈ", "demo.user") == []
    assert password_problems("all lowercase letters here", "demo.user") == []


def test_temporary_passwords_are_long_unlike_and_unambiguous() -> None:
    seen = {generate_temporary_password() for _ in range(200)}
    assert len(seen) == 200
    for t in seen:
        assert len(t) == 19 and t.count("-") == 3 and not set(t) & set("0O1lI")


# ---- sign-in -----------------------------------------------------------------------------------------------------
def test_sign_in_returns_a_principal_that_matches_the_contract(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    p = a.admin.principal
    assert_valid("principal", p)
    assert p["fpo_id"] == a.fpo_id and p["role"] == "FPO_ADMIN" and "users.manage" in p["capabilities"]


def test_wrong_password_and_unknown_user_are_indistinguishable(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    c = TestClient(app, raise_server_exceptions=False)
    wrong = c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": "not the password"})
    unknown = c.post("/api/v1/auth/login", json={"username": uname("nobody"), "password": "not the password"})
    assert wrong.status_code == unknown.status_code == 401
    assert strip(wrong) == strip(unknown)
    assert "set-cookie" not in wrong.headers and "set-cookie" not in unknown.headers


def test_cookie_flags_and_the_token_is_stored_only_as_a_hash(
    engine: Engine, two: tuple[Fpo, Fpo], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    monkeypatch.setenv("KISANITI_COOKIE_SECURE", "true")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": GOOD_PW})
    cookie = r.headers["set-cookie"]
    assert "kn_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    token = cookie.split("kn_session=")[1].split(";")[0]
    with engine.connect() as conn:
        hashes = {x[0] for x in conn.execute(text("SELECT token_sha256 FROM sessions"))}
        assert hashlib.sha256(token.encode()).hexdigest() in hashes
        assert token not in hashes


def test_each_sign_in_gets_a_new_session_token(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    tokens = set()
    for _ in range(3):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": GOOD_PW})
        tokens.add(r.cookies.get("kn_session"))
    assert len(tokens) == 3


def test_throttle_blocks_after_repeated_failures_and_does_not_reveal_whether_the_user_exists(
    two: tuple[Fpo, Fpo], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    for name in (a.admin_name, uname("ghost")):  # one real user, one that does not exist
        c = TestClient(app, raise_server_exceptions=False)
        codes = [
            c.post("/api/v1/auth/login", json={"username": name, "password": "bad password"}).status_code
            for _ in range(5)
        ]
        assert codes == [401] * 5
        r = c.post("/api/v1/auth/login", json={"username": name, "password": GOOD_PW})  # even the right password
        assert r.status_code == 429 and r.headers["retry-after"].isdigit()
        assert_valid("throttled_problem", r.json())
        assert r.json()["code"] == "TOO_MANY_ATTEMPTS"


def test_success_clears_the_failure_count(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    c = TestClient(app, raise_server_exceptions=False)
    for _ in range(4):
        assert (
            c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": "bad password"}).status_code == 401
        )
    assert c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": GOOD_PW}).status_code == 200
    for _ in range(4):  # a fresh count: four more failures do not block yet
        assert (
            c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": "bad password"}).status_code == 401
        )


def test_concurrent_failures_are_all_counted(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    codes: list[int] = []
    lock = threading.Lock()

    def attempt() -> None:
        c = TestClient(app, raise_server_exceptions=False)
        code = c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": "bad"}).status_code
        with lock:
            codes.append(code)

    ts = [threading.Thread(target=attempt) for _ in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(codes) <= {401, 429} and codes.count(401) >= 5  # at least the threshold of failures were counted
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/api/v1/auth/login", json={"username": a.admin_name, "password": GOOD_PW}).status_code == 429


def test_a_deactivated_user_cannot_sign_in(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    uid, name, _s = add_user(a, "VIEWER")
    assert a.admin.post(f"/users/{uid}/deactivate").status_code == 204
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/auth/login", json={"username": name, "password": GOOD_PW}
    )
    assert r.status_code == 401


def test_login_body_rejects_an_fpo_field(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    r = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/auth/login", json={"username": a.admin_name, "password": GOOD_PW, "fpo_id": b.fpo_id}
    )
    assert r.status_code == 422


# ---- sessions and CSRF -------------------------------------------------------------------------------------------
def test_requests_without_a_session_are_unauthenticated() -> None:
    c = TestClient(app, raise_server_exceptions=False)
    for method, path in (("get", "/api/v1/auth/me"), ("get", "/api/v1/users"), ("post", "/api/v1/auth/logout")):
        r = getattr(c, method)(path)
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED", path


def test_logout_revokes_the_session_on_the_server(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    s = Session(a.admin_name)
    assert s.get("/auth/me").status_code == 200
    assert s.post("/auth/logout").status_code == 204
    stale = TestClient(app, raise_server_exceptions=False)
    stale.cookies.set("kn_session", s.client.cookies.get("kn_session") or "x")
    assert stale.get("/api/v1/auth/me").status_code == 401


def test_expired_and_idle_sessions_stop_working(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    s1, s2 = Session(a.admin_name), Session(a.admin_name)
    h1 = hashlib.sha256(s1.client.cookies["kn_session"].encode()).hexdigest()
    h2 = hashlib.sha256(s2.client.cookies["kn_session"].encode()).hexdigest()
    with engine.begin() as c:
        c.execute(
            text("UPDATE sessions SET expires_at = now() - interval '1 minute' WHERE token_sha256 = :h"), {"h": h1}
        )
        c.execute(
            text("UPDATE sessions SET last_seen_at = now() - interval '3 hours' WHERE token_sha256 = :h"), {"h": h2}
        )
    assert s1.get("/auth/me").status_code == 401  # absolute expiry
    assert s2.get("/auth/me").status_code == 401  # idle expiry (120 minutes)


@pytest.mark.parametrize("headers", [{}, {"X-CSRF-Token": "wrong" * 10}, {"X-CSRF-Token": ""}])
def test_state_changing_requests_need_the_csrf_token(two: tuple[Fpo, Fpo], headers: dict[str, str]) -> None:
    a, _ = two
    r = a.admin.client.put("/api/v1/auth/language", json={"language": "pa"}, headers=headers)
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN" and r.json()["issues"][0]["code"] == "CSRF_INVALID"


def test_a_foreign_origin_is_refused_even_with_the_right_token(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    bad = a.admin.client.put(
        "/api/v1/auth/language", json={"language": "pa"}, headers={**a.admin.csrf, "Origin": "https://evil.example"}
    )
    ok = a.admin.client.put(
        "/api/v1/auth/language", json={"language": "pa"}, headers={**a.admin.csrf, "Origin": "http://localhost:3000"}
    )
    assert bad.status_code == 403 and ok.status_code == 200


def test_csrf_failures_are_audited(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    a.admin.client.put("/api/v1/auth/language", json={"language": "pa"})
    assert "ACCESS_DENIED" in audit_events(engine, actor_user_id=a.admin_id)


def test_language_is_saved_and_validated(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    r = a.admin.put("/auth/language", {"language": "pa"})
    assert r.status_code == 200 and r.json()["language"] == "pa"
    assert a.admin.get("/auth/me").json()["language"] == "pa"
    assert a.admin.put("/auth/language", {"language": "hi"}).status_code == 422


# ---- passwords in use --------------------------------------------------------------------------------------------
def test_new_users_must_change_their_password_before_anything_else(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    _uid, _name, temp = add_user(a, "STAFF", signed_in=False)
    s = Session(_name, temp)
    assert s.principal["must_change_password"] is True
    assert s.get("/auth/me").status_code == 200
    r = s.get("/users")
    assert r.status_code == 403 and r.json()["issues"][0]["code"] == "PASSWORD_CHANGE_REQUIRED"


def test_change_password_rules_and_effects(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    _uid, name, temp = add_user(a, "STAFF", signed_in=False)
    s1, s2 = Session(name, temp), Session(name, temp)
    assert s1.post("/auth/change-password", {"current_password": "wrong", "new_password": GOOD_PW}).status_code == 401
    weak = s1.post("/auth/change-password", {"current_password": temp, "new_password": "Password123"})
    assert weak.status_code == 422 and weak.json()["issues"][0]["code"] == "WEAK_PASSWORD"
    short = s1.post("/auth/change-password", {"current_password": temp, "new_password": "short"})
    assert short.status_code == 422
    assert s1.post("/auth/change-password", {"current_password": temp, "new_password": GOOD_PW}).status_code == 204
    assert s1.get("/auth/me").json()["must_change_password"] is False  # this session stays signed in
    assert s2.get("/auth/me").status_code == 401  # the user's other sessions are revoked
    assert TestClient(app).post("/api/v1/auth/login", json={"username": name, "password": temp}).status_code == 401


# ---- roles ---------------------------------------------------------------------------------------------------------
def test_roles_grant_only_their_capabilities(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    _, _, staff = add_user(a, "STAFF", ["messages.send"])
    _, _, viewer = add_user(a, "VIEWER")
    assert set(staff.principal["capabilities"]) == {"data.read", "data.write", "messages.send"}
    assert viewer.principal["capabilities"] == ["data.read"]
    for s in (staff, viewer):
        r = s.get("/users")
        assert r.status_code == 403 and r.json()["issues"][0]["code"] == "CAPABILITY_MISSING"
        assert s.post("/users", {"username": uname("x"), "display_name": "x", "role": "VIEWER"}).status_code == 403


def test_extra_capabilities_only_for_staff_and_operator_role_cannot_be_created_over_http(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    body = {"username": uname("v"), "display_name": "v", "role": "VIEWER", "extra_capabilities": ["messages.send"]}
    assert a.admin.post("/users", body).status_code == 422
    body = {"username": uname("o"), "display_name": "o", "role": "PLATFORM_OPERATOR"}
    assert a.admin.post("/users", body).status_code == 422
    body = {"username": uname("p"), "display_name": "p", "role": "STAFF", "password": "chosen"}
    assert a.admin.post("/users", body).status_code == 422  # passwords are generated, never chosen by the caller


def test_the_operator_has_no_fpo_and_cannot_use_tenant_endpoints(engine: Engine) -> None:
    name = uname("ops")
    _, temp = cli.create_operator(name, "Operator")
    first_login_and_set_password(name, temp)
    s = Session(name)
    assert s.principal["fpo_id"] is None and s.principal["capabilities"] == ["platform.admin"]
    assert_valid("principal", s.principal)
    assert s.get("/users").status_code == 403


def test_cli_refuses_a_real_fpo(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["create-fpo", "--name", "Real FPO", "--admin-username", "real.admin"]) == 2


# ---- two FPOs must never see each other --------------------------------------------------------------------------
def test_each_fpo_lists_only_its_own_users(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    ua, _, _ = add_user(a, "STAFF", signed_in=False)
    ub, _, _ = add_user(b, "VIEWER", signed_in=False)
    ids_a = {u["id"] for u in a.admin.get("/users").json()}
    ids_b = {u["id"] for u in b.admin.get("/users").json()}
    assert ua in ids_a and ub not in ids_a and ub in ids_b and ua not in ids_b
    assert all(u["fpo_id"] == a.fpo_id for u in a.admin.get("/users").json())
    for u in a.admin.get("/users").json():
        assert_valid("user", u)
        assert set(u) == {
            "contract",
            "id",
            "fpo_id",
            "username",
            "display_name",
            "role",
            "extra_capabilities",
            "language",
            "active",
            "must_change_password",
            "origin",
            "created_at",
        }  # no hash, no password field


def test_another_fpos_user_looks_exactly_like_a_missing_user_and_is_audited(
    engine: Engine, two: tuple[Fpo, Fpo]
) -> None:
    a, b = two
    ua, _, _ = add_user(a, "STAFF", signed_in=False)
    other = b.admin.get(f"/users/{ua}")
    missing = b.admin.get("/users/usr_00000000000000000000000999")
    assert other.status_code == missing.status_code == 404
    assert strip(other) == strip(missing)
    assert audit_events(engine, actor_user_id=b.admin_id).count("CROSS_TENANT_REFERENCE") == 1  # only the real one


def test_no_write_endpoint_reaches_another_fpos_user(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    ua, name_a, pw_a = add_user(a, "STAFF", signed_in=False)
    with engine.connect() as c:
        before = c.execute(text("SELECT password_hash, active FROM users WHERE id = :i"), {"i": ua}).one()
    assert b.admin.post(f"/users/{ua}/reset-password").status_code == 404
    assert b.admin.post(f"/users/{ua}/deactivate").status_code == 404
    with engine.connect() as c:
        after = c.execute(text("SELECT password_hash, active FROM users WHERE id = :i"), {"i": ua}).one()
    assert tuple(after) == tuple(before) and after.active is True
    assert TestClient(app).post("/api/v1/auth/login", json={"username": name_a, "password": pw_a}).status_code == 200


def test_create_user_rejects_an_fpo_field_and_scopes_to_the_caller(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    r = a.admin.post("/users", {"username": uname("z"), "display_name": "z", "role": "STAFF", "fpo_id": b.fpo_id})
    assert r.status_code == 422
    r = a.admin.post("/users", {"username": uname("z"), "display_name": "z", "role": "STAFF"})
    assert r.status_code == 201 and r.json()["user"]["fpo_id"] == a.fpo_id
    assert_valid("temporary_password_response", r.json())


def test_usernames_are_unique_across_the_platform(two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    name = uname("shared")
    assert a.admin.post("/users", {"username": name, "display_name": "x", "role": "VIEWER"}).status_code == 201
    r = b.admin.post("/users", {"username": name, "display_name": "x", "role": "VIEWER"})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


def test_an_admin_cannot_deactivate_themselves(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    assert a.admin.post(f"/users/{a.admin_id}/deactivate").status_code == 409


def test_reset_password_forces_a_change_and_ends_the_users_sessions(two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    uid, name, s = add_user(a, "STAFF")
    r = a.admin.post(f"/users/{uid}/reset-password")
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    assert s.get("/auth/me").status_code == 401
    again = Session(name, r.json()["temporary_password"])
    assert again.principal["must_change_password"] is True


# ---- the database enforces it too, even if the application were wrong ---------------------------------------------
def as_app(engine: Engine, fpo: str | None, sql: str, **params: Any) -> list[Any]:
    with engine.begin() as c:
        c.execute(text("SET LOCAL ROLE kisaniti_app"))
        if fpo is not None:
            c.execute(text("SELECT set_config('app.fpo_id', :f, true)"), {"f": fpo})
        result = c.execute(text(sql), params)
        return list(result) if result.returns_rows else []


def test_restricted_role_sees_no_tenant_rows_without_a_tenant_and_only_its_own_with_one(
    engine: Engine, two: tuple[Fpo, Fpo]
) -> None:
    a, b = two
    add_user(a, "STAFF", signed_in=False)
    assert as_app(engine, None, "SELECT id FROM users") == []
    assert as_app(engine, "", "SELECT id FROM users") == []
    assert as_app(engine, None, "SELECT id FROM audit_log") == []
    rows_a = as_app(engine, a.fpo_id, "SELECT DISTINCT fpo_id FROM users")
    assert [r[0] for r in rows_a] == [a.fpo_id]
    rows_b = as_app(engine, b.fpo_id, "SELECT DISTINCT fpo_id FROM audit_log")
    assert [r[0] for r in rows_b] == [b.fpo_id]


def test_restricted_role_cannot_write_another_fpos_rows(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, b = two
    cols = "(id, fpo_id, username, display_name, role, must_change_password, password_hash, origin)"
    vals = "(:i, :f, :u, 'x', 'VIEWER', true, 'h', 'SYNTHETIC')"
    with pytest.raises(DBAPIError, match="row-level security"):
        as_app(
            engine,
            a.fpo_id,
            f"INSERT INTO users {cols} VALUES {vals}",
            i="usr_0000000000000000000000ZZZZ",
            f=b.fpo_id,
            u=uname("w"),
        )
    ub = b.admin_id
    as_app(engine, a.fpo_id, "UPDATE users SET active = false WHERE id = :i", i=ub)  # silently matches no row
    with engine.connect() as c:
        assert c.execute(text("SELECT active FROM users WHERE id = :i"), {"i": ub}).scalar_one() is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT password_hash FROM users",
        "SELECT * FROM users",
        "SELECT token_sha256 FROM sessions",
        "SELECT * FROM login_attempts",
        "UPDATE users SET role = 'FPO_ADMIN'",
        "UPDATE users SET fpo_id = NULL",
    ],
)
def test_restricted_role_cannot_touch_secrets_or_privileged_columns(
    engine: Engine, two: tuple[Fpo, Fpo], sql: str
) -> None:
    a, _ = two
    with pytest.raises(DBAPIError):
        as_app(engine, a.fpo_id, sql)


def test_audit_log_is_append_only_even_for_the_owner(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE audit_log SET event = 'LOGOUT'"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("DELETE FROM audit_log"))
    with pytest.raises(DBAPIError):
        as_app(engine, a.fpo_id, "DELETE FROM audit_log")


def test_audit_trail_records_the_access_events_without_secrets(engine: Engine, two: tuple[Fpo, Fpo]) -> None:
    a, _ = two
    TestClient(app).post("/api/v1/auth/login", json={"username": a.admin_name, "password": "bad password"})
    TestClient(app).post("/api/v1/auth/login", json={"username": uname("ghost"), "password": "bad password"})
    with engine.connect() as c:
        rows = c.execute(text("SELECT * FROM audit_log ORDER BY id DESC LIMIT 20")).mappings().all()
    events = {r["event"] for r in rows}
    assert "LOGIN_FAILURE" in events
    assert all(
        set(r.keys()) == {"id", "occurred_at", "request_id", "event", "actor_user_id", "fpo_id", "subject_user_id"}
        for r in rows
    )
    unknown = [r for r in rows if r["event"] == "LOGIN_FAILURE" and r["fpo_id"] is None]
    assert unknown and all(r["actor_user_id"] is None and r["subject_user_id"] is None for r in unknown)
    sample = rows[0]
    assert_valid(
        "audit_event",
        {
            "contract": "access.audit_event/v1",
            "id": sample["id"],
            "occurred_at": sample["occurred_at"].strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{sample['occurred_at'].microsecond // 1000:03d}Z",
            "request_id": sample["request_id"],
            "event": sample["event"],
            "actor_user_id": sample["actor_user_id"],
            "fpo_id": sample["fpo_id"],
            "subject_user_id": sample["subject_user_id"],
        },
    )
