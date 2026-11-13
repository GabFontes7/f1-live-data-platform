"""Tests for bronze ingestion.

The network is not exercised. What is tested is the guard that makes the
rest trustworthy: FastF1 returns an *empty* session when its Formula 1
sign-in fails, and reports success doing it. A pipeline that accepts that
builds an empty warehouse and passes its own tests.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from f1platform import ingest as ing


class FakeSession:
    """Stands in for fastf1.core.Session with only what ingestion reads."""

    def __init__(self, results, laps=None):
        self.results = results
        self.laps = laps if laps is not None else pd.DataFrame()
        self.weather_data = pd.DataFrame()
        self.race_control_messages = pd.DataFrame()

    def load(self):
        pass


# --- naming -----------------------------------------------------------------


def test_slug_is_stable_and_readable():
    ref = ing.SessionRef(2026, "Sao Paulo Grand Prix", "R")

    assert ref.slug == "2026-sao-paulo-grand-prix-r"


def test_slug_separates_sessions_of_the_same_event():
    race = ing.SessionRef(2026, "Las Vegas Grand Prix", "R")
    quali = ing.SessionRef(2026, "Las Vegas Grand Prix", "Q")

    assert race.slug != quali.slug


# --- the guard that matters -------------------------------------------------


def test_an_empty_session_is_refused(monkeypatch, tmp_path):
    """A failed sign-in looks like success and yields no drivers."""
    monkeypatch.setattr(
        ing.fastf1, "get_session", lambda *a, **k: FakeSession(pd.DataFrame())
    )

    with pytest.raises(ing.EmptySessionError, match="no drivers"):
        ing.load_session(ing.SessionRef(2026, "Sao Paulo Grand Prix", "R"), tmp_path)


def test_the_refusal_names_authentication_as_the_cause(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ing.fastf1, "get_session", lambda *a, **k: FakeSession(pd.DataFrame())
    )

    with pytest.raises(ing.EmptySessionError) as error:
        ing.load_session(ing.SessionRef(2026, "Sao Paulo Grand Prix", "R"), tmp_path)

    assert "sign-in" in str(error.value), "the message must point at the real cause"


def test_a_populated_session_passes(monkeypatch, tmp_path):
    results = pd.DataFrame({"Abbreviation": ["NOR", "VER"], "Position": [1.0, 2.0]})
    monkeypatch.setattr(
        ing.fastf1, "get_session", lambda *a, **k: FakeSession(results)
    )

    session = ing.load_session(
        ing.SessionRef(2026, "Sao Paulo Grand Prix", "R"), tmp_path
    )

    assert len(session.results) == 2


# --- writing ----------------------------------------------------------------


def test_only_non_empty_tables_are_collected():
    results = pd.DataFrame({"Abbreviation": ["NOR"]})
    laps = pd.DataFrame({"Driver": ["NOR"], "LapNumber": [1]})

    frames = ing.frames_from(FakeSession(results, laps))

    assert set(frames) == {"results", "laps"}, "empty tables must not be written"


def test_timedeltas_are_stored_as_seconds(tmp_path):
    laps = pd.DataFrame(
        {
            "Driver": ["NOR"],
            "LapTime": pd.to_timedelta(["0 days 00:01:12.689000"]),
        }
    )
    ref = ing.SessionRef(2026, "Sao Paulo Grand Prix", "R")

    session_dir = ing.write_bronze(ref, {"laps": laps}, tmp_path)
    written = pd.read_parquet(session_dir / "laps.parquet")

    assert written["LapTime"].iloc[0] == pytest.approx(72.689)


def test_manifest_records_what_was_written(tmp_path):
    results = pd.DataFrame({"Abbreviation": ["NOR", "VER"], "Position": [1.0, 2.0]})
    ref = ing.SessionRef(2026, "Sao Paulo Grand Prix", "R")

    session_dir = ing.write_bronze(ref, {"results": results}, tmp_path)
    manifest = json.loads((session_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["slug"] == ref.slug
    assert manifest["tables"]["results"]["rows"] == 2
    assert "Abbreviation" in manifest["tables"]["results"]["columns"]


def test_main_exits_non_zero_on_an_empty_session(monkeypatch):
    monkeypatch.setattr(
        ing.fastf1, "get_session", lambda *a, **k: FakeSession(pd.DataFrame())
    )

    code = ing.main(
        ["--year", "2026", "--event", "Sao Paulo Grand Prix", "--session", "R"]
    )

    assert code == 1, "an empty ingest must not look like success to CI"
