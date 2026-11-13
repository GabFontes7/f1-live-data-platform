"""Export a committed test fixture from a bronze session.

The acceptance test compares this pipeline against the FIA's published
classification. That test is worth little if it only runs on one laptop, so a
copy of one real session is committed and CI runs against it.

The fixture carries **every column of every table**, deliberately. An earlier
version kept only what the warehouse read, which made the fixture a mirror of
today's model rather than a record of the source: any analysis that needed a
column the model had not reached yet was blocked until someone with a
Formula 1 account re-exported it. The whole session is under 100 KB. Dropping
columns bought nothing and cost the ability to explore.

The one exclusion is driver headshot URLs: long, useless to analysis, and
links to photographs this project has no business redistributing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger("f1platform.fixtures")

DEFAULT_BRONZE_DIR = Path("data/bronze")
DEFAULT_FIXTURE_DIR = Path("tests/fixtures")

# Every table bronze lands. Missing ones are reported, not fatal: a practice
# session has no race control messages worth speaking of.
TABLES = ("results", "laps", "weather_data", "race_control_messages")

# Columns kept out regardless of table.
DROP_COLUMNS = frozenset({"HeadshotUrl"})

# Columns the warehouse reads today. Not a filter -- a checklist, so a source
# that stops providing one of them is caught here instead of in a silently
# empty gold table weeks later.
REQUIRED_COLUMNS = {
    "results": (
        "DriverNumber", "Abbreviation", "FullName", "TeamName", "Position",
        "ClassifiedPosition", "GridPosition", "Status", "Points", "Laps", "Time",
    ),
    "laps": (
        "Driver", "LapNumber", "LapTime", "Stint", "Compound", "TyreLife",
        "FreshTyre", "TrackStatus", "Deleted", "DeletedReason", "IsAccurate",
        "PitInTime", "PitOutTime",
    ),
}


class MissingColumnsError(KeyError):
    """Raised when the source no longer provides a column the warehouse reads."""


def check_required(frame: pd.DataFrame, table: str) -> None:
    missing = [
        column
        for column in REQUIRED_COLUMNS.get(table, ())
        if column not in frame.columns
    ]
    if missing:
        raise MissingColumnsError(
            f"{table} no longer provides {missing}, which the warehouse reads. "
            "The source schema changed; fix the model before exporting."
        )


def prepare(frame: pd.DataFrame, table: str) -> pd.DataFrame:
    """Drop only what is deliberately excluded, keeping everything else."""
    check_required(frame, table)
    keep = [column for column in frame.columns if column not in DROP_COLUMNS]
    return frame[keep].copy()


def export(
    session_slug: str,
    bronze_dir: Path = DEFAULT_BRONZE_DIR,
    fixture_dir: Path = DEFAULT_FIXTURE_DIR,
) -> Path:
    source = bronze_dir / session_slug
    if not source.is_dir():
        raise FileNotFoundError(
            f"{source} does not exist -- run f1-ingest for {session_slug} first"
        )

    target = fixture_dir / session_slug
    target.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {"slug": session_slug, "tables": {}}
    total_bytes = 0

    for table in TABLES:
        source_path = source / f"{table}.parquet"
        if not source_path.exists():
            LOGGER.warning("%s is not in this session, skipping", table)
            continue

        frame = pd.read_parquet(source_path)
        prepared = prepare(frame, table)
        target_path = target / f"{table}.parquet"
        prepared.to_parquet(target_path, index=False, compression="zstd")

        size = target_path.stat().st_size
        total_bytes += size
        dropped = len(frame.columns) - len(prepared.columns)
        manifest["tables"][table] = {
            "rows": len(prepared),
            "columns": list(prepared.columns),
        }
        LOGGER.info(
            "%-22s %6d rows  %2d columns%s  %6.1f KB",
            table,
            len(prepared),
            len(prepared.columns),
            f" ({dropped} dropped)" if dropped else "",
            size / 1024,
        )

    if not manifest["tables"]:
        raise FileNotFoundError(f"{source} held none of {TABLES}")

    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    LOGGER.info("fixture total: %.1f KB at %s", total_bytes / 1024, target)
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-fixture",
        description="Export a committed test fixture from a bronze session.",
    )
    parser.add_argument("--session", required=True, help="bronze slug")
    parser.add_argument("--bronze-dir", type=Path, default=DEFAULT_BRONZE_DIR)
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        export(args.session, args.bronze_dir, args.fixture_dir)
    except (FileNotFoundError, MissingColumnsError) as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
