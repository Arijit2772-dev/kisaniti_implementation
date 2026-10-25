"""A device registry that reads the `devices` table (decision CHAT-40, contract devices/v1).

It behaves like the dict the ingestion code already uses (`registry.get(device_id)`, `device_id in registry`), but
refreshes from PostgreSQL: at most every `ttl_s` seconds, and for a device it does not know at most every `miss_s`
seconds, so a device registered in the website is accepted within seconds, and a flood of messages from an unknown
device cannot hammer the database. Only active devices are listed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from .validate import Device

Fetch = Callable[[], Iterable[tuple[str, str]]]


class DbRegistry(Mapping[str, Device]):
    def __init__(
        self, fetch: Fetch, ttl_s: float = 30.0, miss_s: float = 5.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._fetch, self._ttl, self._miss, self._clock = fetch, ttl_s, miss_s, clock
        self._cache: dict[str, Device] = {}
        self._loaded = float("-inf")
        self._last_miss_refresh = float("-inf")

    def _refresh(self) -> None:
        self._cache = {d: Device(d, origin) for d, origin in self._fetch()}
        self._loaded = self._clock()

    def _maybe_refresh(self, key: str | None) -> None:
        now = self._clock()
        if now - self._loaded >= self._ttl:
            self._refresh()
        elif key is not None and key not in self._cache and now - self._last_miss_refresh >= self._miss:
            self._last_miss_refresh = now
            self._refresh()

    def __getitem__(self, key: str) -> Device:
        self._maybe_refresh(key)
        return self._cache[key]

    def __iter__(self) -> Iterator[str]:
        self._maybe_refresh(None)
        return iter(self._cache)

    def __len__(self) -> int:
        self._maybe_refresh(None)
        return len(self._cache)


def postgres_fetcher(dsn: str) -> Fetch:
    """A fetch function on its own autocommit connection (never the store's connection); reconnects after a failure."""
    import psycopg

    state: dict[str, Any] = {"conn": None}

    def fetch() -> list[tuple[str, str]]:
        for attempt in (1, 2):
            try:
                if state["conn"] is None:
                    state["conn"] = psycopg.connect(
                        dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
                    )
                conn = state["conn"]
                rows = conn.execute("SELECT device_id, source_origin FROM devices WHERE active").fetchall()
                return [(str(r[0]), str(r[1])) for r in rows]
            except psycopg.Error:
                state["conn"] = None
                if attempt == 2:
                    raise
        return []

    return fetch
