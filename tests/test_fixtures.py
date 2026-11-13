"""Tests for the fixture exporter.

The fixture is what CI trusts and what any analysis explores, so it carries
every column the source gave. What the exporter must never do is quietly ship
a fixture missing a column the warehouse reads.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from f1platform import fixtures as fx


def minimal(table: str) -> pd.DataFrame:
    return pd.DataFrame({column: ["x"] for column in fx.REQUIRED_COLUMNS[table]})


def test_every_column_is_kept_by_default():
    frame = minimal("laps").assign(SpeedST=[300.0], Position=[1.0])

    prepared = fx.prepare(frame, "laps")

    assert "SpeedST" in prepared.columns
    assert "Position" in prepared.columns


def test_headshot_urls_are_excluded():
    frame = minimal("results").assign(HeadshotUrl=["http://example/x.png"])

    prepared = fx.prepare(frame, "results")

    assert "HeadshotUrl" not in prepared.columns
    assert "TeamName" in prepared.columns


def test_a_source_that_lost_a_required_column_is_refused():
    frame = minimal("laps").drop(columns=["LapTime"])

    with pytest.raises(fx.MissingColumnsError, match="LapTime"):
        fx.prepare(frame, "laps")


def test_a_table_with_no_required_columns_is_kept_whole():
    """Weather has no checklist; every column it brings is exported."""
    frame = pd.DataFrame({"AirTemp": [22.0], "Rainfall": [True]})

    prepared = fx.prepare(frame, "weather_data")

    assert list(prepared.columns) == ["AirTemp", "Rainfall"]


def test_a_missing_bronze_session_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="f1-ingest"):
        fx.export("absent", tmp_path, tmp_path / "fixtures")


def test_optional_tables_are_skipped_not_fatal(tmp_path):
    bronze = tmp_path / "bronze" / "s"
    bronze.mkdir(parents=True)
    minimal("results").to_parquet(bronze / "results.parquet", index=False)
    minimal("laps").to_parquet(bronze / "laps.parquet", index=False)

    target = fx.export("s", tmp_path / "bronze", tmp_path / "fixtures")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))

    assert set(manifest["tables"]) == {"results", "laps"}
    assert not (target / "weather_data.parquet").exists()


def test_a_session_with_no_known_tables_is_refused(tmp_path):
    bronze = tmp_path / "bronze" / "s"
    bronze.mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="held none of"):
        fx.export("s", tmp_path / "bronze", tmp_path / "fixtures")


def test_the_manifest_records_the_columns_exported(tmp_path):
    bronze = tmp_path / "bronze" / "s"
    bronze.mkdir(parents=True)
    minimal("results").to_parquet(bronze / "results.parquet", index=False)
    minimal("laps").assign(SpeedST=[1.0]).to_parquet(
        bronze / "laps.parquet", index=False
    )

    target = fx.export("s", tmp_path / "bronze", tmp_path / "fixtures")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))

    assert "SpeedST" in manifest["tables"]["laps"]["columns"]
