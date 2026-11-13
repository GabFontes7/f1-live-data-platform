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


def lap(driver, number, seconds, *, status="1", deleted=False,
        pit_in=None, pit_out=None, compound="SOFT", tyre_life=None,
        position=None, stint=1):
    """One lap row carrying every column the silver layer reads."""
    return {
        "Driver": driver,
        "DriverNumber": "1",
        "Team": "Alpha",
        "LapNumber": float(number),
        "LapTime": seconds,
        "Time": float(number) * 90.0,
        "LapStartTime": float(number) * 90.0 - seconds,
        "Position": float(position if position is not None else 1),
        "Stint": float(stint),
        "Compound": compound,
        "TyreLife": float(tyre_life if tyre_life is not None else number),
        "FreshTyre": False,
        "Sector1Time": seconds / 3,
        "Sector2Time": seconds / 3,
        "Sector3Time": seconds / 3,
        "SpeedI1": 250.0,
        "SpeedI2": 260.0,
        "SpeedFL": 270.0,
        "SpeedST": 300.0,
        "IsPersonalBest": False,
        "TrackStatus": status,
        "Deleted": deleted,
        "DeletedReason": "track limits" if deleted else "",
        "IsAccurate": True,
        "FastF1Generated": False,
        "PitInTime": pit_in,
        "PitOutTime": pit_out,
    }


def make_results() -> pd.DataFrame:
    """Three drivers: a winner, a lapped finisher, and a retirement."""
    return pd.DataFrame(
        {
            "DriverNumber": ["1", "2", "3"],
            "Abbreviation": ["WIN", "LAP", "OUT"],
            "BroadcastName": ["W ONE", "L TWO", "O THREE"],
            "DriverId": ["one", "two", "three"],
            "CountryCode": ["BRA", "BRA", "BRA"],
            "FullName": ["Winner One", "Lapped Two", "Out Three"],
            "TeamName": ["Alpha", "Beta", "Gamma"],
            "TeamColor": ["FF0000", "00FF00", "0000FF"],
            "TeamId": ["alpha", "beta", "gamma"],
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
        [
            lap("WIN", 10, 72.689, compound="MEDIUM", tyre_life=5),
            lap("LAP", 11, 70.100, deleted=True, tyre_life=6),
            lap("OUT", 12, 69.000, tyre_life=7),
            lap("WIN", 13, 75.000, status="4", pit_in=1234.0, compound="HARD",
                tyre_life=1),
        ]
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


def test_a_lapped_driver_keeps_his_lap_deficit(built):
    """Hiding the number loses information; naming it honestly does not."""
    down, gap, behind = built.execute(
        """
        SELECT laps_down, gap_to_winner_seconds, time_behind_seconds
        FROM gold_race_classification WHERE driver_code = 'LAP'
        """
    ).fetchone()

    assert down == 1, "one lap behind the winner"
    assert gap is None, "a true gap to the winner exists only on the lead lap"
    assert behind == pytest.approx(4.427), "time behind, with whole laps taken out"


def test_an_unclassified_driver_has_no_lap_deficit(built):
    """A retirement is not 59 laps down, it is out."""
    down = built.execute(
        "SELECT laps_down FROM gold_race_classification WHERE driver_code = 'OUT'"
    ).fetchone()[0]

    assert down is None


def test_the_winner_is_zero_laps_down(built):
    down = built.execute(
        "SELECT laps_down FROM gold_race_classification WHERE driver_code = 'WIN'"
    ).fetchone()[0]

    assert down == 0


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


# --- pace: which laps count, and what the trend actually measures ----------


def make_pace_results() -> pd.DataFrame:
    """Two teammates plus a driver whose race ended early."""
    return pd.DataFrame(
        {
            "DriverNumber": ["1", "2", "3"],
            "Abbreviation": ["FST", "SLW", "DNF"],
            "BroadcastName": ["F ONE", "S TWO", "D THREE"],
            "DriverId": ["fst", "slw", "dnf"],
            "CountryCode": ["BRA", "BRA", "BRA"],
            "FullName": ["Fast One", "Slow Two", "Broken Three"],
            "TeamName": ["Alpha", "Alpha", "Beta"],
            "TeamColor": ["FF0000", "FF0000", "0000FF"],
            "TeamId": ["alpha", "alpha", "beta"],
            "Position": [1.0, 2.0, 3.0],
            "ClassifiedPosition": ["1", "2", "R"],
            "GridPosition": [1.0, 2.0, 3.0],
            # The slower teammate finishes a lap down, so the report has a
            # lap deficit to render and the delta still has two full stints.
            "Status": ["Finished", "Lapped", "Retired"],
            "Points": [25.0, 18.0, 0.0],
            "Laps": [10.0, 9.0, 2.0],
            "Time": [900.0, 5.0, None],
        }
    )


@pytest.fixture
def pace(tmp_path):
    rows = [lap("FST", n, 90.0 + n * 0.1) for n in range(1, 7)]
    rows += [lap("SLW", n, 91.0 + n * 0.1) for n in range(1, 7)]
    rows += [
        lap("FST", 7, 89.0, status="12"),          # a yellow appeared
        lap("FST", 8, 88.0, deleted=True),         # deleted
        lap("FST", 9, 95.0, pit_in=1.0),           # in-lap
        lap("FST", 10, 96.0, pit_out=1.0),         # out-lap
    ]
    rows += [lap("DNF", n, 92.0) for n in range(1, 3)]  # too few laps
    bronze = write_bronze(tmp_path / "bronze", make_pace_results(), pd.DataFrame(rows))
    db_path = tmp_path / "pace.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    yield connection
    connection.close()


def test_only_green_uninterrupted_laps_are_representative(pace):
    excluded = pace.execute(
        """
        SELECT lap_number FROM gold_representative_laps
        WHERE driver_code = 'FST' AND NOT is_representative
        ORDER BY lap_number
        """
    ).fetchall()

    assert [row[0] for row in excluded] == [7, 8, 9, 10], (
        "yellow, deleted, in-lap and out-lap must all be excluded"
    )


def test_a_yellow_flag_lap_is_not_green(pace):
    is_green = pace.execute(
        "SELECT is_green FROM gold_representative_laps "
        "WHERE driver_code = 'FST' AND lap_number = 7"
    ).fetchone()[0]

    assert is_green is False, "TrackStatus '12' means a yellow appeared mid-lap"


def test_a_swap_during_a_pit_cycle_is_not_a_pass_on_track(tmp_path):
    """Most raw position swaps are pit stops. The flag keeps them apart."""
    rows = [
        lap("FST", 1, 90.0, position=1.0), lap("SLW", 1, 91.0, position=2.0),
        lap("DNF", 1, 92.0, position=3.0),
        # Lap 2: FST pits, so both cars moving ahead of him are pit traffic.
        lap("FST", 2, 99.0, position=3.0, pit_in=1.0),
        lap("SLW", 2, 90.0, position=1.0),
        lap("DNF", 2, 91.0, position=2.0),
        # Lap 3: DNF takes SLW with nobody in the pit lane. A real pass.
        lap("FST", 3, 92.0, position=3.0),
        lap("SLW", 3, 91.0, position=2.0),
        lap("DNF", 3, 90.0, position=1.0),
    ]
    bronze = write_bronze(tmp_path / "bronze", make_pace_results(), pd.DataFrame(rows))
    db_path = warehouse.build(SESSION, bronze, tmp_path / "c.duckdb")
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        swaps = connection.execute(
            "SELECT passer, passed, is_on_track FROM gold_position_changes "
            "ORDER BY passer, passed"
        ).fetchall()
    finally:
        connection.close()

    assert swaps == [
        ("DNF", "FST", False),
        ("DNF", "SLW", True),
        ("SLW", "FST", False),
    ]


def test_the_profile_counts_what_happened_in_the_session(tmp_path):
    """One row saying what the session was, so the report can decide."""
    rows = []
    for driver, offset in (("FST", 0.0), ("SLW", 1.0), ("DNF", 2.0)):
        for number in range(1, 6):
            status = "14" if number == 3 else "1"
            rows.append(lap(driver, number, 90.0 + offset, status=status,
                            position=1 + offset))
    rows.append(lap("SLW", 6, 95.0, pit_in=1.0, position=2.0))
    bronze = write_bronze(tmp_path / "bronze", make_pace_results(), pd.DataFrame(rows))
    db_path = warehouse.build(SESSION, bronze, tmp_path / "p.duckdb")
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        profile = connection.execute(
            "SELECT laps_run, safety_car_laps, wet_laps, pit_stops, winner "
            "FROM gold_session_profile"
        ).fetchone()
    finally:
        connection.close()

    assert profile == (6, 1, 0, 1, "FST")


def test_the_track_is_wet_when_most_cars_are_on_wet_tyres(tmp_path):
    """The track's state is read from the tyres, lap by lap."""
    rows = [lap("FST", 1, 95.0, compound="INTERMEDIATE"),
            lap("SLW", 1, 96.0, compound="INTERMEDIATE"),
            lap("DNF", 1, 97.0, compound="SOFT"),
            lap("FST", 2, 90.0), lap("SLW", 2, 91.0),
            lap("DNF", 2, 92.0, compound="INTERMEDIATE")]
    bronze = write_bronze(tmp_path / "bronze", make_pace_results(), pd.DataFrame(rows))
    db_path = warehouse.build(SESSION, bronze, tmp_path / "t.duckdb")
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        state = connection.execute(
            "SELECT lap_number, cars_on_track, cars_on_wet_tyres, is_wet_track "
            "FROM gold_track_state ORDER BY lap_number"
        ).fetchall()
    finally:
        connection.close()

    assert state == [(1, 3, 2, True), (2, 3, 1, False)]


def test_a_short_stint_reports_no_pace(pace):
    row = pace.execute(
        "SELECT representative_laps, median_pace_seconds, lap_time_trend_s_per_lap "
        "FROM gold_stint_pace WHERE driver_code = 'DNF'"
    ).fetchone()

    assert row[0] == 2
    assert row[1] is None, "a slope fitted to two laps is noise with a decimal point"
    assert row[2] is None


def test_the_trend_is_measured_not_assumed(pace):
    trend = pace.execute(
        "SELECT lap_time_trend_s_per_lap FROM gold_stint_pace "
        "WHERE driver_code = 'FST'"
    ).fetchone()[0]

    assert trend == pytest.approx(0.1, abs=0.01), "0.1s added per lap of tyre age"


def test_teammates_of_similar_race_length_are_comparable(pace):
    row = pace.execute(
        "SELECT delta_seconds, is_comparable FROM gold_teammate_delta "
        "WHERE driver_code = 'FST'"
    ).fetchone()

    assert row[0] == pytest.approx(-1.0), "a second a lap quicker than the teammate"
    assert row[1] is True


def test_a_driver_without_a_teammate_is_absent(pace):
    rows = pace.execute(
        "SELECT count(*) FROM gold_teammate_delta WHERE driver_code = 'DNF'"
    ).fetchone()[0]

    assert rows == 0, "no teammate means no comparison, not a null one"
