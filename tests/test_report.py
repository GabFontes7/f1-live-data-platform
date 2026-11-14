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


def build_page(tmp_path, laps, name="x"):
    """Render a whole page from a handful of laps."""
    bronze = write_bronze(tmp_path / name, make_pace_results(), pd.DataFrame(laps))
    db_path = tmp_path / f"{name}.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        return report.render(connection, SESSION), report.track_condition(connection)[1]
    finally:
        connection.close()


def dry_laps(**kwargs):
    return [lap(driver, number, 90.0 + number * 0.1, **kwargs)
            for driver in ("FST", "SLW", "DNF") for number in range(1, 7)]


def test_one_session_is_rendered_even_when_the_warehouse_holds_several(tmp_path):
    """Every table with a session_slug is scoped, including ones added later."""
    from tests.test_warehouse import make_qualifying_results, qualifying_laps

    db_path = tmp_path / "many.duckdb"
    first = write_bronze(tmp_path / "a", make_qualifying_results(
        q1=[95.0, 95.4, 95.9], q2=[94.0, 94.4, None], q3=[93.0, None, None]
    ), qualifying_laps())
    warehouse.build(SESSION, first, db_path)

    second = tmp_path / "b"
    (second / "2026-other-q").mkdir(parents=True)
    other = make_qualifying_results(
        q1=[85.0, 85.4, 85.9], q2=[84.0, 84.4, None], q3=[83.0, None, None]
    )
    other.to_parquet(second / "2026-other-q" / "results.parquet", index=False)
    laps = qualifying_laps()
    laps["LapTime"] = laps["LapTime"] - 10.0
    laps.to_parquet(second / "2026-other-q" / "laps.parquet", index=False)
    warehouse.build("2026-other-q", second, db_path)

    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        report.scope_to_session(connection, SESSION)
        drivers, rows = connection.execute(
            "SELECT count(DISTINCT driver_code), count(*) "
            "FROM gold_qualifying_result"
        ).fetchone()
        page = report.render(connection, SESSION)
    finally:
        connection.close()

    assert (drivers, rows) == (3, 3), "the other session must not leak in"
    assert "1:25.000" not in page, "no lap from the other session on the page"


def test_a_driver_without_a_position_does_not_break_the_page(tmp_path):
    """Across a season, every nullable column eventually arrives null."""
    from tests.test_warehouse import make_qualifying_results, qualifying_laps

    results = make_qualifying_results(
        q1=[95.0, 95.4, None], q2=[94.0, 94.4, None], q3=[93.0, None, None]
    )
    results["Position"] = [1.0, 2.0, None]
    bronze = write_bronze(tmp_path / "bronze", results, qualifying_laps())
    db_path = tmp_path / "null.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        page = report.render(connection, SESSION)
    finally:
        connection.close()

    assert "DNF" in page, "the driver is still listed"
    assert "<td class='pos'>—</td>" in page, "with a dash where the position was"
    assert "Pole" in page and "FST" in page


def test_a_qualifying_session_gets_its_own_page(tmp_path):
    """A qualifying session is not a short race: different tabs, different cards."""
    from tests.test_warehouse import make_qualifying_results, qualifying_laps

    results = make_qualifying_results(
        q1=[95.0, 95.4, 95.9], q2=[94.0, 94.4, None], q3=[93.0, None, None]
    )
    bronze = write_bronze(tmp_path / "bronze", results, qualifying_laps())
    db_path = tmp_path / "q.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        page = report.render(connection, SESSION)
        slugs = [slug for slug, _, _ in report.tabs_for(connection)]
    finally:
        connection.close()

    assert slugs[:2] == ["summary", "laps"]
    assert "Como ficou o grid" in page and "Pole" in page
    assert "Classificação</h2>" not in page, "no race classification table"
    assert "Estratégia" not in page, "no pit strategy in a qualifying session"


def test_a_dry_race_says_nothing_about_rain(tmp_path):
    """The page describes this session, not the one the code was written for.

    A card reading "0 of 71 laps with a wet track" is a leftover from the
    2026 Sao Paulo race, which is why this test exists.
    """
    page, bands = build_page(tmp_path, dry_laps(), name="dry")

    assert "wet" not in bands
    assert "pista molhada" not in page
    assert "Voltas com pista molhada" not in page
    assert "pneu intermediário" not in page
    assert "card-label'>Voltas com" not in page, (
        "a card counting zero laps of something that did not happen is noise"
    )


def test_a_wet_race_does_say_so(tmp_path):
    page, bands = build_page(tmp_path, dry_laps(compound="INTERMEDIATE"), name="wet")

    assert "wet" in bands
    assert "Voltas com pista molhada" in page


def test_a_race_without_neutralisation_draws_no_flag_band(tmp_path):
    _page, bands = build_page(tmp_path, dry_laps(), name="green")

    assert bands == []


def test_a_safety_car_earns_its_band_and_its_card(tmp_path):
    laps = dry_laps()
    for row in laps:
        if row["LapNumber"] in (3.0, 4.0):
            row["TrackStatus"] = "14"
    page, bands = build_page(tmp_path, laps, name="sc")

    assert bands == ["neutralisation"]
    assert "Voltas com safety car" in page
    assert "Safety car na pista" in page, "the band explains itself on hover"


def test_the_track_state_is_drawn_under_the_lap_chart(rendered):
    _page, connection = rendered
    wet_laps = connection.execute(
        "SELECT count(*) FROM gold_track_state WHERE wet_tyre_share > 0"
    ).fetchone()[0]
    chart = report.lap_time_chart(connection)

    assert chart.count("fill='#0067ad'") == wet_laps, "one cell per wet lap"
    assert "Pista secando ou começando a molhar" in chart, (
        "a third of the grid on intermediates is not a wet track yet"
    )


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


def two_session_warehouse(tmp_path):
    """A race and a qualifying session, as the site will hold them."""
    from tests.test_warehouse import make_qualifying_results, qualifying_laps

    db_path = tmp_path / "site.duckdb"
    bronze = tmp_path / "bronze"
    for slug, results, laps in (
        ("2026-somewhere-grand-prix-r", make_pace_results(),
         pd.DataFrame(dry_laps())),
        ("2026-somewhere-grand-prix-q",
         make_qualifying_results(q1=[95.0, 95.4, 95.9], q2=[94.0, 94.4, None],
                                 q3=[93.0, None, None]),
         qualifying_laps()),
    ):
        (bronze / slug).mkdir(parents=True)
        results.to_parquet(bronze / slug / "results.parquet", index=False)
        laps.to_parquet(bronze / slug / "laps.parquet", index=False)
        warehouse.build(slug, bronze, db_path)
    return db_path


def test_one_command_builds_every_page_and_an_index(tmp_path):
    """A new grand prix is an ingest away from being on the site."""
    db_path = two_session_warehouse(tmp_path)

    written = report.build_site(db_path, tmp_path / "site")

    names = sorted(path.name for path in written)
    assert names == [
        "2026-somewhere-grand-prix-q.html",
        "2026-somewhere-grand-prix-r.html",
        "index.html",
    ]
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "Somewhere Grand Prix 2026" in index
    assert "Classificação" in index and "Corrida" in index


def test_every_page_links_to_every_other(tmp_path):
    db_path = two_session_warehouse(tmp_path)

    report.build_site(db_path, tmp_path / "site")

    race = (tmp_path / "site" / "2026-somewhere-grand-prix-r.html").read_text(
        encoding="utf-8"
    )
    assert "href='2026-somewhere-grand-prix-q.html'" in race
    assert "href='index.html'" in race
    assert "href='2026-somewhere-grand-prix-r.html'" not in race, (
        "a page does not link to itself"
    )


def test_the_index_numbers_the_rounds_when_it_knows_them(tmp_path):
    from tests.test_warehouse import write_event

    db_path = two_session_warehouse(tmp_path)
    bronze = tmp_path / "bronze"
    for slug, number in (("2026-somewhere-grand-prix-r", 7),
                         ("2026-somewhere-grand-prix-q", 7)):
        write_event(bronze, slug, round_number=number, name="Somewhere Grand Prix")
        warehouse.build(slug, bronze, db_path)

    report.build_site(db_path, tmp_path / "site")
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")

    assert "Etapa 7 · Somewhere Grand Prix" in index


def test_each_qualifying_time_shows_the_tyre_it_was_set_on(tmp_path):
    """A wet session changes tyre between segments: the table must show it."""
    from tests.test_warehouse import make_qualifying_results, qualifying_laps

    laps = qualifying_laps()
    laps["Compound"] = [
        "WET" if number <= 2 else "INTERMEDIATE" for number in laps["LapNumber"]
    ]
    bronze = write_bronze(tmp_path / "bronze", make_qualifying_results(
        q1=[95.0, 95.4, 95.9], q2=[94.0, 94.4, None], q3=[93.0, None, None]
    ), laps)
    db_path = tmp_path / "t.duckdb"
    warehouse.build(SESSION, bronze, db_path)
    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        tyres = report.segment_tyres(connection)
        page = report.render(connection, SESSION)
    finally:
        connection.close()

    assert tyres[("FST", 1)] == "WET"
    assert tyres[("FST", 3)] == "INTERMEDIATE"
    assert report.COMPOUND_COLOURS["WET"] in page
    assert report.COMPOUND_COLOURS["INTERMEDIATE"] in page


def test_the_fastest_lap_says_which_tyre_it_was_set_on(rendered):
    page, _connection = rendered

    assert "class='tyre'" in page
    assert report.COMPOUND_COLOURS["SOFT"] in page


def test_a_missing_warehouse_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="f1-build"):
        report.build_report(SESSION, tmp_path / "absent.duckdb", tmp_path)


def test_a_lapped_driver_shows_the_lap_deficit(rendered):
    """The screens say "+1 volta", not a blank cell."""
    page, _ = rendered

    assert "+1 volta" in page


def test_driver_names_are_escaped(page):
    assert "<script>" not in page
