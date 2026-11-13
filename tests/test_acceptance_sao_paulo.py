"""Acceptance test: rebuild the official São Paulo classification from raw data.

This is the test the whole vertical slice exists to pass. The expected values
come from the FIA's final classification for the 2026 São Paulo Grand Prix,
not from this pipeline -- so the pipeline either reproduces an outside,
published truth or it is wrong.

One assertion exercises four business rules at once: finishing order,
time penalties applied after the flag, the 90%-distance classification rule,
and the fastest-lap award going only to a classified driver on a valid lap.

It runs twice where possible: against the committed fixture, so CI proves it
too, and against a freshly ingested bronze session when one is on disk. The
same assertions over two copies of the data also catch a fixture that has
drifted from what the source now returns.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from f1platform import warehouse

SESSION = "2026-sao-paulo-grand-prix-r"
BRONZE_DIR = Path("data/bronze")
FIXTURE_DIR = Path("tests/fixtures")

# The fixture is committed, so CI runs the first of these. The second runs
# only on a machine that has ingested the session itself.
SOURCES = [("fixture", FIXTURE_DIR), ("bronze", BRONZE_DIR)]

EXPECTED_PODIUM = ["NOR", "VER", "HAD"]
EXPECTED_POSITIONS = {"ANT": 5, "LEC": 8, "BEA": 17}
EXPECTED_UNCLASSIFIED = {"STR": "R", "BOT": "W"}
EXPECTED_FASTEST_LAP = ("ANT", 72.689)
EXPECTED_WINNER_LAPS = 71
EXPECTED_WINNER_POINTS = 25.0


@pytest.fixture(scope="module", params=SOURCES, ids=lambda source: source[0])
def warehouse_db(request, tmp_path_factory):
    name, directory = request.param
    if not (directory / SESSION / "results.parquet").exists():
        pytest.skip(
            f"no {name} copy of {SESSION}. For bronze, run f1-ingest; for the "
            "fixture, run f1-fixture and commit the result"
        )
    db_path = tmp_path_factory.mktemp(f"warehouse-{name}") / "acceptance.duckdb"
    warehouse.build(SESSION, directory, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    yield connection
    connection.close()


def test_the_podium_matches_the_official_result(warehouse_db):
    rows = warehouse_db.execute(
        """
        SELECT driver_code
        FROM gold_race_classification
        WHERE finishing_position <= 3
        ORDER BY finishing_position
        """
    ).fetchall()

    assert [row[0] for row in rows] == EXPECTED_PODIUM


def test_positions_after_post_race_penalties(warehouse_db):
    """Antonelli, Leclerc and Bearman each took a 5s penalty after the flag."""
    for code, position in EXPECTED_POSITIONS.items():
        actual = warehouse_db.execute(
            "SELECT finishing_position FROM gold_race_classification "
            "WHERE driver_code = ?",
            [code],
        ).fetchone()
        assert actual is not None, f"{code} is missing from the classification"
        assert actual[0] == position, f"{code} should be P{position}, got P{actual[0]}"


def test_unclassified_drivers_are_marked_not_ranked(warehouse_db):
    """The 90%-distance rule: a driver can finish last and still not be classified."""
    for code, expected_code in EXPECTED_UNCLASSIFIED.items():
        row = warehouse_db.execute(
            """
            SELECT classified_position, is_classified, finishing_position
            FROM gold_race_classification
            WHERE driver_code = ?
            """,
            [code],
        ).fetchone()
        assert row is not None, f"{code} is missing"
        classified_position, is_classified, finishing_position = row
        assert classified_position == expected_code
        assert is_classified is False, f"{code} must not count as classified"
        assert finishing_position is None, f"{code} must not hold a ranked position"


def test_every_other_driver_is_classified(warehouse_db):
    unclassified = warehouse_db.execute(
        "SELECT driver_code FROM gold_race_classification WHERE NOT is_classified"
    ).fetchall()

    assert sorted(row[0] for row in unclassified) == sorted(EXPECTED_UNCLASSIFIED)


def test_the_fastest_lap_is_the_official_one(warehouse_db):
    row = warehouse_db.execute(
        "SELECT driver_code, lap_time_seconds FROM gold_fastest_lap"
    ).fetchone()
    assert row is not None, "no fastest lap was computed"

    code, seconds = row
    expected_code, expected_seconds = EXPECTED_FASTEST_LAP

    if (code, round(seconds, 3)) != (expected_code, expected_seconds):
        top = warehouse_db.execute(
            """
            SELECT driver_code, lap_number, lap_time_seconds, is_deleted
            FROM silver_laps
            WHERE lap_time_seconds IS NOT NULL
            ORDER BY lap_time_seconds
            LIMIT 5
            """
        ).fetchall()
        pytest.fail(
            f"expected {expected_code} at {expected_seconds}s, got {code} at "
            f"{seconds}s. Five quickest laps in the data: {top}"
        )


def test_the_winner_completed_the_full_distance(warehouse_db):
    laps, points, duration = warehouse_db.execute(
        """
        SELECT laps_completed, points, race_duration_seconds
        FROM gold_race_classification
        WHERE finishing_position = 1
        """
    ).fetchone()

    assert laps == EXPECTED_WINNER_LAPS
    assert points == EXPECTED_WINNER_POINTS
    assert duration is not None, "the winner's row must carry the race duration"


def test_only_the_winner_carries_a_race_duration(warehouse_db):
    """`Time` means the race duration on one row and a gap on the others."""
    count = warehouse_db.execute(
        "SELECT count(*) FROM gold_race_classification "
        "WHERE race_duration_seconds IS NOT NULL"
    ).fetchone()[0]

    assert count == 1


def test_a_lapped_driver_has_no_gap_to_the_winner(warehouse_db):
    """Hamilton finished a lap down; his `Time` is not a gap to the winner."""
    gap = warehouse_db.execute(
        "SELECT gap_to_winner_seconds FROM gold_race_classification "
        "WHERE driver_code = 'HAM'"
    ).fetchone()[0]

    assert gap is None, "a lapped driver's Time must not be read as a gap"
