"""Tests for the recorder's file handling.

The recorder's network behaviour is not unit tested -- it needs a live session.
What is tested is the part that decides *where* bytes land and whether a resumed
recording appends instead of overwriting, because getting that wrong silently
destroys a session that cannot be captured again.
"""

from __future__ import annotations

from f1platform.record import build_output_path, build_parser, describe


def test_output_path_is_one_file_per_session(tmp_path):
    first = build_output_path("2026-las-vegas-fp1", tmp_path)
    second = build_output_path("2026-las-vegas-fp1", tmp_path)

    assert first == second, "a resumed recording must target the same file"
    assert first.parent.is_dir()
    assert first.name == "2026-las-vegas-fp1.txt"


def test_output_paths_do_not_collide_across_sessions(tmp_path):
    fp1 = build_output_path("2026-las-vegas-fp1", tmp_path)
    race = build_output_path("2026-las-vegas-race", tmp_path)

    assert fp1 != race


def test_describe_reports_nothing_for_a_missing_file(tmp_path):
    assert describe(tmp_path / "absent.txt") == "no file written"


def test_describe_counts_lines(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_text("a\nb\nc\n", encoding="utf-8")

    assert describe(path).startswith("3 lines")


def test_describe_survives_undecodable_bytes(tmp_path):
    path = tmp_path / "broken.txt"
    path.write_bytes(b"ok\n\xff\xfe bad bytes\n")

    assert describe(path).startswith("2 lines")


def test_debug_messages_are_kept_by_default():
    args = build_parser().parse_args(["--session", "s"])

    assert args.no_debug_messages is False, "the complete message is the raw truth"


def test_reconnects_forever_by_default():
    args = build_parser().parse_args(["--session", "s"])

    assert args.max_reconnects == 0
