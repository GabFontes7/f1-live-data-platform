"""Export a committed test fixture from a bronze session.

The acceptance test compares this pipeline against the FIA's published
classification. That test is worth little if it only runs on one laptop, so a
trimmed copy of one real session is committed and CI runs against it.

Trimmed means narrower, not shorter. Every lap stays, because the
fastest-lap rule is only meaningful against the full set of laps; what goes
is the columns nothing downstream reads -- headshot URLs, team colours,
sector speeds. That keeps the fixture small without making it a toy.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger("f1platform.fixtures")

DEFAULT_BRONZE_DIR = Path("data/bronze")
DEFAULT_FIXTURE_DIR = Path("tests/fixtures")

# The columns silver reads, and nothing else. Keep in step with warehouse.py:
# the acceptance test builds the warehouse from this fixture, so a column
# dropped here that the warehouse still needs fails the build immediately.
KEEP_COLUMNS = {
    "results": [
        "DriverNumber",
        "Abbreviation",
        "FullName",
        "TeamName",
        "Position",
        "ClassifiedPosition",
        "GridPosition",
        "Status",
        "Points",
        "Laps",
        "Time",
    ],
    "laps": [
        "Driver",
        "LapNumber",
        "LapTime",
        "Stint",
        "Compound",
        "TyreLife",
        "FreshTyre",
        "TrackStatus",
        "Deleted",
        "DeletedReason",
        "IsAccurate",
        "PitInTime",
        "PitOutTime",
    ],
}


def trim(frame: pd.DataFrame, keep: list[str], table: str) -> pd.DataFrame:
    """Keep the listed columns, failing loudly if the source lost one."""
    missing = [column for column in keep if column not in frame.columns]
    if missing:
        raise KeyError(
            f"{table} is missing {missing}. The source schema changed, so the "
            "fixture would be built against columns that no longer exist."
        )
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

    total_bytes = 0
    for table, keep in KEEP_COLUMNS.items():
        source_path = source / f"{table}.parquet"
        if not source_path.exists():
            raise FileNotFoundError(f"{source_path} is missing")

        frame = pd.read_parquet(source_path)
        trimmed = trim(frame, keep, table)
        target_path = target / f"{table}.parquet"
        trimmed.to_parquet(target_path, index=False, compression="zstd")

        size = target_path.stat().st_size
        total_bytes += size
        LOGGER.info(
            "%s: %d rows, %d of %d columns kept, %.1f KB",
            table,
            len(trimmed),
            len(keep),
            len(frame.columns),
            size / 1024,
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
    except (FileNotFoundError, KeyError) as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
