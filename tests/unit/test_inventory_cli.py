from __future__ import annotations

import io
import json
import logging
from datetime import date

import pytest

from api.inventory import INVENTORY_FIELDS, run_inventory
from application.contracts import Provider
from application.operational_records import InventoryRecord


def _records() -> list[InventoryRecord]:
    return [
        InventoryRecord(
            series_id="us_10y",
            provider=Provider.FRED,
            min_observation_date=date(1962, 1, 2),
            max_observation_date=date(2026, 8, 19),
            row_count=100,
            duplicate_key_count=0,
            file_count=12,
        ),
        InventoryRecord(
            series_id="vix",
            provider=Provider.CBOE,
            min_observation_date=date(1990, 1, 2),
            max_observation_date=date(2026, 8, 19),
            row_count=200,
            duplicate_key_count=0,
            file_count=20,
        ),
    ]


def _run(argv: list[str], reader=lambda: _records()) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_inventory(argv, reader=reader, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def test_text_render_has_exact_stable_fields_and_deterministic_order() -> None:
    code, output, error = _run([])
    assert code == 0
    assert error == ""
    lines = output.splitlines()
    assert lines[0].split("\t") == list(INVENTORY_FIELDS)
    assert len(lines) == 3
    assert lines[1].split("\t") == [
        "us_10y",
        "fred",
        "1962-01-02",
        "2026-08-19",
        "100",
        "0",
        "12",
    ]
    assert lines[2].split("\t")[0] == "vix"


def test_repeatable_series_and_provider_filters_are_registry_validated() -> None:
    code, output, error = _run(["--series", "vix", "--provider", "cboe"])
    assert code == 0
    assert error == ""
    lines = output.splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("vix\tcboe\t")

    code, output, error = _run(["--series", "missing"])
    assert code != 0
    assert output == ""
    assert "unknown series filter" in error

    code, output, error = _run(["--provider", "missing"])
    assert code != 0
    assert output == ""
    assert "unknown provider filter" in error


def test_json_has_same_logical_fields_order_and_values_as_text() -> None:
    text_code, text, _ = _run(["--series", "us_10y"])
    json_code, encoded, _ = _run(["--series", "us_10y", "--json"])
    assert text_code == json_code == 0
    data = json.loads(encoded)
    assert list(data[0]) == list(INVENTORY_FIELDS)
    assert data[0] == {
        "series_id": "us_10y",
        "provider": "fred",
        "min_observation_date": "1962-01-02",
        "max_observation_date": "2026-08-19",
        "row_count": 100,
        "duplicate_key_count": 0,
        "file_count": 12,
    }
    text_values = text.splitlines()[1].split("\t")
    assert text_values[:4] == [
        data[0]["series_id"],
        data[0]["provider"],
        data[0]["min_observation_date"],
        data[0]["max_observation_date"],
    ]


def test_empty_result_is_success_for_text_and_json() -> None:
    code, output, error = _run([], reader=lambda: [])
    assert code == 0
    assert error == ""
    assert output == "\t".join(INVENTORY_FIELDS) + "\n"
    code, output, error = _run(["--json"], reader=lambda: [])
    assert code == 0
    assert error == ""
    assert output == "[]\n"


def test_inventory_rendering_logs_safe_format_and_row_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from api.inventory import render_json, render_text

    with caplog.at_level(logging.DEBUG, logger="api.inventory"):
        render_text(_records())
        render_json([])

    assert "inventory rendering started format=text record_count=2" in caplog.text
    assert "inventory rendering completed format=text record_count=2" in caplog.text
    assert "inventory rendering started format=json record_count=0" in caplog.text
    assert "inventory rendering completed format=json record_count=0" in caplog.text
    assert "us_10y" not in caplog.text
    assert "1962-01-02" not in caplog.text


def test_inventory_render_failure_logs_only_exception_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from api import inventory

    def fail(_record: InventoryRecord) -> dict[str, object]:
        raise ValueError("sensitive record payload")

    monkeypatch.setattr(inventory, "_logical_row", fail)
    with (
        caplog.at_level(logging.DEBUG, logger="api.inventory"),
        pytest.raises(ValueError, match="sensitive record payload"),
    ):
        inventory.render_text(_records())

    assert "inventory rendering failed format=text error_type=ValueError" in caplog.text
    assert "sensitive record payload" not in caplog.text
    assert "us_10y" not in caplog.text


def test_reader_or_schema_error_returns_nonzero_without_output() -> None:
    def broken() -> list[InventoryRecord]:
        raise ValueError("invalid inventory schema")

    code, output, error = _run([], reader=broken)
    assert code == 2
    assert output == ""
    assert "invalid inventory schema" in error

    def unreadable() -> list[InventoryRecord]:
        raise OSError("permission denied")

    code, output, error = _run([], reader=unreadable)
    assert code == 2
    assert output == ""
    assert "permission denied" in error
