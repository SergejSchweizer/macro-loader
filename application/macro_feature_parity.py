"""Deterministic checks that the PostgreSQL feature projection covers the catalog."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MacroFeatureParityReport:
    """Machine-readable result for the SQL/catalog parity gate."""

    compared_columns: tuple[str, ...]
    missing_columns: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.missing_columns


def compare_catalog_to_sql(
    sql_definition: str, feature_columns: tuple[str, ...]
) -> MacroFeatureParityReport:
    """Report every catalog column absent from the source-controlled SQL projection.

    This deliberately uses identifier boundaries, so similarly named columns cannot
    mask a dropped or renamed projection.  Numerical parity is exercised by the
    real-PostgreSQL integration suite; this helper keeps the report deterministic
    and independent of a live database.
    """
    compared: list[str] = []
    missing: list[str] = []
    for column in feature_columns:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])", sql_definition):
            compared.append(column)
        else:
            missing.append(column)
    return MacroFeatureParityReport(tuple(compared), tuple(missing))
