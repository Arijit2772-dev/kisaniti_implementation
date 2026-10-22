"""Provenance and real/synthetic labels (CONTRACT_SPEC_V1_shared.md §5)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .errors import ContractError, ErrorCode


class Origin(StrEnum):
    REAL = "REAL"
    POLICY = "POLICY"
    SYNTHETIC = "SYNTHETIC"


class Mode(StrEnum):
    RESEARCH = "RESEARCH"
    DEMO = "DEMO"
    LIVE = "LIVE"


class SourceKind(StrEnum):
    AGMARKNET = "AGMARKNET"
    MSP_SCHEDULE = "MSP_SCHEDULE"
    FIXTURE_GENERATOR = "FIXTURE_GENERATOR"
    SIMULATOR = "SIMULATOR"
    SENSOR = "SENSOR"
    MANUAL_ENTRY = "MANUAL_ENTRY"
    ROUTE_PROVIDER = "ROUTE_PROVIDER"
    DERIVED = "DERIVED"


@dataclass(frozen=True, slots=True)
class Generator:
    script: str
    version: str
    seed: int


@dataclass(frozen=True, slots=True)
class Provenance:
    origin: Origin
    source_kind: SourceKind
    source_ref: str
    acquired_at: str
    source_version: str | None = None
    generator: Generator | None = None

    def __post_init__(self) -> None:
        if not self.source_ref:
            raise ContractError(ErrorCode.VALIDATION_FAILED, "provenance.source_ref is empty")
        if self.origin is Origin.SYNTHETIC and self.generator is None:
            raise ContractError(
                ErrorCode.VALIDATION_FAILED, "SYNTHETIC provenance needs a generator (script, version, seed)"
            )


def derived_origin(origins: Iterable[Origin]) -> Origin:
    """Taint rule: SYNTHETIC if any input is SYNTHETIC, otherwise REAL."""
    items = list(origins)
    if not items:
        raise ContractError(ErrorCode.VALIDATION_FAILED, "a derived object needs at least one input")
    return Origin.SYNTHETIC if Origin.SYNTHETIC in items else Origin.REAL


def check_mode(mode: Mode, origins: Iterable[Origin]) -> None:
    """LIVE runs refuse synthetic inputs; there is no silent fallback to DEMO."""
    if mode is Mode.LIVE and Origin.SYNTHETIC in set(origins):
        raise ContractError(ErrorCode.SYNTHETIC_INPUT_IN_LIVE, "LIVE mode received SYNTHETIC input")


def counts_for_accuracy(origin: Origin) -> bool:
    """Only REAL observations count toward accuracy or benefit metrics (POLICY values never do)."""
    return origin is Origin.REAL
