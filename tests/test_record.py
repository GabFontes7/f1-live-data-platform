"""Tests for the recorder's file handling and its supervisor loop.

The network itself is not unit tested -- that needs a live session. What is
tested is everything that decides whether a session survives: where bytes
land, that resuming appends instead of overwriting, and that a failure which
will never succeed stops instead of looping forever.

That last one is not hypothetical. The first version retried a bad argument
twenty-four times in two minutes before the author stopped it by hand.
"""

from __future__ import annotations

import pytest

from f1platform import record as rec


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Keep the supervisor's sleep out of the test suite's runtime."""
    monkeypatch.setattr(rec, "RECONNECT_BACKOFF_SECONDS", 0)


# --- where bytes land -------------------------------------------------------


def test_output_path_is_one_file_per_session(tmp_path):
    first = rec.build_output_path("2026-las-vegas-fp1", tmp_path)
    second = rec.build_output_path("2026-las-vegas-fp1", tmp_path)

    assert first == second, "a resumed recording must target the same file"
    assert first.parent.is_dir()
    assert first.name == "2026-las-vegas-fp1.txt"


def test_output_paths_do_not_collide_across_sessions(tmp_path):
    fp1 = rec.build_output_path("2026-las-vegas-fp1", tmp_path)
    race = rec.build_output_path("2026-las-vegas-race", tmp_path)

    assert fp1 != race


def test_describe_reports_nothing_for_a_missing_file(tmp_path):
    assert rec.describe(tmp_path / "absent.txt") == "no file written"


def test_describe_counts_lines(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("a\nb\nc\n", encoding="utf-8")

    assert rec.describe(path).startswith("3 lines")


def test_describe_survives_undecodable_bytes(tmp_path):
    path = tmp_path / "broken.txt"
    path.write_bytes(b"ok\n\xff\xfe bad bytes\n")

    assert rec.describe(path).startswith("2 lines")


def test_size_of_is_zero_for_a_missing_file(tmp_path):
    assert rec.size_of(tmp_path / "absent.txt") == 0


# --- the supervisor loop ----------------------------------------------------


def test_a_failure_that_never_succeeds_stops(tmp_path):
    """A bad argument fails identically every time; retrying it is pointless."""
    attempts = []

    def always_fails(path, idle_timeout):
        attempts.append(path)
        raise ValueError("Debug mode is no longer supported.")

    rec.record(
        session="s",
        output_dir=tmp_path,
        max_consecutive_errors=3,
        runner=always_fails,
    )

    assert len(attempts) == 3, "must stop at the limit, not loop forever"


def test_a_clean_exit_forgives_an_earlier_failure(tmp_path):
    """One bad run between good ones is a hiccup, not a reason to give up."""
    calls = {"n": 0}

    def fails_once_then_works(path, idle_timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("dropped")
        path.write_text("data\n", encoding="utf-8")

    rec.record(
        session="s",
        output_dir=tmp_path,
        max_reconnects=4,
        max_consecutive_errors=2,
        runner=fails_once_then_works,
    )

    assert calls["n"] == 5, "the error counter must reset after a clean exit"


def test_clean_exits_reconnect_up_to_the_limit(tmp_path):
    """A silent feed is normal before a session starts; keep reconnecting."""
    calls = {"n": 0}

    def quiet(path, idle_timeout):
        calls["n"] += 1

    rec.record(session="s", output_dir=tmp_path, max_reconnects=3, runner=quiet)

    assert calls["n"] == 4, "one first run plus three reconnects"


def test_user_interrupt_stops_immediately(tmp_path):
    calls = {"n": 0}

    def interrupted(path, idle_timeout):
        calls["n"] += 1
        raise KeyboardInterrupt

    rec.record(session="s", output_dir=tmp_path, runner=interrupted)

    assert calls["n"] == 1


def test_record_returns_the_path_it_wrote(tmp_path):
    path = rec.record(
        session="2026-las-vegas-fp1",
        output_dir=tmp_path,
        max_reconnects=1,
        runner=lambda path, idle_timeout: None,
    )

    assert path == tmp_path / "2026-las-vegas-fp1" / "2026-las-vegas-fp1.txt"


# --- CLI defaults -----------------------------------------------------------


def test_reconnects_forever_by_default():
    args = rec.build_parser().parse_args(["--session", "s"])

    assert args.max_reconnects == 0


def test_gives_up_on_repeated_errors_by_default():
    args = rec.build_parser().parse_args(["--session", "s"])

    assert args.max_consecutive_errors == rec.DEFAULT_MAX_CONSECUTIVE_ERRORS
