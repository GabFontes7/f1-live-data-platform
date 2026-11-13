import pandas as pd
import pytest

from f1platform import schema as sch


def test_missing_session_lists_what_exists(tmp_path):
    (tmp_path / "2026-las-vegas-r").mkdir()
    with pytest.raises(FileNotFoundError, match="2026-las-vegas-r"):
        sch.session_dir("typo", tmp_path)


def test_describe_reports_rows_columns_and_nulls(tmp_path):
    frame = pd.DataFrame({"Abbreviation": ["NOR", "VER"], "Time": [1.0, None]})
    path = tmp_path / "results.parquet"
    frame.to_parquet(path, index=False)

    text = sch.describe_table(path)

    assert "2 rows x 2 columns" in text
    assert "1/2 non-null" in text


def test_tables_are_listed_in_a_stable_order(tmp_path):
    for name in ("laps", "results", "weather_data"):
        pd.DataFrame({"a": [1]}).to_parquet(tmp_path / f"{name}.parquet", index=False)

    assert [p.stem for p in sch.tables_in(tmp_path)] == ["laps", "results", "weather_data"]
