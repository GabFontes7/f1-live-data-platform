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
    -- How many whole laps behind the winner. Only meaningful for a
    -- classified driver: a retirement is not "41 laps down", it is out.
    CASE WHEN is_classified
         THEN max(laps_completed) OVER () - laps_completed END AS laps_down,
    -- The winner's `Time` is the race duration.
    CASE WHEN finishing_order = 1 THEN time_seconds END AS race_duration_seconds,
    -- For everyone else `Time` is the time behind the leader with whole laps
    -- taken out, which is why it restarts low at the top of each lap group:
    -- in Sao Paulo, Bearman a lap down reads 91.7s and Perez two laps down
    -- reads 27.1s. Kept under a name that says what it is.
    CASE WHEN finishing_order > 1 THEN time_seconds END AS time_behind_seconds,
    -- A true gap to the winner exists only on the lead lap.
    CASE WHEN finishing_order > 1
          AND max(laps_completed) OVER () - laps_completed = 0
         THEN time_seconds END AS gap_to_winner_seconds
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


# A lap is usable for measuring pace only if nothing external slowed it.
# TrackStatus is the concatenation of every status seen during the lap, so
# '1' means the lap was green from start to finish while '12' means a yellow
# appeared partway. FastF1's own accuracy check accepts yellow and calls
# itself "slightly paranoid"; pace measurement is stricter, because a yellow
# costs time in exactly the sector it covers. See docs/adr/0003.
GOLD_REPRESENTATIVE_LAPS = """
CREATE OR REPLACE TABLE gold_representative_laps AS
SELECT
    l.*,
    l.track_status = '1'                     AS is_green,
    l.track_status = '1'
        AND NOT l.is_deleted
        AND NOT l.is_in_lap
        AND NOT l.is_out_lap
        AND l.lap_time_seconds IS NOT NULL   AS is_representative
FROM silver_laps AS l
"""

# A slope fitted to three laps is noise with a decimal point. Stints below
# this many representative laps report their lap count and nothing else.
MIN_LAPS_FOR_PACE = 4

# Compounds run when the track is wet. Their stints are dominated by the
# track drying, not by the tyre wearing out.
WET_COMPOUNDS = ("INTERMEDIATE", "WET")

# Two medians are comparable only when both drivers ran a similar share of
# the race. Below this ratio of representative laps, the delta is measuring
# two different races rather than two drivers.
MIN_LAP_OVERLAP = 0.7

GOLD_STINT_PACE = f"""
CREATE OR REPLACE TABLE gold_stint_pace AS
WITH usable AS (
    SELECT * FROM gold_representative_laps WHERE is_representative
)
SELECT
    l.session_slug,
    l.driver_code,
    l.stint,
    any_value(l.compound)                        AS compound,
    count(*)                                     AS representative_laps,
    min(l.tyre_life)                             AS tyre_life_at_start,
    CASE WHEN count(*) >= {MIN_LAPS_FOR_PACE}
         THEN median(l.lap_time_seconds) END     AS median_pace_seconds,
    CASE WHEN count(*) >= {MIN_LAPS_FOR_PACE}
         THEN stddev_samp(l.lap_time_seconds) END AS consistency_seconds,
    -- Seconds per extra lap of tyre age. Deliberately NOT called
    -- degradation: it is the sum of at least three effects pulling in
    -- opposite directions, and this pipeline measures only their total.
    CASE WHEN count(*) >= {MIN_LAPS_FOR_PACE}
         THEN regr_slope(l.lap_time_seconds, l.tyre_life) END
                                                 AS lap_time_trend_s_per_lap,
    any_value(l.compound) IN {WET_COMPOUNDS}     AS is_wet_compound,
    -- A negative trend means the car got quicker as the tyre aged, so
    -- something other than the tyre dominated the stint.
    CASE WHEN count(*) >= {MIN_LAPS_FOR_PACE}
         THEN regr_slope(l.lap_time_seconds, l.tyre_life) < 0 END
                                                 AS trend_is_negative
FROM usable AS l
GROUP BY l.session_slug, l.driver_code, l.stint
ORDER BY l.driver_code, l.stint
"""
"""Why `lap_time_trend_s_per_lap` is not called degradation.

The slope of lap time against tyre age within a stint is the sum of at
least three effects:

* **tyre wear**, which makes laps slower, the only one the name would imply;
* **fuel burn**, which makes every car quicker by roughly a twentieth of a
  second per lap as the tank empties, and never stops;
* **track evolution**, which on a drying track is larger than both. In the
  2026 São Paulo race, 41 of 79 stints ran intermediates and the winner's
  first two stints both trend negative -- the track was drying faster than
  the tyres were wearing.

Separating them needs a fuel-correction coefficient this pipeline has no
evidence for, so it reports the total it can actually measure and names it
accordingly. `trend_is_negative` marks the stints where the tyre was not
the dominant term.
"""

# The only comparison in Formula 1 that controls for the car. Two drivers
# share a car's strengths and its limitations, so the gap between them is
# the closest thing to a measurement of the driver alone.
GOLD_TEAMMATE_DELTA = f"""
CREATE OR REPLACE TABLE gold_teammate_delta AS
WITH driver_pace AS (
    SELECT
        l.session_slug,
        l.driver_code,
        r.team_name,
        count(*)                      AS representative_laps,
        median(l.lap_time_seconds)    AS median_pace_seconds
    FROM gold_representative_laps AS l
    JOIN silver_results AS r
      ON r.driver_code = l.driver_code AND r.session_slug = l.session_slug
    WHERE l.is_representative
    GROUP BY l.session_slug, l.driver_code, r.team_name
    HAVING count(*) >= {MIN_LAPS_FOR_PACE}
),
paired AS (
    SELECT
        a.session_slug,
        a.driver_code,
        a.team_name,
        a.representative_laps,
        a.median_pace_seconds,
        b.driver_code             AS teammate_code,
        b.median_pace_seconds     AS teammate_pace_seconds,
        b.representative_laps     AS teammate_representative_laps,
        -- How many teammates this driver has, not how many drivers the team
        -- ran: a two-driver team gives each of them exactly one.
        count(*) OVER (PARTITION BY a.session_slug, a.driver_code)
                                  AS teammate_count
    FROM driver_pace AS a
    JOIN driver_pace AS b
      ON b.team_name = a.team_name
     AND b.session_slug = a.session_slug
     AND b.driver_code <> a.driver_code
)
SELECT
    session_slug,
    driver_code,
    team_name,
    teammate_code,
    representative_laps,
    teammate_representative_laps,
    median_pace_seconds,
    teammate_pace_seconds,
    -- Negative means quicker than the teammate.
    median_pace_seconds - teammate_pace_seconds AS delta_seconds,
    -- Two medians are only comparable when both drivers ran a similar share
    -- of the race. In the 2026 Sao Paulo race Stroll retired on lap 30 with
    -- a sick car, and his delta to Alonso reads -10s: an arithmetically
    -- correct number that measures a broken car, not a slower driver.
    least(representative_laps, teammate_representative_laps)::DOUBLE
        / greatest(representative_laps, teammate_representative_laps)
        >= {MIN_LAP_OVERLAP} AS is_comparable
FROM paired
-- A driver with more than one teammate in a session has no single
-- comparison, so the delta would be ambiguous rather than wrong.
WHERE teammate_count = 1
ORDER BY delta_seconds
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
        connection.execute(GOLD_REPRESENTATIVE_LAPS)
        connection.execute(GOLD_STINT_PACE)
        connection.execute(GOLD_TEAMMATE_DELTA)

        for table in (
            "silver_results",
            "silver_laps",
            "gold_race_classification",
            "gold_fastest_lap",
            "gold_representative_laps",
            "gold_stint_pace",
            "gold_teammate_delta",
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
