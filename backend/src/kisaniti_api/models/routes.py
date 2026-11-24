"""GET /api/v1/models/registry (models/v1): which forecasting model the product uses, and the frozen evidence why.

The document is built by scripts/make_model_registry.py from the research repository's committed result file and is
served unchanged. It is the same for every FPO (it holds no FPO data), but like all data it needs a signed-in user.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI

from ..access.deps import require
from ..access.service import Principal

router = APIRouter(prefix="/api/v1")
Read = Annotated[Principal, Depends(require("data.read"))]
REGISTRY = Path(__file__).with_name("registry_c0.json")


@cache
def registry() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(REGISTRY.read_text())
    return doc


@router.get("/models/registry")
def get_registry(_: Read) -> dict[str, Any]:
    return registry()


def install(app: FastAPI) -> None:
    app.include_router(router)
