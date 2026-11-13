"""Render one session as a single self-contained HTML page.

No JavaScript, no CDN, no build step: the charts are SVG written by Python
and the file opens offline. That keeps the report shareable by email, usable
on a plane, and cheap to publish from CI, at the cost of interactivity it
does not need.

Everything on the page comes from the warehouse, so a number here and a
number in a query cannot disagree.
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
from pathlib import Path

import duckdb

LOGGER = logging.getLogger("f1platform.report")

DEFAULT_DB_PATH = Path("data/warehouse.duckdb")
DEFAULT_OUTPUT_DIR = Path("reports")

# Formula 1's own compound colours, so the chart reads the way television does.
COMPOUND_COLOURS = {
    "SOFT": "#e8002d",
    "MEDIUM": "#f5c518",
    "HARD": "#8d8d8d",
    "INTERMEDIATE": "#2ea84f",
    "WET": "#0067ad",
}
UNKNOWN_COLOUR = "#b0b0b0"

CHART_WIDTH = 920
CHART_HEIGHT = 380
PAD_LEFT = 62
PAD_RIGHT = 20
PAD_TOP = 18
PAD_BOTTOM = 42


def esc(value: object) -> str:
    return html.escape(str(value))


def _scale(value: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    if hi == lo:
        return (out_lo + out_hi) / 2
    return out_lo + (value - lo) / (hi - lo) * (out_hi - out_lo)


def lap_time_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """Every representative lap, coloured by compound.

    One dot per lap makes the race's shape visible at a glance: a drying
    track shows as the whole field sliding downward, and a tyre change shows
    as the colour of a driver's dots switching mid-race.
    """
    rows = connection.execute(
        """
        SELECT lap_number, lap_time_seconds, compound
        FROM gold_representative_laps
        WHERE is_representative
        ORDER BY lap_number
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>No representative laps in this session.</p>"

    times = sorted(row[1] for row in rows)
    # Clip the scale to the middle 99% so one slow lap cannot flatten the rest.
    low = times[int(len(times) * 0.005)]
    high = times[int(len(times) * 0.995) - 1]
    span = high - low
    low, high = low - span * 0.05, high + span * 0.05

    laps = [row[0] for row in rows]
    first_lap, last_lap = min(laps), max(laps)

    plot_x0, plot_x1 = PAD_LEFT, CHART_WIDTH - PAD_RIGHT
    plot_y0, plot_y1 = PAD_TOP, CHART_HEIGHT - PAD_BOTTOM

    parts = [
        f"<svg viewBox='0 0 {CHART_WIDTH} {CHART_HEIGHT}' role='img' "
        "aria-label='Lap times through the race, coloured by tyre compound'>"
    ]

    # Horizontal grid with the time axis.
    steps = 5
    for index in range(steps + 1):
        value = low + (high - low) * index / steps
        y = _scale(value, low, high, plot_y1, plot_y0)
        parts.append(
            f"<line x1='{plot_x0}' x2='{plot_x1}' y1='{y:.1f}' y2='{y:.1f}' "
            "stroke='#e6e6e6'/>"
        )
        minutes, seconds = divmod(value, 60)
        parts.append(
            f"<text x='{plot_x0 - 8}' y='{y + 4:.1f}' text-anchor='end' "
            f"class='tick'>{int(minutes)}:{seconds:05.2f}</text>"
        )

    # Lap-number axis.
    for lap in range(first_lap, last_lap + 1, 10):
        x = _scale(lap, first_lap, last_lap, plot_x0, plot_x1)
        parts.append(
            f"<text x='{x:.1f}' y='{plot_y1 + 24}' text-anchor='middle' "
            f"class='tick'>{lap}</text>"
        )
    parts.append(
        f"<text x='{(plot_x0 + plot_x1) / 2:.0f}' y='{CHART_HEIGHT - 6}' "
        "text-anchor='middle' class='axis-title'>Lap</text>"
    )

    for lap_number, seconds, compound in rows:
        if not low <= seconds <= high:
            continue
        x = _scale(lap_number, first_lap, last_lap, plot_x0, plot_x1)
        y = _scale(seconds, low, high, plot_y1, plot_y0)
        colour = COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)
        parts.append(
            f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.1' fill='{colour}' "
            "fill-opacity='0.75'/>"
        )

    parts.append("</svg>")
    return "".join(parts)


def compound_legend(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT compound, count(*) AS laps
        FROM gold_representative_laps
        WHERE is_representative
        GROUP BY compound
        ORDER BY laps DESC
        """
    ).fetchall()
    items = [
        f"<span class='key'><i style='background:"
        f"{COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)}'></i>"
        f"{esc(compound.title())} <b>{laps:,}</b></span>"
        for compound, laps in rows
    ]
    return f"<div class='legend'>{''.join(items)}</div>"


def teammate_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """Diverging bars: how each driver compares to the one in the same car."""
    rows = connection.execute(
        """
        SELECT driver_code, teammate_code, delta_seconds, is_comparable, team_name
        FROM gold_teammate_delta
        WHERE delta_seconds < 0
        ORDER BY delta_seconds
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>No teammate pairs in this session.</p>"

    comparable = [row for row in rows if row[3]]
    widest = max((abs(row[2]) for row in comparable), default=1.0)

    bar_height, gap = 22, 8
    height = len(rows) * (bar_height + gap) + 24
    centre = 360

    parts = [
        f"<svg viewBox='0 0 {CHART_WIDTH} {height}' role='img' "
        "aria-label='Median pace gap between each pair of teammates'>"
    ]
    for index, (driver, teammate, delta, is_comparable, team) in enumerate(rows):
        y = index * (bar_height + gap) + 12
        magnitude = min(abs(delta), widest)
        width = magnitude / widest * 420 if widest else 0
        colour = "#1f6feb" if is_comparable else "#c9ccd1"
        parts.append(
            f"<text x='{centre - 10}' y='{y + 15}' text-anchor='end' "
            f"class='bar-label'>{esc(driver)} <tspan class='muted'>vs "
            f"{esc(teammate)}</tspan></text>"
        )
        parts.append(
            f"<rect x='{centre}' y='{y}' width='{width:.1f}' height='{bar_height}' "
            f"rx='3' fill='{colour}'/>"
        )
        note = "" if is_comparable else " · not comparable"
        parts.append(
            f"<text x='{centre + width + 8:.1f}' y='{y + 15}' class='bar-value'>"
            f"{abs(delta):.3f}s{esc(note)} <tspan class='muted'>{esc(team)}</tspan>"
            "</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def classification_table(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT classified_position, driver_code, driver_name, team_name,
               status, points, laps_completed, gap_to_winner_seconds,
               race_duration_seconds, is_classified
        FROM gold_race_classification
        ORDER BY finishing_order
        """
    ).fetchall()

    body = []
    for (position, code, name, team, status, points, laps, gap,
         duration, is_classified) in rows:
        if duration is not None:
            minutes, seconds = divmod(duration, 60)
            hours, minutes = divmod(int(minutes), 60)
            time_cell = f"{hours}:{minutes:02d}:{seconds:06.3f}"
        elif gap is not None:
            time_cell = f"+{gap:.3f}"
        else:
            time_cell = "—"
        row_class = "" if is_classified else " class='unclassified'"
        body.append(
            f"<tr{row_class}><td class='pos'>{esc(position)}</td>"
            f"<td><b>{esc(code)}</b> {esc(name)}</td><td>{esc(team)}</td>"
            f"<td>{esc(status)}</td><td class='num'>{int(laps)}</td>"
            f"<td class='num'>{time_cell}</td>"
            f"<td class='num'>{points:g}</td></tr>"
        )

    return (
        "<table><thead><tr><th>Pos</th><th>Driver</th><th>Team</th>"
        "<th>Status</th><th>Laps</th><th>Time / gap</th><th>Pts</th></tr>"
        f"</thead><tbody>{''.join(body)}</tbody></table>"
    )


def headline(connection: duckdb.DuckDBPyConnection, slug: str) -> str:
    total, representative = connection.execute(
        "SELECT count(*), sum(is_representative::int) FROM gold_representative_laps"
    ).fetchone()
    wet_stints, stints = connection.execute(
        "SELECT sum(is_wet_compound::int), count(*) FROM gold_stint_pace"
    ).fetchone()
    fastest = connection.execute(
        "SELECT driver_code, lap_time_seconds FROM gold_fastest_lap"
    ).fetchone()

    cards = [
        ("Laps recorded", f"{total:,}"),
        ("Usable for pace", f"{representative:,}"),
        ("Stints on wet tyres", f"{wet_stints} of {stints}"),
    ]
    if fastest:
        minutes, seconds = divmod(fastest[1], 60)
        cards.append(("Fastest lap", f"{fastest[0]} {int(minutes)}:{seconds:06.3f}"))

    items = "".join(
        f"<div class='card'><span class='card-value'>{esc(value)}</span>"
        f"<span class='card-label'>{esc(label)}</span></div>"
        for label, value in cards
    )
    return f"<div class='cards'>{items}</div>"


STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 24px 64px; background: #fbfbfa; color: #1a1a19;
       font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif; }
main { max-width: 980px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.015em; }
h2 { font-size: 17px; margin: 40px 0 6px; letter-spacing: -0.01em; }
p.sub { margin: 0; color: #6b6b68; }
p.note { margin: 4px 0 14px; color: #6b6b68; font-size: 13.5px; max-width: 72ch; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 8px; }
.card { flex: 1 1 150px; background: #fff; border: 1px solid #e8e8e4;
        border-radius: 10px; padding: 12px 14px; }
.card-value { display: block; font-size: 20px; font-weight: 650; }
.card-label { display: block; color: #6b6b68; font-size: 12.5px; margin-top: 2px; }
svg { width: 100%; height: auto; background: #fff; border: 1px solid #e8e8e4;
      border-radius: 10px; }
.tick { font-size: 11px; fill: #85857f; }
.axis-title { font-size: 12px; fill: #6b6b68; }
.bar-label { font-size: 12.5px; fill: #1a1a19; }
.bar-value { font-size: 12px; fill: #45453f; }
.muted { fill: #9a9a93; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin: 8px 2px 0;
          font-size: 13px; color: #45453f; }
.key i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
         margin-right: 6px; vertical-align: -1px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13.5px;
        background: #fff; border: 1px solid #e8e8e4; border-radius: 10px;
        overflow: hidden; }
th { text-align: left; font-weight: 600; font-size: 12px; text-transform: uppercase;
     letter-spacing: 0.04em; color: #6b6b68; padding: 9px 12px;
     border-bottom: 1px solid #e8e8e4; }
td { padding: 8px 12px; border-bottom: 1px solid #f1f1ed; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.pos { font-weight: 650; width: 44px; }
tr.unclassified td { color: #9a9a93; }
footer { margin-top: 46px; padding-top: 16px; border-top: 1px solid #e8e8e4;
         color: #85857f; font-size: 12.5px; }
.empty { color: #85857f; font-style: italic; }
"""


def render(connection: duckdb.DuckDBPyConnection, slug: str) -> str:
    title = slug.replace("-", " ").title()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>{esc(title)}</h1>
<p class="sub">Built from raw timing data. Every figure comes from the warehouse.</p>
{headline(connection, slug)}

<h2>Lap times through the race</h2>
<p class="note">One dot per lap, coloured by tyre compound. Laps under a yellow
flag, deleted laps, in-laps and out-laps are excluded, because none of them can
be compared to a clean lap.</p>
{lap_time_chart(connection)}
{compound_legend(connection)}

<h2>Teammate pace gap</h2>
<p class="note">The median gap to the driver in the same car — the only
comparison in Formula 1 that holds the machinery constant. Greyed bars are
flagged not comparable: one driver ran far fewer laps than the other, so the
gap measures two different races rather than two drivers.</p>
{teammate_chart(connection)}

<h2>Classification</h2>
<p class="note">Rebuilt from the raw data and verified against the FIA's
published result. Greyed rows were not classified.</p>
{classification_table(connection)}

<footer>Unofficial. Not affiliated with Formula 1. Generated by
f1-live-data-platform.</footer>
</main>
</body>
</html>
"""


def build_report(
    session_slug: str,
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} is missing -- run f1-build first")

    connection = duckdb.connect(str(db_path), read_only=True)
    try:
        page = render(connection, session_slug)
    finally:
        connection.close()

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{session_slug}.html"
    output_path.write_text(page, encoding="utf-8")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-report",
        description="Render a session as a self-contained HTML page.",
    )
    parser.add_argument("--session", required=True, help="session slug")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        path = build_report(args.session, args.db, args.output_dir)
    except FileNotFoundError as error:
        LOGGER.error("%s", error)
        return 1

    LOGGER.info("report written to %s (%.0f KB)", path, path.stat().st_size / 1024)
    return 0


if __name__ == "__main__":
    sys.exit(main())
