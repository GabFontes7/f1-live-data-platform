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
    assert "<script" not in page, "no JavaScript: the report must open offline"
    assert "http://" not in page and "https://" not in page, "no external assets"


def test_every_driver_appears_in_the_classification(page):
    for code in ("FST", "SLW", "DNF"):
        assert f"<b>{code}</b>" in page


def test_an_unclassified_driver_is_marked(page):
    assert "unclassified" in page, "a retirement must be visually distinguished"


def test_the_lap_chart_plots_every_representative_lap(rendered):
    """The invariant, not a magic number: one dot per usable lap."""
    page, connection = rendered
    usable = connection.execute(
        "SELECT sum(is_representative::int) FROM gold_representative_laps"
    ).fetchone()[0]

    assert page.count("<circle") == usable


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

    assert "No representative laps" in rendered


def test_a_missing_warehouse_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="f1-build"):
        report.build_report(SESSION, tmp_path / "absent.duckdb", tmp_path)


def test_a_lapped_driver_shows_the_lap_deficit(rendered):
    """The screens say "+1 lap", not a blank cell."""
    page, _ = rendered

    assert "+1 lap" in page


def test_driver_names_are_escaped(page):
    assert "<script>" not in page
