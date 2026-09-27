"""Canonical column contract shared by Fed feature producers and consumers."""

from __future__ import annotations

FED_POLICY_FEATURE_COLUMNS = (
    "fed_next_expected_move_bp",
    "fed_path_slope_m3_bp",
    "fed_next_uncertainty_bp",
    "fed_repricing_5obs_bp",
)

FED_POLICY_LINEAGE_COLUMN = "available_at_utc"
FED_POLICY_COLUMNS = ("timestamp_m1", *FED_POLICY_FEATURE_COLUMNS, FED_POLICY_LINEAGE_COLUMN)
