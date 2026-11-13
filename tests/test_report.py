"""Tests for the HTML report.

The report is the artefact a visitor looks at, so what matters is that every
figure on it comes from the warehouse and that an empty section says so
instead of rendering a blank chart.
"""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from f1platform import report, warehouse
from tests.test_warehouse import SESSION, lap, make_pace_results, write_bronze


@pytest.fixture
def rendered(tmp_path):
    rows = [lap("FST", n, 90.0 + n * 0.1) for n in range(1, 7)]
    rows += [lap("SLW", n, 91.0 + n * 0.1) for n in range(1, 7)]
    rows += [lap("DNF", n, 92.0, compound="INTERMEDIATE") for n in range(1, 3)]
    bronze = write_bronze(tmp_path / "bronze", make_pace_results(), pd.DataFrame(rows))
    db_path = tmp_path / "r.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    yield report.render(connection, SESSION), connection
    connection.close()


@pytest.fixture
def page(rendered):
    return rendered[0]


def test_the_page_is_self_contained(page):
    """One inline script (the tooltip, ADR 0004) and nothing fetched."""
    assert page.count("<script") == 1, "exactly one script: the inline tooltip"
    assert " src=" not in page, "nothing is loaded from anywhere"
    assert "http://" not in page and "https://" not in page, "no external assets"


def test_the_tooltip_script_only_reads_what_python_wrote():
    """The page is complete without the script: it computes and fetches nothing."""
    for forbidden in ("fetch(", "XMLHttpRequest", "innerHTML", "eval("):
        assert forbidden not in report.TOOLTIP_SCRIPT


def test_every_lap_dot_says_who_and_when(rendered):
    _page, connection = rendered
    chart = report.lap_time_chart(connection)

    assert chart.count("data-d=") == chart.count("<circle")
    assert "FST · volta 1" in chart


def test_the_position_chart_starts_on_the_grid(rendered):
    """Lap 0 is the grid slot: the start must not be hidden inside lap one."""
    _page, connection = rendered
    chart = report.position_chart(connection)

    assert "FST · largada" in chart
    assert "largada</text>" in chart, "the axis names lap 0"


def test_the_track_state_is_drawn_under_the_lap_chart(rendered):
    _page, connection = rendered
    laps = connection.execute("SELECT count(*) FROM gold_track_state").fetchone()[0]

    assert report.lap_time_chart(connection).count("estado da pista") == laps


def test_messages_about_one_incident_become_one_story():
    """Noted, investigated, penalised and served: four messages, one card."""
    stamp = "(14:50:05)"
    messages = [
        (37, None, f"TURN 1 INCIDENT INVOLVING CARS 23 (ALB) AND 10 (GAS) NOTED {stamp}", None),
        (40, None, "FIA STEWARDS: TURN 1 INCIDENT INVOLVING CARS 23 (ALB) AND 10 (GAS) "
                   f"UNDER INVESTIGATION {stamp}", None),
        (42, None, "FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR 23 (ALB) - "
                   f"REJOINING UNSAFELY {stamp}", None),
        (46, None, "FIA STEWARDS: PENALTY SERVED - 5 SECOND TIME PENALTY FOR "
                   f"CAR 23 (ALB) {stamp}", None),
        (3, None, "INCIDENT INVOLVING CAR 3 (VER) NOTED - FALSE START (14:03:17)", None),
    ]

    incidents = report.build_incidents(messages)

    assert len(incidents) == 2
    story = incidents[0]
    assert story["cars"] == {"ALB", "GAS"} and story["turn"] == "1"
    assert [step[0] for step in story["steps"]] == [37, 40, 42, 46]
    assert [step[2] for step in story["steps"]] == ["open", "open", "penalty", "served"]
    assert story["reason"] == "Voltou à pista de forma insegura"


def test_two_incidents_at_the_same_second_stay_separate():
    """Two cars jumping the same start share a clock time, not an incident."""
    messages = [
        (3, None, "INCIDENT INVOLVING CAR 23 (ALB) NOTED - FALSE START (14:03:17)", None),
        (3, None, "INCIDENT INVOLVING CAR 3 (VER) NOTED - FALSE START (14:03:17)", None),
    ]

    assert len(report.build_incidents(messages)) == 2


@pytest.mark.parametrize(
    ("flag", "message", "kind"),
    [
        ("BLUE", "WAVED BLUE FLAG FOR CAR 51 (CAM)", "blue"),
        ("DOUBLE YELLOW", "DOUBLE YELLOW IN TRACK SECTOR 13", "yellow"),
        (None, "CAR 5 (BOR) TIME 1:28.993 DELETED - TRACK LIMITS AT TURN 1 LAP 15", "deleted"),
        (None, "FIA STEWARDS: 10 SECOND TIME PENALTY FOR CAR 27 (HUL)", "penalty"),
        (None, "OVERTAKE DISABLED", "track"),
    ],
)
def test_race_control_messages_are_sorted_into_kinds(flag, message, kind):
    assert report.classify_message(flag, message) == kind


def test_every_driver_appears_in_the_classification(page):
    for code in ("FST", "SLW", "DNF"):
        assert f"<b>{code}</b>" in page


def test_an_unclassified_driver_is_marked(page):
    assert "unclassified" in page, "a retirement must be visually distinguished"


def test_the_lap_chart_plots_every_representative_lap(rendered):
    """The invariant, not a magic number: one dot per usable lap.

    Counted on the chart itself rather than the page, because other charts
    draw circles too -- the speed chart marks each driver's median with one.
    """
    _page, connection = rendered
    usable = connection.execute(
        "SELECT sum(is_representative::int) FROM gold_representative_laps"
    ).fetchone()[0]

    assert report.lap_time_chart(connection).count("<circle") == usable


def test_an_optional_tab_is_dropped_when_its_table_is_absent(rendered):
    """A session without weather data gets no weather tab, not an empty one."""
    page, _connection = rendered

    assert "id='panel-weather'" not in page
    assert "id='panel-summary'" in page


def test_compound_colours_follow_the_television_convention(page):
    assert report.COMPOUND_COLOURS["SOFT"] in page
    assert report.COMPOUND_COLOURS["INTERMEDIATE"] in page


def test_a_session_without_laps_says_so_instead_of_drawing_nothing(tmp_path):
    results = make_pace_results()
    empty = pd.DataFrame([lap("FST", 1, 90.0, pit_in=1.0)])
    bronze = write_bronze(tmp_path / "bronze", results, empty)
    db_path = tmp_path / "empty.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        rendered = report.render(connection, SESSION)
    finally:
        connection.close()

    assert "Nenhuma volta representativa" in rendered


def test_a_missing_warehouse_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="f1-build"):
        report.build_report(SESSION, tmp_path / "absent.duckdb", tmp_path)


def test_a_lapped_driver_shows_the_lap_deficit(rendered):
    """The screens say "+1 volta", not a blank cell."""
    page, _ = rendered

    assert "+1 volta" in page


def test_driver_names_are_escaped(page):
    assert "<script>" not in page
