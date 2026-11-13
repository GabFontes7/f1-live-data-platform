"""Build the DuckDB warehouse from the bronze Parquet layer.

Silver types and renames what bronze landed. Gold applies the sport's rules,
and those rules are the whole point of this module -- a table that looks
right and quietly breaks one of them is worse than no table.

Three rules are enforced here, each visible in the source data:

* **Classification is not position.** ``ClassifiedPosition`` is text: "1"
  through "20", then "R" for retired and "W" for withdrawn. ``Position`` is
  a number for everyone, so ordering by it alone silently promotes drivers
  the FIA did not classify.
* **A deleted lap is not a lap.** Laps carry ``Deleted`` and
  ``DeletedReason``; a deleted lap stays in the data and must not win a
  fastest-lap award.
* **``Time`` means different things per row.** For the winner it is the race
  duration; for a finisher it is the gap to the winner; for a lapped driver
  it is neither. Gold stores the winner's duration once and leaves the rest
  as the gap it is, rather than pretending one column means one thing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import duckdb

LOGGER = logging.getLogger("f1platform.warehouse")

DEFAULT_BRONZE_DIR = Path("data/bronze")
DEFAULT_DB_PATH = Path("data/warehouse.duckdb")

# ClassifiedPosition values that are not a finishing position.
# R retired, W withdrawn, D disqualified, E excluded, F failed to qualify,
# N not classified.
NON_CLASSIFIED_CODES = ("R", "W", "D", "E", "F", "N")


def _codes_sql() -> str:
    return ", ".join(f"'{code}'" for code in NON_CLASSIFIED_CODES)


SILVER_RESULTS = f"""
CREATE OR REPLACE TABLE silver_results AS
SELECT
    ? AS session_slug,
    CAST("DriverNumber" AS VARCHAR)        AS driver_number,
    "Abbreviation"                         AS driver_code,
    "FullName"                             AS driver_name,
    "TeamName"                             AS team_name,
    CAST("Position" AS INTEGER)            AS finishing_order,
    "ClassifiedPosition"                   AS classified_position,
    CAST("GridPosition" AS INTEGER)        AS grid_position,
    "Status"                               AS status,
    CAST("Points" AS DOUBLE)               AS points,
    CAST("Laps" AS INTEGER)                AS laps_completed,
    CAST("Time" AS DOUBLE)                 AS time_seconds,
    "ClassifiedPosition" NOT IN ({_codes_sql()}) AS is_classified
FROM read_parquet(?)
"""

SILVER_LAPS = """
CREATE OR REPLACE TABLE silver_laps AS
SELECT
    ? AS session_slug,
    "Driver"                        AS driver_code,
    CAST("LapNumber" AS INTEGER)    AS lap_number,
    CAST("LapTime" AS DOUBLE)       AS lap_time_seconds,
    CAST("Stint" AS INTEGER)        AS stint,
    "Compound"                      AS compound,
    CAST("TyreLife" AS DOUBLE)      AS tyre_life,
    "FreshTyre"                     AS fresh_tyre,
    "TrackStatus"                   AS track_status,
    "Deleted"                       AS is_deleted,
    "DeletedReason"                 AS deleted_reason,
    "IsAccurate"                    AS is_accurate,
    "PitInTime" IS NOT NULL         AS is_in_lap,
    "PitOutTime" IS NOT NULL        AS is_out_lap
FROM read_parquet(?)
"""

GOLD_CLASSIFICATION = """
CREATE OR REPLACE TABLE gold_race_classification AS
SELECT
    session_slug,
    driver_code,
    driver_name,
    team_name,
    classified_position,
    is_classified,
    -- The order the timing system listed everyone in, classified or not.
    -- Kept so the table can be sorted without relying on physical row order.
    finishing_order,
    -- Only a classified driver has a finishing position worth ranking.
    CASE WHEN is_classified THEN finishing_order END AS finishing_position,
    grid_position,
    CASE WHEN is_classified THEN grid_position - finishing_order END AS positions_gained,
    status,
    points,
    laps_completed,
    -- The winner's `Time` is the race duration; everyone else's is a gap.
    CASE WHEN finishing_order = 1 THEN time_seconds END AS race_duration_seconds,
    CASE WHEN finishing_order > 1 AND status = 'Finished' THEN time_seconds END
        AS gap_to_winner_seconds
FROM silver_results
ORDER BY finishing_order
"""

GOLD_FASTEST_LAP = """
CREATE OR REPLACE TABLE gold_fastest_lap AS
SELECT
    l.session_slug,
    l.driver_code,
    l.lap_number,
    l.lap_time_seconds
FROM silver_laps AS l
JOIN silver_results AS r
  ON r.driver_code = l.driver_code
 AND r.session_slug = l.session_slug
WHERE NOT l.is_deleted          -- a deleted lap cannot take the award
  AND r.is_classified           -- nor can an unclassified driver
  AND l.lap_time_seconds IS NOT NULL
ORDER BY l.lap_time_seconds
LIMIT 1
"""


def build(
    session_slug: str,
    bronze_dir: Path = DEFAULT_BRONZE_DIR,
    db_path: Path = DEFAULT_DB_PATH,
) -> Path:
    """Build silver and gold tables for one session into a DuckDB file."""
    session_dir = bronze_dir / session_slug
    results_parquet = session_dir / "results.parquet"
    laps_parquet = session_dir / "laps.parquet"

    for required in (results_parquet, laps_parquet):
        if not required.exists():
            raise FileNotFoundError(
                f"{required} is missing -- run f1-ingest for {session_slug} first"
            )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(db_path))
    try:
        connection.execute(SILVER_RESULTS, [session_slug, str(results_parquet)])
        connection.execute(SILVER_LAPS, [session_slug, str(laps_parquet)])
        connection.execute(GOLD_CLASSIFICATION)
        connection.execute(GOLD_FASTEST_LAP)

        for table in (
            "silver_results",
            "silver_laps",
            "gold_race_classification",
            "gold_fastest_lap",
        ):
            rows = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            LOGGER.info("%s: %s rows", table, f"{rows:,}")
    finally:
        connection.close()

    return db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-build",
        description="Build the DuckDB warehouse from a bronze session.",
    )
    parser.add_argument("--session", required=True, help="bronze slug")
    parser.add_argument("--bronze-dir", type=Path, default=DEFAULT_BRONZE_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        db_path = build(args.session, args.bronze_dir, args.db)
    except FileNotFoundError as error:
        LOGGER.error("%s", error)
        return 1

    LOGGER.info("warehouse written to %s", db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
