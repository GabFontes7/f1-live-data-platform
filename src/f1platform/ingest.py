"""Fetch one session from FastF1 and write it to the bronze layer as Parquet.

Bronze is what the source gave us, typed and stored, with no business logic
applied. Everything downstream reads from here, so this module's one job is
to land the data faithfully -- and to refuse to land nothing.

That refusal is the point. FastF1 3.9 signs in to a Formula 1 account even
for historical data, and when the sign-in fails it logs a warning, carries
on, and returns an *empty* session. The load reports success. A pipeline
that trusts it builds an empty warehouse and passes its own tests, which is
worse than crashing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import fastf1
import pandas as pd

LOGGER = logging.getLogger("f1platform.ingest")

DEFAULT_OUTPUT_DIR = Path("data/bronze")
DEFAULT_CACHE_DIR = Path("cache/fastf1")

# Tables pulled from a loaded session, by the attribute that holds them.
BRONZE_TABLES = ("results", "laps", "weather_data", "race_control_messages")

# What a session knows about the event it belongs to. FastF1 hands this over
# with the session itself, so recording it costs no extra request -- and it
# is what lets a page say "round 21" instead of guessing from a file name.
EVENT_FIELDS = (
    "RoundNumber", "EventName", "OfficialEventName", "Country", "Location",
    "EventFormat", "EventDate",
)

# The sessions a weekend offers, and the order they run in.
SESSION_CODES = ("FP1", "FP2", "FP3", "SQ", "S", "Q", "R")

# Ingesting a season takes a while, so the default is the two sessions that
# decide the weekend. --sessions widens it.
DEFAULT_SEASON_SESSIONS = ("Q", "R")


class EmptySessionError(RuntimeError):
    """Raised when a session loads without error but carries no data."""


@dataclass(frozen=True)
class SessionRef:
    """Everything needed to name and fetch one session."""

    year: int
    event: str
    session: str

    @property
    def slug(self) -> str:
        event = self.event.lower().replace(" ", "-")
        return f"{self.year}-{event}-{self.session.lower()}"


def load_session(ref: SessionRef, cache_dir: Path) -> fastf1.core.Session:
    """Load a session from FastF1, refusing to return an empty one.

    An empty result almost always means the F1 account sign-in failed, so the
    error says that rather than leaving the caller to guess.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    # `enable_cache` is deprecated in FastF1 3.9 and removed in a future
    # version; `configure` is keyword-only and takes the same directory.
    fastf1.Cache.configure(cache_dir=str(cache_dir))

    session = fastf1.get_session(ref.year, ref.event, ref.session)
    session.load()

    drivers = len(session.results) if session.results is not None else 0
    if drivers == 0:
        raise EmptySessionError(
            f"{ref.slug} loaded without error but contains no drivers. FastF1 "
            "returns an empty session when the Formula 1 sign-in fails, so "
            "check `f1-record --check-auth` or set F1_USERNAME and F1_PASSWORD. "
            "Refusing to write an empty bronze layer."
        )

    LOGGER.info("loaded %s: %d drivers", ref.slug, drivers)
    return session


def event_metadata(session: fastf1.core.Session) -> dict:
    """The event this session belongs to, as plain JSON-safe values."""
    event = getattr(session, "event", None)
    if event is None:
        return {}
    metadata = {}
    for field in EVENT_FIELDS:
        try:
            value = event[field]
        except (KeyError, IndexError, TypeError):
            continue
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        if isinstance(value, pd.Timestamp):
            value = value.date().isoformat()
        elif hasattr(value, "item"):
            value = value.item()
        metadata[field] = value
    return metadata


def frames_from(session: fastf1.core.Session) -> dict[str, pd.DataFrame]:
    """Collect the bronze tables a session exposes, skipping any it lacks."""
    frames: dict[str, pd.DataFrame] = {}
    for name in BRONZE_TABLES:
        frame = getattr(session, name, None)
        if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
            LOGGER.warning("%s is empty or unavailable, skipping", name)
            continue
        frames[name] = pd.DataFrame(frame)
    return frames


def write_bronze(
    ref: SessionRef,
    frames: dict[str, pd.DataFrame],
    output_dir: Path,
    event: dict | None = None,
) -> Path:
    """Write each table as Parquet under data/bronze/<slug>/ and a manifest."""
    session_dir = output_dir / ref.slug
    session_dir.mkdir(parents=True, exist_ok=True)

    manifest = {"slug": ref.slug, "year": ref.year, "event": ref.event,
                "session": ref.session, "tables": {}}
    if event:
        payload = {"slug": ref.slug, "session_code": ref.session.upper(), **event}
        (session_dir / "event.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        manifest["event_metadata"] = payload
        LOGGER.info(
            "event: round %s, %s",
            event.get("RoundNumber", "?"), event.get("EventName", "?"),
        )

    for name, frame in frames.items():
        path = session_dir / f"{name}.parquet"
        # Durations and timedeltas survive Parquet badly across engines; store
        # them as nanoseconds so silver can rebuild them without guessing.
        prepared = frame.copy()
        for column in prepared.columns:
            if pd.api.types.is_timedelta64_dtype(prepared[column]):
                prepared[column] = prepared[column].dt.total_seconds()
        prepared.to_parquet(path, index=False)
        manifest["tables"][name] = {"rows": len(frame), "columns": list(frame.columns)}
        LOGGER.info("wrote %s (%d rows, %d columns)", path, len(frame), len(frame.columns))

    manifest_path = session_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return session_dir


def ingest(
    ref: SessionRef,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Path:
    session = load_session(ref, cache_dir)
    frames = frames_from(session)
    if not frames:
        raise EmptySessionError(f"{ref.slug} yielded no usable tables")
    return write_bronze(ref, frames, output_dir, event_metadata(session))


def season_refs(
    year: int, sessions: tuple[str, ...], cache_dir: Path
) -> list[SessionRef]:
    """Every session of a season worth ingesting, in calendar order.

    A sprint weekend offers sessions a normal one does not, so the schedule
    is asked which sessions each round actually held rather than assuming.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.configure(cache_dir=str(cache_dir))
    schedule = fastf1.get_event_schedule(year, include_testing=False)
    wanted = [code.upper() for code in sessions]
    refs = []
    for _, event in schedule.iterrows():
        held = {
            str(event.get(f"Session{number}", "")).strip()
            for number in range(1, 6)
        }
        for code in sorted(wanted, key=SESSION_CODES.index):
            if code in SESSION_CODE_NAMES and not (
                SESSION_CODE_NAMES[code] & held
            ):
                continue
            refs.append(
                SessionRef(year=year, event=str(event["EventName"]), session=code)
            )
    return refs


# A session code appears in the schedule under its full name.
SESSION_CODE_NAMES = {
    "FP1": {"Practice 1"},
    "FP2": {"Practice 2"},
    "FP3": {"Practice 3"},
    "SQ": {"Sprint Qualifying", "Sprint Shootout"},
    "S": {"Sprint"},
    "Q": {"Qualifying"},
    "R": {"Race"},
}


def ingest_season(
    year: int,
    sessions: tuple[str, ...] = DEFAULT_SEASON_SESSIONS,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Ingest a whole season, carrying on past the sessions that fail.

    One missing session must not cost the other forty-seven: a race that was
    never run, a feed that came back empty, a weekend that changed format.
    Each failure is recorded and reported at the end.
    """
    refs = season_refs(year, sessions, cache_dir)
    LOGGER.info("%d sessions to ingest for %d", len(refs), year)
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    for index, ref in enumerate(refs, start=1):
        LOGGER.info("[%d/%d] %s", index, len(refs), ref.slug)
        try:
            ingest(ref, output_dir, cache_dir)
        except Exception as error:  # noqa: BLE001 - one bad session, not the run
            LOGGER.warning("  skipped %s: %s", ref.slug, error)
            failed.append((ref.slug, str(error)))
            continue
        done.append(ref.slug)
    return done, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-ingest",
        description="Fetch one session from FastF1 into the bronze layer.",
    )
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument(
        "--event", help='event name, e.g. "Sao Paulo Grand Prix"'
    )
    parser.add_argument("--session", help="FP1, FP2, FP3, Q, SQ, S or R")
    parser.add_argument(
        "--season",
        action="store_true",
        help="ingest every round of --year instead of one session",
    )
    parser.add_argument(
        "--sessions",
        default=",".join(DEFAULT_SEASON_SESSIONS),
        help="with --season: which sessions per round (default Q,R)",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.season:
        sessions = tuple(
            code.strip().upper() for code in args.sessions.split(",") if code.strip()
        )
        unknown = [code for code in sessions if code not in SESSION_CODES]
        if unknown:
            LOGGER.error("unknown session code(s): %s", ", ".join(unknown))
            return 2
        done, failed = ingest_season(
            args.year, sessions, args.output_dir, args.cache_dir
        )
        LOGGER.info("ingested %d sessions, skipped %d", len(done), len(failed))
        for slug, reason in failed:
            LOGGER.info("  skipped %s: %s", slug, reason)
        return 0 if done else 1

    if not args.event or not args.session:
        LOGGER.error("give --event and --session, or --season")
        return 2

    ref = SessionRef(year=args.year, event=args.event, session=args.session)
    try:
        session_dir = ingest(ref, args.output_dir, args.cache_dir)
    except EmptySessionError as error:
        LOGGER.error("%s", error)
        return 1

    LOGGER.info("bronze written to %s", session_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
