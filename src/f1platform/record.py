"""Record the Formula 1 live timing stream into a raw, append-only log.

The raw log is this project's source of truth. Whatever the feed sends is
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
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from fastf1.livetiming.client import SignalRClient

LOGGER = logging.getLogger("f1platform.record")

DEFAULT_OUTPUT_DIR = Path("data/raw")
DEFAULT_IDLE_TIMEOUT = 60
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3
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


def size_of(path: Path) -> int:
    """Bytes on disk, or 0 if the file does not exist yet."""
    return path.stat().st_size if path.exists() else 0


@dataclass
class StopFlag:
    """Set when the user asks to stop, so the supervisor knows not to reconnect."""

    requested: bool = False


@contextmanager
def interrupt_guard():
    """Make Ctrl+C visible to the supervisor, not just to the client.

    `SignalRClient.start` catches KeyboardInterrupt itself, shuts down and
    returns normally. Without this, a user pressing Ctrl+C would look exactly
    like a session ending quietly, and the supervisor would reconnect -- so
    stopping the recorder would take one Ctrl+C per reconnect, forever.

    The handler records the intent *and* raises, so the client still gets to
    close its connection cleanly on the way out.
    """
    flag = StopFlag()

    def handler(signum, frame):  # noqa: ARG001 - signal handler signature
        flag.requested = True
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:
        # Not on the main thread; the caller handles interrupts its own way.
        yield flag
        return

    try:
        yield flag
    finally:
        signal.signal(signal.SIGINT, previous)


class AuthError(RuntimeError):
    """Raised when the F1 sign-in fails, instead of letting the client guess."""


def sign_in() -> None:
    """Obtain a livetiming token, raising a readable error if that fails.

    This runs *before* the client starts, on purpose. Left to itself, FastF1
    reacts to a rejected password by logging a warning, continuing without
    authentication, and then failing inside the SignalR layer with
    ``TypeError: access_token_factory is not function`` -- a message that says
    nothing about the actual problem, which is a typo in a password.

    Credentials come from the environment variables ``F1_USERNAME`` and
    ``F1_PASSWORD``; without them an interactive terminal is prompted.
    """
    try:
        from fastf1.internals.f1account import get_token
    except ImportError as error:  # pragma: no cover - version drift
        raise AuthError(
            f"this FastF1 version does not expose the account helper ({error}); "
            "sign-in behaviour has changed and the recorder needs updating"
        ) from error

    try:
        get_token()
    except Exception as error:  # noqa: BLE001 - any failure means "do not proceed"
        raise AuthError(
            f"F1 sign-in failed: {error}. Check the account at "
            "https://account.formula1.com, run --check-auth to retry the login, "
            "or pass --no-auth to connect without it"
        ) from error


def check_auth() -> bool:
    """Sign in now and report whether it worked.

    The feed requires a (free) Formula 1 account. Finding that out while a
    session is running is too late, so this runs the sign-in on demand, days
    early. FastF1 stores the resulting token on disk and reuses it, which is
    why a reconnect mid-session does not prompt again.
    """
    if os.environ.get("F1_USERNAME"):
        LOGGER.info("using credentials from F1_USERNAME / F1_PASSWORD")
    else:
        LOGGER.info("F1_USERNAME is not set, so you will be prompted")

    try:
        sign_in()
    except AuthError as error:
        LOGGER.error("%s", error)
        return False

    LOGGER.info("sign-in succeeded; the token is stored and will be reused")
    return True


def record_once(path: Path, idle_timeout: int, no_auth: bool = False) -> None:
    """Run a single client session, appending to `path` if it already exists.

    SignalRClient exits on its own once `idle_timeout` seconds pass with no
    message. That is the normal way a session ends, and also how a silent
    connection drop surfaces -- the supervisor decides whether to reconnect.
    """
    if not no_auth:
        sign_in()

    filemode = "a" if path.exists() else "w"
    client = SignalRClient(
        filename=str(path),
        filemode=filemode,
        timeout=idle_timeout,
        no_auth=no_auth,
    )
    client.start()


def record(
    session: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    max_reconnects: int = 0,
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
    runner: Callable[[Path, int], None] = record_once,
) -> Path:
    """Record a session, reconnecting until the user stops it.

    Two kinds of failure are told apart, because they deserve opposite
    responses:

    * a **clean exit** means the client ran and then went quiet. That is what
      a finished session looks like, and also what a silent network drop looks
      like, so it is always worth reconnecting -- indefinitely when
      `max_reconnects` is 0.
    * an **exception** means the client could not run at all. A bad argument
      or a broken install fails this way every single time, so retrying it
      forever just fills the terminal with the same traceback. After
      `max_consecutive_errors` in a row the recorder gives up and says so.

    A single exception between good runs is forgiven: the counter resets on
    any clean exit.
    """
    path = build_output_path(session, output_dir)
    LOGGER.info("recording %s", session)
    LOGGER.info("writing to %s", path)
    LOGGER.info("stop with Ctrl+C when the session is over")

    attempt = 0
    consecutive_errors = 0
    started = time.monotonic()

    with interrupt_guard() as stop:
        while True:
            attempt += 1
            if attempt > 1:
                LOGGER.info("reconnect attempt %d", attempt - 1)

            bytes_before = size_of(path)
            try:
                runner(path, idle_timeout)
            except KeyboardInterrupt:
                LOGGER.info("stopped by user")
                break
            except Exception as error:  # noqa: BLE001 - the supervisor survives anything
                consecutive_errors += 1
                LOGGER.error(
                    "client failed (%d in a row): %s: %s",
                    consecutive_errors,
                    type(error).__name__,
                    error,
                )
                if consecutive_errors >= max_consecutive_errors:
                    LOGGER.error(
                        "giving up after %d failures in a row -- the client could not "
                        "run at all, so check the error above: a bad argument, a broken "
                        "install, or no route to the feed",
                        consecutive_errors,
                    )
                    break
            else:
                if stop.requested:
                    # The client caught the Ctrl+C itself and returned normally.
                    LOGGER.info("stopped by user")
                    break
                consecutive_errors = 0
                captured = size_of(path) - bytes_before
                LOGGER.info(
                    "client exited cleanly, captured %s this run (%s total)",
                    f"{captured:,} bytes",
                    describe(path),
                )

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
        help="slug for this session, e.g. 2026-las-vegas-fp1",
    )
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help=(
            "sign in to the F1 account and exit, to verify credentials days "
            "before a session instead of discovering a problem during one"
        ),
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "connect without signing in. FastF1 warns this may work for only "
            "some sessions, or return empty or partial data -- not worth the "
            "risk on a session that cannot be recorded twice"
        ),
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
        "--max-consecutive-errors",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_ERRORS,
        help=(
            "stop after this many failures in a row, which signal a bug rather "
            f"than a network problem (default: {DEFAULT_MAX_CONSECUTIVE_ERRORS})"
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

    if args.check_auth:
        return 0 if check_auth() else 1

    if not args.session:
        LOGGER.error("--session is required unless you pass --check-auth")
        return 2

    record(
        session=args.session,
        output_dir=args.output_dir,
        idle_timeout=args.idle_timeout,
        max_reconnects=args.max_reconnects,
        max_consecutive_errors=args.max_consecutive_errors,
        runner=partial(record_once, no_auth=args.no_auth),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
