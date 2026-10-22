"""Seeded SYNTHETIC allocation inputs for tests and solver benchmarks (labelled synthetic; never real data)."""

from __future__ import annotations

import random
from datetime import date, timedelta

from ..ids import fixture_id
from ..provenance import Mode, Origin
from ..quantity import Quantity, RatePerKg
from .model import AllocationInput, AllocationMode, Choice, Lot, MarketLoad, StorageCategory

NS = "kisaniti-alloc-random"


def random_instance(
    seed: int,
    farmers: int,
    lots: int,
    mandis: int,
    dates: int,
    density: float = 0.6,
    max_increments: int = 3,
    increment_kg: int = 100,
    capacity_share: float = 0.6,
    mode: AllocationMode = AllocationMode.PARTIAL,
    strict: bool = True,
    ties: bool = False,
) -> AllocationInput:
    """A random instance. ``capacity_share`` scales total mandi-day capacity relative to total supply.
    ``ties`` draws net rates from a tiny set so many business ties reach the tie-break."""
    rng = random.Random(seed)
    F = [fixture_id(NS, "mbr", f"{seed}-{i}") for i in range(farmers)]
    M = [fixture_id(NS, "mnd", f"{seed}-{i}") for i in range(mandis)]
    D = [date(2025, 10, 1) + timedelta(days=i) for i in range(dates)]
    inc = Quantity.from_decimal(increment_kg)
    L = []
    for i in range(lots):
        owner = F[i] if i < farmers else rng.choice(F)
        n = rng.randint(1, max_increments)
        q = inc * n
        if mode is AllocationMode.PARTIAL and rng.random() < 0.3:  # a remainder smaller than one increment
            q = q + Quantity.from_decimal(rng.randint(1, increment_kg - 1))
        cat = rng.choice(list(StorageCategory))
        L.append(Lot(fixture_id(NS, "lot", f"{seed}-{i}"), owner, q, inc, cat))
    choices = []
    for lot in L:
        keys = [(m, d) for m in M for d in D]
        picked = [k for k in keys if rng.random() < density] or [rng.choice(keys)]
        for m, d in picked:
            price = rng.choice([23_8900, 24_0000]) if ties else rng.randint(23_0000, 25_0000)
            cost = rng.choice([3000, 5000]) if ties else rng.randint(1000, 9000)
            choices.append(Choice(lot.id, m, d, RatePerKg(price), RatePerKg(cost)))
    supply = sum(lot.quantity.units for lot in L)
    per_slot = max(increment_kg * 1000, int(supply * capacity_share / max(1, mandis * dates)))
    caps = {
        (m, d): Quantity((per_slot // (increment_kg * 1000)) * increment_kg * 1000 + rng.choice([0, 0, 50_000]))
        for m in M
        for d in D
    }
    return AllocationInput(
        fixture_id(NS, "fpo", seed),
        fixture_id(NS, "cmd", "paddy"),
        Mode.RESEARCH,
        Origin.SYNTHETIC,
        mode,
        MarketLoad.STRICT if strict else MarketLoad.DISABLED,
        tuple(D),
        tuple(M),
        tuple(F),
        tuple(L),
        tuple(choices),
        caps,
    )
