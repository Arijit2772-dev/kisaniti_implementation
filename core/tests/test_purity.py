"""core/ must stay pure: no web, database, network or subprocess imports (CHAT-28, architecture §41)."""

import ast
from pathlib import Path

FORBIDDEN = {
    "fastapi",
    "starlette",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "asyncpg",
    "requests",
    "httpx",
    "urllib",
    "http",
    "socket",
    "aiohttp",
    "subprocess",
    "paho",
    "boto3",
    "django",
    "flask",
}
SRC = Path(__file__).resolve().parents[1] / "src" / "kisaniti_core"


def test_no_forbidden_imports() -> None:
    bad = []
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            bad += [f"{f.name}: {n}" for n in names if n.split(".")[0] in FORBIDDEN]
    assert not bad, bad
