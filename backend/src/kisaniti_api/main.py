"""FastAPI application. Run: uvicorn kisaniti_api.main:app"""

from __future__ import annotations

from fastapi import FastAPI
from kisaniti_core import __version__ as core_version

from . import __version__, health, problems
from .access import routes as access_routes
from .config import load_settings
from .devices import routes as device_routes
from .market import routes as market_routes
from .org import routes as org_routes
from .plan import routes as plan_routes

app = FastAPI(title="KisanNiti API", version=__version__)
problems.install(app)
app.include_router(health.router)
access_routes.install(app)
device_routes.install(app)
org_routes.install(app)
market_routes.install(app)
plan_routes.install(app)


@app.get("/api/v1/meta")
def meta() -> dict[str, object]:
    """Versions and mode, so the UI can show the DEMO banner and the paper can cite exact versions."""
    return {
        "api_version": __version__,
        "core_version": core_version,
        "contracts": ["shared/v1", "access/v1", "devices/v1", "org/v1", "market/v1", "plan/v1"],
        "mode": load_settings().mode.value,
    }
