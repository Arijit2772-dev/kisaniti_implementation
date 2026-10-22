"""Every example in contracts/<slice>/v<n>/examples/valid must pass its schema; every invalid one must fail.

Run from the core environment: ``core/.venv/bin/pytest contracts/tests``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SLICES = sorted(p for p in ROOT.glob("*/v*") if (p / "examples" / "index.json").exists())


def _registry(slice_dir: Path) -> Registry:  # type: ignore[type-arg]
    reg: Registry = Registry()  # type: ignore[type-arg]
    for schema_dir in {slice_dir, ROOT / "shared" / "v1"}:
        for f in schema_dir.glob("*.schema.json"):
            doc = json.loads(f.read_text())
            Draft202012Validator.check_schema(doc)
            reg = reg.with_resource(doc["$id"], Resource.from_contents(doc))
    return reg


def _validator(slice_dir: Path, ref: str) -> Draft202012Validator:
    base = json.loads((slice_dir / ref.split("#")[0]).read_text())["$id"]
    target = base + ("#" + ref.split("#", 1)[1] if "#" in ref else "")
    return Draft202012Validator({"$ref": target}, registry=_registry(slice_dir))


def _cases() -> list[tuple[Path, Path, str, bool]]:
    out = []
    for s in SLICES:
        index = json.loads((s / "examples" / "index.json").read_text())
        for kind in ("valid", "invalid"):
            for f in sorted((s / "examples" / kind).glob("*.json")):
                refs = [r for p, r in index.items() if not p.startswith("_") and f.name.startswith(p)]
                assert len(refs) == 1, f"{f} matches {len(refs)} index prefixes"
                out.append((s, f, refs[0], kind == "valid"))
    return out


CASES = _cases()


def test_every_slice_has_examples() -> None:
    assert SLICES, "no contract slices found"
    for s in SLICES:
        assert any((s / "examples" / "valid").glob("*.json")), f"{s} has no valid examples"
        assert any((s / "examples" / "invalid").glob("*.json")), f"{s} has no invalid examples"


@pytest.mark.parametrize(("slice_dir", "path", "ref", "should_pass"), CASES, ids=[f"{c[0].parent.name}/{c[1].parent.name}/{c[1].name}" for c in CASES])
def test_example(slice_dir: Path, path: Path, ref: str, should_pass: bool) -> None:
    v = _validator(slice_dir, ref)
    doc: Any = json.loads(path.read_text())
    items = doc if isinstance(doc, list) else [doc]
    errors = [list(v.iter_errors(item)) for item in items]
    if should_pass:
        assert all(not e for e in errors), [str(x[0].message) for x in errors if x]
    else:
        assert any(e for e in errors), f"{path.name} unexpectedly valid"


def _primitive_cases() -> list[tuple[str, Any, str]]:
    prim = json.loads((ROOT / "shared" / "v1" / "examples" / "primitives.json").read_text())
    return [(name, value, kind) for name, groups in prim.items() if not name.startswith("_")
            for kind in ("valid", "invalid", "invalid_semantic") for value in groups[kind]]


@pytest.mark.parametrize(("name", "value", "kind"), _primitive_cases())
def test_primitive_schema(name: str, value: Any, kind: str) -> None:
    v = _validator(ROOT / "shared" / "v1", f"common.schema.json#/$defs/{name}")
    ok = v.is_valid(value)
    assert ok == (kind != "invalid"), f"{name} {value!r} ({kind})"
