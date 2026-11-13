"""Tests for the gold rules, on data built to break them.

The acceptance test proves the pipeline reproduces one real race. These
prove the rules hold in the cases that race happened not to contain -- a
deleted lap quicker than every legal one, an unclassified driver who was
quicker still, a lapped finisher. CI runs these without any downloaded data.
"""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from f1platform import warehouse

SESSION = "2026-test-grand-prix-r"


def write_bronze(tmp_path, results: pd.DataFrame, laps: pd.DataFrame):
    session_dir = tmp_path / SESSION
    session_dir.mkdir(parents=True, exist_ok=True)
    results.to_parquet(session_dir / "results.parquet", index=False)
    laps.to_parquet(session_dir / "laps.parquet", index=False)
    return tmp_path


def make_results() -> pd.DataFrame:
    """Three drivers: a winner, a lapped finisher, and a retirement."""
    return pd.DataFrame(
        {
            "DriverNumber": ["1", "2", "3"],
            "Abbreviation": ["WIN", "LAP", "OUT"],
            "FullName": ["Winner One", "Lapped Two", "Out Three"],
            "TeamName": ["Alpha", "Beta", "Gamma"],
            "Position": [1.0, 2.0, 3.0],
            "ClassifiedPosition": ["1", "2", "R"],
            "GridPosition": [3.0, 1.0, 2.0],
            "Status": ["Finished", "Lapped", "Retired"],
            "Points": [25.0, 18.0, 0.0],
            "Laps": [71.0, 70.0, 12.0],
            "Time": [5723.504, 4.427, None],
        }
    )


def make_laps() -> pd.DataFrame:
    """A legal quick lap, a quicker deleted one, and a quicker unclassified one."""
    return pd.DataFrame(
        {
            "Driver": ["WIN", "LAP", "OUT", "WIN"],
            "LapNumber": [10.0, 11.0, 12.0, 13.0],
            "LapTime": [72.689, 70.100, 69.000, 75.000],
            "Stint": [1.0, 1.0, 1.0, 2.0],
            "Compound": ["MEDIUM", "SOFT", "SOFT", "HARD"],
            "TyreLife": [5.0, 6.0, 7.0, 1.0],
            "FreshTyre": [False, False, True, True],
            "TrackStatus": ["1", "1", "1", "4"],
            "Deleted": [False, True, False, False],
            "DeletedReason": ["", "track limits", "", ""],
            "IsAccurate": [True, True, True, False],
            "PitInTime": [None, None, None, 1234.0],
            "PitOutTime": [None, None, None, None],
        }
    )


@pytest.fixture
def built(tmp_path):
    bronze = write_bronze(tmp_path / "bronze", make_results(), make_laps())
    db_path = tmp_path / "w.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    yield connection
    connection.close()


def test_a_retired_driver_is_not_classified(built):
    row = built.execute(
        "SELECT is_classified, finishing_position FROM gold_race_classification "
        "WHERE driver_code = 'OUT'"
    ).fetchone()

    assert row == (False, None)


def test_a_lapped_finisher_is_still_classified(built):
    row = built.execute(
        "SELECT is_classified, finishing_position FROM gold_race_classification "
        "WHERE driver_code = 'LAP'"
    ).fetchone()

    assert row == (True, 2)


def test_a_deleted_lap_cannot_win_the_fastest_lap(built):
    code, seconds = built.execute(
        "SELECT driver_code, lap_time_seconds FROM gold_fastest_lap"
    ).fetchone()

    assert code == "WIN", "70.100 was quicker but deleted; 69.000 was unclassified"
    assert seconds == pytest.approx(72.689)


def test_only_the_winner_keeps_a_race_duration(built):
    rows = built.execute(
        "SELECT driver_code, race_duration_seconds, gap_to_winner_seconds "
        "FROM gold_race_classification ORDER BY finishing_order"
    ).fetchall()

    assert rows[0][1] == pytest.approx(5723.504), "the winner carries the duration"
    assert rows[1][1] is None, "nobody else does"
    assert rows[1][2] is None, "and a lapped driver's Time is not a gap"


def test_positions_gained_uses_the_grid(built):
    gained = built.execute(
        "SELECT positions_gained FROM gold_race_classification "
        "WHERE driver_code = 'WIN'"
    ).fetchone()[0]

    assert gained == 2, "started third, won"


def test_in_and_out_laps_are_flagged(built):
    in_laps = built.execute(
        "SELECT count(*) FROM silver_laps WHERE is_in_lap"
    ).fetchone()[0]

    assert in_laps == 1


def test_a_missing_bronze_session_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="f1-ingest"):
        warehouse.build("absent", tmp_path, tmp_path / "w.duckdb")
