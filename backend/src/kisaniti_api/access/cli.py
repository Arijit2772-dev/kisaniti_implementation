"""Operator commands: create a (fictional) FPO with its first admin, or a platform operator.

    python -m kisaniti_api.access.cli create-fpo --name "Demo FPO A (fictional)" --admin-username admin.a --synthetic
    python -m kisaniti_api.access.cli create-operator --username platform.ops

The temporary password is printed once and never stored in clear. Creating a real FPO with real people is refused
until the privacy contract's controls and a legal review exist (PRIVACY_CONTRACT, college demo rule).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime

from kisaniti_core.ids import new_id
from kisaniti_core.timeutil import format_instant
from sqlalchemy import text

from .. import __version__
from ..config import load_settings
from ..db import get_engine
from .config import load_access_settings
from .passwords import generate_temporary_password, hash_password
from .service import audit

INSERT_USER = (
    "INSERT INTO users (id, fpo_id, username, display_name, role, extra_capabilities, language, active, "
    "must_change_password, password_hash, origin) VALUES (:i, :f, :u, :d, :r, '{}', 'en', true, true, :h, :o)"
)


def create_fpo(name: str, admin_username: str, admin_display_name: str) -> tuple[str, str, str]:
    s = load_access_settings()
    eng = get_engine(load_settings().database_url)
    fpo_id, uid, temp = new_id("fpo"), new_id("usr"), generate_temporary_password()
    now = format_instant(datetime.now(UTC))
    prov = {
        "origin": "SYNTHETIC",
        "source_kind": "FIXTURE_GENERATOR",
        "source_ref": "kisaniti_api.access.cli create-fpo",
        "generator": {"script": "kisaniti_api.access.cli", "version": __version__, "seed": 0},
        "acquired_at": now,
    }
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fpo (id, name, timezone, origin, provenance) VALUES (:i, :n, 'Asia/Kolkata', "
                "'SYNTHETIC', CAST(:p AS jsonb))"
            ),
            {"i": fpo_id, "n": name, "p": json.dumps(prov)},
        )
        conn.execute(
            text(INSERT_USER),
            {
                "i": uid,
                "f": fpo_id,
                "u": admin_username,
                "d": admin_display_name,
                "r": "FPO_ADMIN",
                "h": hash_password(temp, s.scrypt_n),
                "o": "SYNTHETIC",
            },
        )
        audit(conn, f"cli-{secrets.token_hex(6)}", "USER_CREATED", fpo_id=fpo_id, subject=uid)
    return fpo_id, uid, temp


def create_operator(username: str, display_name: str) -> tuple[str, str]:
    s = load_access_settings()
    eng = get_engine(load_settings().database_url)
    uid, temp = new_id("usr"), generate_temporary_password()
    with eng.begin() as conn:
        conn.execute(
            text(INSERT_USER),
            {
                "i": uid,
                "f": None,
                "u": username,
                "d": display_name,
                "r": "PLATFORM_OPERATOR",
                "h": hash_password(temp, s.scrypt_n),
                "o": "REAL",
            },
        )
        audit(conn, f"cli-{secrets.token_hex(6)}", "USER_CREATED", subject=uid)
    return uid, temp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kisaniti_api.access.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("create-fpo")
    f.add_argument("--name", required=True)
    f.add_argument("--admin-username", required=True)
    f.add_argument("--admin-display-name", default="FPO admin (fictional)")
    f.add_argument(
        "--synthetic", action="store_true", help="required: only fictional FPOs until the privacy controls exist"
    )
    o = sub.add_parser("create-operator")
    o.add_argument("--username", required=True)
    o.add_argument("--display-name", default="Platform operator")
    a = ap.parse_args(argv)
    if a.cmd == "create-fpo":
        if not a.synthetic:
            print(
                "refused: real FPOs need the privacy controls and a legal review first; use --synthetic",
                file=sys.stderr,
            )
            return 2
        fpo_id, uid, temp = create_fpo(a.name, a.admin_username, a.admin_display_name)
        print(
            json.dumps(
                {"fpo_id": fpo_id, "admin_user_id": uid, "username": a.admin_username, "temporary_password": temp}
            )
        )
    else:
        uid, temp = create_operator(a.username, a.display_name)
        print(json.dumps({"user_id": uid, "username": a.username, "temporary_password": temp}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
