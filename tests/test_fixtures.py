"""Tests for the fixture exporter.

The fixture is what CI trusts, so the exporter must fail loudly rather than
quietly produce a narrower file than the warehouse needs.
"""

from __future__ import annotations

import pandas as pd
import pytest

from f1platform import fixtures as fx


def test_trim_keeps_only_the_listed_columns():
    frame = pd.DataFrame({"Driver": ["NOR"], "HeadshotUrl": ["http://x"], "LapTime": [72.6]})

    trimmed = fx.trim(frame, ["Driver", "LapTime"], "laps")

    assert list(trimmed.columns) == ["Driver", "LapTime"]


def test_trim_refuses_a_source_that_lost_a_column():
    frame = pd.DataFrame({"Driver": ["NOR"]})

    with pytest.raises(KeyError, match="LapTime"):
        fx.trim(frame, ["Driver", "LapTime"], "laps")


def test_a_missing_bronze_session_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="f1-ingest"):
        fx.export("absent", tmp_path, tmp_path / "fixtures")


def test_the_fixture_carries_every_column_the_warehouse_reads():
    """Drift guard: warehouse.py selects these by name from the fixture."""
    from f1platform import warehouse

    sql = warehouse.SILVER_RESULTS + warehouse.SILVER_LAPS
    for table, columns in fx.KEEP_COLUMNS.items():
        for column in columns:
            assert f'"{column}"' in sql, f"{table}.{column} is exported but unused"


def test_export_round_trips_a_session(tmp_path):
    bronze = tmp_path / "bronze" / "s"
    bronze.mkdir(parents=True)
    pd.DataFrame({c: ["x"] for c in fx.KEEP_COLUMNS["results"]}).to_parquet(
        bronze / "results.parquet", index=False
    )
    pd.DataFrame({c: ["x"] for c in fx.KEEP_COLUMNS["laps"]}).to_parquet(
        bronze / "laps.parquet", index=False
    )

    target = fx.export("s", tmp_path / "bronze", tmp_path / "fixtures")

    assert (target / "results.parquet").exists()
    assert (target / "laps.parquet").exists()
