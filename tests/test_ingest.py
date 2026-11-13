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


# --- event metadata ---------------------------------------------------------


def fake_event(**overrides):
    """A row of FastF1's schedule, with only the fields ingestion reads."""
    row = {
        "RoundNumber": 21,
        "EventName": "São Paulo Grand Prix",
        "OfficialEventName": "FORMULA 1 GRANDE PRÊMIO DE SÃO PAULO 2026",
        "Country": "Brazil",
        "Location": "São Paulo",
        "EventFormat": "conventional",
        "EventDate": pd.Timestamp("2026-11-08"),
    }
    row.update(overrides)
    return pd.Series(row)


def test_the_event_is_recorded_with_the_session(tmp_path):
    """The round number comes free with the session FastF1 already loaded."""
    session = FakeSession(pd.DataFrame({"Abbreviation": ["FST"]}))
    session.event = fake_event()

    metadata = ing.event_metadata(session)
    ing.write_bronze(
        ing.SessionRef(2026, "Sao Paulo Grand Prix", "R"),
        {"results": pd.DataFrame({"Abbreviation": ["FST"]})},
        tmp_path,
        metadata,
    )

    written = json.loads(
        (tmp_path / "2026-sao-paulo-grand-prix-r" / "event.json").read_text()
    )
    assert written["RoundNumber"] == 21
    assert written["EventDate"] == "2026-11-08", "a date, not a timestamp blob"
    assert written["session_code"] == "R"


def test_a_session_without_event_metadata_still_lands(tmp_path):
    """Older sessions, and anything FastF1 cannot name, must not break."""
    session = FakeSession(pd.DataFrame({"Abbreviation": ["FST"]}))

    assert ing.event_metadata(session) == {}

    ing.write_bronze(
        ing.SessionRef(2026, "Sao Paulo Grand Prix", "R"),
        {"results": pd.DataFrame({"Abbreviation": ["FST"]})},
        tmp_path,
        {},
    )

    assert not (tmp_path / "2026-sao-paulo-grand-prix-r" / "event.json").exists()


# --- a whole season ---------------------------------------------------------


def fake_schedule():
    """Two rounds: one conventional, one with a sprint."""
    return pd.DataFrame([
        {"RoundNumber": 1, "EventName": "Alpha Grand Prix",
         "Session1": "Practice 1", "Session2": "Practice 2",
         "Session3": "Practice 3", "Session4": "Qualifying", "Session5": "Race"},
        {"RoundNumber": 2, "EventName": "Beta Grand Prix",
         "Session1": "Practice 1", "Session2": "Sprint Qualifying",
         "Session3": "Sprint", "Session4": "Qualifying", "Session5": "Race"},
    ])


def test_a_season_lists_only_the_sessions_each_round_held(monkeypatch, tmp_path):
    monkeypatch.setattr(ing.fastf1, "get_event_schedule",
                        lambda *a, **k: fake_schedule())

    refs = ing.season_refs(2026, ("Q", "R", "S"), tmp_path)

    assert [ref.slug for ref in refs] == [
        "2026-alpha-grand-prix-q",
        "2026-alpha-grand-prix-r",
        "2026-beta-grand-prix-s",
        "2026-beta-grand-prix-q",
        "2026-beta-grand-prix-r",
    ], "the sprint belongs to the second round only"


def test_one_broken_session_does_not_stop_the_season(monkeypatch, tmp_path):
    """Forty-seven good sessions must not be lost to one bad one."""
    monkeypatch.setattr(ing.fastf1, "get_event_schedule",
                        lambda *a, **k: fake_schedule())

    attempted = []

    def flaky(ref, output_dir, cache_dir):
        attempted.append(ref.slug)
        if ref.slug == "2026-alpha-grand-prix-r":
            raise ing.EmptySessionError("no drivers")
        return output_dir / ref.slug

    monkeypatch.setattr(ing, "ingest", flaky)
    done, failed = ing.ingest_season(2026, ("Q", "R"), tmp_path, tmp_path)

    assert len(attempted) == 4
    assert done == ["2026-alpha-grand-prix-q", "2026-beta-grand-prix-q",
                    "2026-beta-grand-prix-r"]
    assert failed == [("2026-alpha-grand-prix-r", "no drivers")]


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
