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
    "TeamColor"                            AS team_colour,
    "TeamId"                               AS team_id,
    "DriverId"                             AS driver_id,
    "BroadcastName"                        AS broadcast_name,
    "CountryCode"                          AS country_code,
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
    CAST("DriverNumber" AS VARCHAR) AS driver_number,
    "Team"                          AS team_name,
    CAST("LapNumber" AS INTEGER)    AS lap_number,
    CAST("LapTime" AS DOUBLE)       AS lap_time_seconds,
    CAST("Time" AS DOUBLE)          AS session_time_seconds,
    CAST("LapStartTime" AS DOUBLE)  AS lap_start_seconds,
    CAST("Position" AS INTEGER)     AS position,
    CAST("Stint" AS INTEGER)        AS stint,
    "Compound"                      AS compound,
    CAST("TyreLife" AS DOUBLE)      AS tyre_life,
    "FreshTyre"                     AS fresh_tyre,
    CAST("Sector1Time" AS DOUBLE)   AS sector1_seconds,
    CAST("Sector2Time" AS DOUBLE)   AS sector2_seconds,
    CAST("Sector3Time" AS DOUBLE)   AS sector3_seconds,
    CAST("SpeedI1" AS DOUBLE)       AS speed_i1_kph,
    CAST("SpeedI2" AS DOUBLE)       AS speed_i2_kph,
    CAST("SpeedFL" AS DOUBLE)       AS speed_finish_kph,
    CAST("SpeedST" AS DOUBLE)       AS speed_trap_kph,
    "IsPersonalBest"                AS is_personal_best,
    "TrackStatus"                   AS track_status,
    "Deleted"                       AS is_deleted,
    "DeletedReason"                 AS deleted_reason,
    "IsAccurate"                    AS is_accurate,
    "FastF1Generated"               AS is_reconstructed,
    "PitInTime" IS NOT NULL         AS is_in_lap,
    "PitOutTime" IS NOT NULL        AS is_out_lap,
    CAST("PitInTime" AS DOUBLE)     AS pit_in_seconds,
    CAST("PitOutTime" AS DOUBLE)    AS pit_out_seconds
FROM read_parquet(?)
"""

# A track-temperature sensor that reports exactly 0.0 C during a session is
# not reporting a temperature, at any circuit: asphalt under running cars
# does not sit at a round zero for one minute between two readings in the
# twenties. First seen in the 2026 Sao Paulo race: one sample of 160 reads 0.0,
# and the readings taken while it was raining swing by 15 C between
# consecutive minutes. Zero becomes NULL; the swings are left alone and
# flagged, because they are noisy measurements rather than absent ones.
SILVER_WEATHER = """
CREATE OR REPLACE TABLE silver_weather AS
SELECT
    ? AS session_slug,
    CAST("Time" AS DOUBLE)                      AS session_time_seconds,
    CAST("AirTemp" AS DOUBLE)                   AS air_temp_c,
    CASE WHEN "TrackTemp" > 0
         THEN CAST("TrackTemp" AS DOUBLE) END   AS track_temp_c,
    CAST("TrackTemp" AS DOUBLE) = 0             AS track_temp_sensor_failed,
    CAST("Humidity" AS DOUBLE)                  AS humidity_pct,
    CAST("Pressure" AS DOUBLE)                  AS pressure_mbar,
    "Rainfall"                                  AS is_raining,
    CAST("WindSpeed" AS DOUBLE)                 AS wind_speed_ms,
    CAST("WindDirection" AS INTEGER)            AS wind_direction_deg
FROM read_parquet(?)
"""

# Race control carries two different things under one feed: flags, which are
# track state, and "Other", which is the stewards talking. Penalties served
# during the race appear here; penalties applied after the chequered flag do
# not, because they live in the FIA's documents instead.
SILVER_RACE_CONTROL = """
CREATE OR REPLACE TABLE silver_race_control AS
SELECT
    ? AS session_slug,
    CAST("Lap" AS INTEGER)              AS lap_number,
    CAST("Time" AS TIMESTAMP)           AS message_time,
    "Category"                          AS category,
    "Message"                           AS message,
    "Flag"                              AS flag,
    "Scope"                             AS scope,
    CAST("Sector" AS INTEGER)           AS sector,
    CAST("RacingNumber" AS VARCHAR)     AS driver_number,
    regexp_matches("Message", 'TIME PENALTY')      AS mentions_penalty,
    regexp_matches("Message", 'INVESTIGAT')        AS mentions_investigation,
    regexp_matches("Message", 'DELETED')           AS mentions_deleted_lap
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

# The track is called wet on a lap when at least this share of the cars on
# it are running a wet-weather compound.
WET_TRACK_SHARE = 0.5

# TrackStatus is a concatenation of every code seen during the lap: 1 clear,
# 2 yellow, 4 safety car, 5 red flag, 6 virtual safety car, 7 VSC ending. A
# lap is neutralised if ANY car saw the code, because the neutralisation is
# a property of the track rather than of a car.
GOLD_TRACK_STATE = f"""
CREATE OR REPLACE TABLE gold_track_state AS
WITH per_lap AS (
    SELECT
        session_slug,
        lap_number,
        count(*)                                       AS cars_on_track,
        count(compound)                                AS cars_with_a_compound,
        sum((compound IN {WET_COMPOUNDS})::INT)        AS cars_on_wet_tyres,
        bool_or(contains(track_status, '5'))           AS had_red_flag,
        bool_or(contains(track_status, '4'))           AS had_safety_car,
        bool_or(contains(track_status, '6')
                OR contains(track_status, '7'))        AS had_virtual_safety_car,
        bool_or(contains(track_status, '2'))           AS had_yellow
    FROM silver_laps
    GROUP BY session_slug, lap_number
)
SELECT
    * EXCLUDE (cars_with_a_compound),
    CASE WHEN cars_with_a_compound > 0
         THEN cars_on_wet_tyres::DOUBLE / cars_with_a_compound END
                                                       AS wet_tyre_share,
    coalesce(cars_on_wet_tyres::DOUBLE
             / nullif(cars_with_a_compound, 0) >= {WET_TRACK_SHARE}, FALSE)
                                                       AS is_wet_track,
    CASE WHEN had_red_flag             THEN 'red'
         WHEN had_safety_car           THEN 'safety_car'
         WHEN had_virtual_safety_car   THEN 'vsc'
         WHEN had_yellow               THEN 'yellow'
         ELSE 'green' END                              AS neutralisation
FROM per_lap
ORDER BY lap_number
"""
"""Why the track's state is read from the tyres, not from the weather feed.

The weather feed carries a `Rainfall` flag, sampled about once a minute at
one point of the circuit. It answers "is water falling on the sensor right
now", which is a different question from "is the track wet". In the 2026
São Paulo race it was true in 7 of 160 samples, while every car ran
intermediates for the first 14 laps and the last 9.

Twenty teams choosing a tyre is a better instrument: nobody stays on
intermediates on a dry track or on slicks on a wet one for long. So a lap
counts as wet when at least half the cars on it are on a wet-weather
compound, and the share itself is kept, because the laps where it moves
from 1 to 0 are the crossover -- the most interesting laps of the race.
"""

# One row per pair of cars that swapped order between two consecutive laps.
# Deliberately NOT called overtakes: see docs/adr/0006.
GOLD_POSITION_CHANGES = """
CREATE OR REPLACE TABLE gold_position_changes AS
WITH ranked AS (
    SELECT
        session_slug, driver_code, lap_number, position, is_in_lap, is_out_lap,
        lag(position)   OVER (PARTITION BY driver_code ORDER BY lap_number)
            AS previous_position,
        lag(lap_number) OVER (PARTITION BY driver_code ORDER BY lap_number)
            AS previous_lap
    FROM silver_laps
    WHERE position IS NOT NULL
)
SELECT
    a.session_slug,
    a.lap_number,
    a.driver_code                              AS passer,
    b.driver_code                              AS passed,
    b.previous_position::INT                   AS position_taken,
    a.is_in_lap OR a.is_out_lap
        OR b.is_in_lap OR b.is_out_lap         AS pit_lap_involved,
    NOT (a.is_in_lap OR a.is_out_lap
         OR b.is_in_lap OR b.is_out_lap)       AS is_on_track
FROM ranked AS a
JOIN ranked AS b
  ON a.lap_number = b.lap_number
 AND a.driver_code <> b.driver_code
WHERE a.previous_lap = a.lap_number - 1
  AND b.previous_lap = b.lap_number - 1
  AND a.previous_position > b.previous_position
  AND a.position < b.position
ORDER BY a.lap_number, a.position
"""

# 2026 cars carry an overtake aid the race director can switch off, and the
# switch arrives as a race control message. Which laps had it is a property
# of the race, so it is carried forward lap by lap rather than left as two
# messages buried in a list of two hundred.
GOLD_OVERTAKE_AID = """
CREATE OR REPLACE TABLE gold_overtake_aid AS
WITH flips AS (
    SELECT
        lap_number,
        upper(message) LIKE '%OVERTAKE ENABLED%' AS aid_enabled
    FROM silver_race_control
    WHERE upper(message) LIKE '%OVERTAKE ENABLED%'
       OR upper(message) LIKE '%OVERTAKE DISABLED%'
),
laps AS (SELECT DISTINCT session_slug, lap_number FROM silver_laps)
SELECT laps.session_slug, laps.lap_number, flips.aid_enabled
FROM laps ASOF LEFT JOIN flips ON laps.lap_number >= flips.lap_number
ORDER BY laps.lap_number
"""

# What actually happened in this session, in one row. The report reads it to
# decide which sections have anything to say: a card about rain belongs on a
# page about a wet race and nowhere else.
GOLD_SESSION_PROFILE = """
CREATE OR REPLACE TABLE gold_session_profile AS
WITH leaders AS (
    SELECT lap_number, any_value(driver_code) AS leader
    FROM silver_laps WHERE position = 1
    GROUP BY lap_number
),
changes AS (
    SELECT count(*) AS lead_changes FROM (
        SELECT leader, lag(leader) OVER (ORDER BY lap_number) AS previous
        FROM leaders
    ) WHERE previous IS NOT NULL AND leader IS DISTINCT FROM previous
),
laps AS (
    SELECT
        any_value(session_slug)                   AS session_slug,
        max(lap_number)                           AS laps_run,
        count(DISTINCT driver_code)               AS cars_with_laps,
        sum(is_in_lap::INT)                       AS pit_stops,
        sum(is_deleted::INT)                      AS deleted_laps,
        count(DISTINCT compound)                  AS compounds_used
    FROM silver_laps
),
swaps AS (
    SELECT
        sum(is_on_track::INT)         AS on_track_position_changes,
        count(*)                      AS position_changes_including_pit_cycles
    FROM gold_position_changes
),
state AS (
    SELECT
        sum(is_wet_track::INT)                            AS wet_laps,
        sum((neutralisation = 'safety_car')::INT)         AS safety_car_laps,
        sum((neutralisation = 'vsc')::INT)                AS vsc_laps,
        sum((neutralisation = 'red')::INT)                AS red_flag_laps,
        sum((neutralisation = 'yellow')::INT)             AS yellow_laps,
        count(*)                                          AS counted_laps
    FROM gold_track_state
),
result AS (
    SELECT
        max(laps_completed)                       AS race_laps,
        count(*)                                  AS entries,
        sum(is_classified::INT)                   AS classified,
        max(positions_gained)                     AS biggest_gain,
        arg_max(driver_code, positions_gained)    AS biggest_gainer,
        arg_min(driver_code, finishing_order)     AS winner
    FROM gold_race_classification
),
margin AS (
    SELECT gap_to_winner_seconds AS winning_margin_seconds, laps_down AS runner_up_laps_down
    FROM gold_race_classification WHERE finishing_order = 2
)
SELECT laps.*, result.*, state.*, swaps.*, changes.lead_changes,
       margin.winning_margin_seconds, margin.runner_up_laps_down
FROM laps, result, state, changes, swaps
LEFT JOIN margin ON TRUE
"""

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

    # Weather and race control are not guaranteed: a session can lack either.
    optional = {
        "silver_weather": (SILVER_WEATHER, session_dir / "weather_data.parquet"),
        "silver_race_control": (
            SILVER_RACE_CONTROL,
            session_dir / "race_control_messages.parquet",
        ),
    }

    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(db_path))
    try:
        connection.execute(SILVER_RESULTS, [session_slug, str(results_parquet)])
        connection.execute(SILVER_LAPS, [session_slug, str(laps_parquet)])

        loaded_optional = []
        for name, (sql, path) in optional.items():
            if path.exists():
                connection.execute(sql, [session_slug, str(path)])
                loaded_optional.append(name)
            else:
                LOGGER.warning("%s is absent, skipping %s", path.name, name)
        connection.execute(GOLD_CLASSIFICATION)
        connection.execute(GOLD_FASTEST_LAP)
        connection.execute(GOLD_REPRESENTATIVE_LAPS)
        connection.execute(GOLD_STINT_PACE)
        connection.execute(GOLD_TRACK_STATE)
        connection.execute(GOLD_TEAMMATE_DELTA)
        connection.execute(GOLD_POSITION_CHANGES)
        if "silver_race_control" in loaded_optional:
            connection.execute(GOLD_OVERTAKE_AID)
            loaded_optional.append("gold_overtake_aid")
        connection.execute(GOLD_SESSION_PROFILE)

        for table in (
            "silver_results",
            "silver_laps",
            "gold_race_classification",
            "gold_fastest_lap",
            "gold_representative_laps",
            "gold_stint_pace",
            "gold_track_state",
            "gold_session_profile",
            "gold_position_changes",
            "gold_teammate_delta",
            *loaded_optional,
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
