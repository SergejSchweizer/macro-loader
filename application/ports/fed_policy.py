"""Application-facing Fed policy snapshot source port."""

from __future__ import annotations

from datetime import date
from typing import Protocol

import polars as pl


class FedPolicySnapshotSource(Protocol):
    """Read the normalized, persisted EOD FedWatch snapshot layer."""

    def refresh(self, start: date, end: date) -> pl.DataFrame: ...

    def read(self) -> pl.DataFrame: ...
