"""Record the Formula 1 live timing stream into a raw, append-only log.

The raw log is this project's source of truth. Every byte the feed sends is
written to disk before anything tries to interpret it, which buys three
things:

* a recorded session can be replayed any number of times, so development
  never has to wait for the next race weekend;
* reprocessing is idempotent -- change the parser, replay the same bytes;
* a change to the feed's format costs a parser rewrite, not the data.

The recorder deliberately does no parsing. See docs/adr/0001.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastf1.livetiming.client import SignalRClient

LOGGER = logging.getLogger("f1platform.record")

DEFAULT_OUTPUT_DIR = Path("data/raw")
DEFAULT_IDLE_TIMEOUT = 60
RECONNECT_BACKOFF_SECONDS = 5


def build_output_path(session: str, output_dir: Path) -> Path:
    """Return the file this run appends to: data/raw/<session>/<session>.txt.

    One file per session, not per run. A dropped connection resumes into the
    same file, so a session interrupted halfway is still one continuous log.
    """
    session_dir = output_dir / session
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / f"{session}.txt"


def describe(path: Path) -> str:
    """Human-readable size and line count for a recorded log."""
    if not path.exists():
        return "no file written"
    size_mb = path.stat().st_size / (1024 * 1024)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = sum(1 for _ in handle)
    return f"{lines:,} lines, {size_mb:.1f} MB"


def record_once(path: Path, idle_timeout: int, debug: bool) -> None:
    """Run a single client session, appending to `path` if it already exists.

    SignalRClient exits on its own once `idle_timeout` seconds pass with no
    message. That is the normal way a session ends, and also how a silent
    connection drop surfaces -- the supervisor decides whether to reconnect.
    """
    filemode = "a" if path.exists() else "w"
    client = SignalRClient(
        filename=str(path),
        filemode=filemode,
        debug=debug,
        timeout=idle_timeout,
    )
    asyncio.run(client.async_start())


def record(
    session: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    max_reconnects: int = 0,
    debug: bool = True,
) -> Path:
    """Record a session, reconnecting until the user stops it.

    `max_reconnects=0` means reconnect indefinitely. The recorder is meant to
    be started before a session and left alone, so giving up on the first
    network hiccup would defeat its purpose.
    """
    path = build_output_path(session, output_dir)
    LOGGER.info("recording %s", session)
    LOGGER.info("writing to %s", path)
    LOGGER.info("stop with Ctrl+C when the session is over")

    attempt = 0
    started = time.monotonic()
    while True:
        attempt += 1
        if attempt > 1:
            LOGGER.info("reconnect attempt %d", attempt - 1)
        try:
            record_once(path, idle_timeout, debug)
        except KeyboardInterrupt:
            LOGGER.info("stopped by user")
            break
        except Exception:  # noqa: BLE001 - the supervisor must survive anything
            LOGGER.exception("client failed; will reconnect")

        LOGGER.info("client exited after %s", describe(path))

        if max_reconnects and (attempt - 1) >= max_reconnects:
            LOGGER.info("reconnect limit reached, stopping")
            break

        try:
            time.sleep(RECONNECT_BACKOFF_SECONDS)
        except KeyboardInterrupt:
            LOGGER.info("stopped by user")
            break

    elapsed = time.monotonic() - started
    LOGGER.info("done after %.0f min -- %s", elapsed / 60, describe(path))
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-record",
        description="Record the F1 live timing stream to a raw append-only log.",
    )
    parser.add_argument(
        "--session",
        required=True,
        help="slug for this session, e.g. 2026-las-vegas-fp1",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"root folder for raw logs (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT,
        help=(
            "seconds of silence before the client gives up and the supervisor "
            f"reconnects (default: {DEFAULT_IDLE_TIMEOUT})"
        ),
    )
    parser.add_argument(
        "--max-reconnects",
        type=int,
        default=0,
        help="stop after this many reconnects; 0 means never stop (default: 0)",
    )
    parser.add_argument(
        "--no-debug-messages",
        action="store_true",
        help=(
            "store only the payload instead of the complete SignalR message; "
            "the complete message is kept by default because it is the raw truth"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    LOGGER.info("f1-record starting at %s", started_at)
    record(
        session=args.session,
        output_dir=args.output_dir,
        idle_timeout=args.idle_timeout,
        max_reconnects=args.max_reconnects,
        debug=not args.no_debug_messages,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
