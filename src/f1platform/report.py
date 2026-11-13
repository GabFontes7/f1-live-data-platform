"""Render one session as a single self-contained HTML page with tabs.

No JavaScript, no CDN, no build step. The tabs are CSS — radio inputs and
sibling selectors — and the charts are SVG written by Python, so the file
opens offline, prints, and survives being emailed.

The page shows everything the warehouse holds rather than a curated subset.
Deciding what to leave out before seeing it is how a column ends up hidden
because it looked awkward, which is a worse failure than a crowded page.

Every figure is read from the warehouse, so a number here and a number in a
query cannot disagree.
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

# Formula 1's own compound colours, so a chart reads the way television does.
COMPOUND_COLOURS = {
    "SOFT": "#e8002d",
    "MEDIUM": "#f5c518",
    "HARD": "#8d8d8d",
    "INTERMEDIATE": "#2ea84f",
    "WET": "#0067ad",
}
UNKNOWN_COLOUR = "#b0b0b0"

WIDTH = 940
PAD_L, PAD_R, PAD_T, PAD_B = 64, 150, 16, 40


def esc(value: object) -> str:
    return html.escape(str(value))


def scale(value: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    if hi == lo:
        return (out_lo + out_hi) / 2
    return out_lo + (value - lo) / (hi - lo) * (out_hi - out_lo)


def fmt_lap(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:06.3f}"


def table_of(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    scroll: bool = True,
    numeric_from: int = 0,
) -> str:
    """Render a query's full result. No truncation: the point is completeness."""
    cursor = connection.execute(sql)
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    if not rows:
        return "<p class='empty'>Nothing in this table for this session.</p>"

    head = "".join(f"<th>{esc(column)}</th>" for column in columns)
    body = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            if value is None:
                cells.append("<td class='null'>—</td>")
            elif isinstance(value, float):
                cells.append(f"<td class='num'>{value:,.3f}</td>")
            elif isinstance(value, bool):
                mark = "✓" if value else "·"
                cells.append(f"<td class='num flag'>{mark}</td>")
            elif isinstance(value, int) and index >= numeric_from:
                cells.append(f"<td class='num'>{value:,}</td>")
            else:
                cells.append(f"<td>{esc(value)}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")

    klass = "scroller" if scroll else ""
    return (
        f"<div class='{klass}'><table class='data'><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
        f"<p class='count'>{len(rows):,} rows &times; {len(columns)} columns</p>"
    )


def has_table(connection: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Weather and race control are optional: a session can simply lack them."""
    return bool(
        connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [name],
        ).fetchone()[0]
    )


def team_colours(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    return {
        code: f"#{colour}"
        for code, colour in connection.execute(
            "SELECT driver_code, team_colour FROM silver_results"
        ).fetchall()
    }


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------


def lap_time_chart(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT lap_number, lap_time_seconds, compound
        FROM gold_representative_laps
        WHERE is_representative
        ORDER BY lap_number
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>No representative laps.</p>"

    height = 400
    times = sorted(row[1] for row in rows)
    low = times[int(len(times) * 0.005)]
    high = times[int(len(times) * 0.995) - 1]
    pad = (high - low) * 0.05
    low, high = low - pad, high + pad
    laps = [row[0] for row in rows]
    first, last = min(laps), max(laps)
    x0, x1 = PAD_L, WIDTH - PAD_R
    y0, y1 = PAD_T, height - PAD_B

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Lap times through the race by tyre compound'>"]
    for index in range(6):
        value = low + (high - low) * index / 5
        y = scale(value, low, high, y1, y0)
        out.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' "
                   "stroke='#ececea'/>")
        out.append(f"<text x='{x0 - 8}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='tick'>{fmt_lap(value)}</text>")
    for lap in range(first, last + 1, 5):
        x = scale(lap, first, last, x0, x1)
        out.append(f"<text x='{x:.1f}' y='{y1 + 22}' text-anchor='middle' "
                   f"class='tick'>{lap}</text>")
    for lap_number, seconds, compound in rows:
        if not low <= seconds <= high:
            continue
        out.append(
            f"<circle cx='{scale(lap_number, first, last, x0, x1):.1f}' "
            f"cy='{scale(seconds, low, high, y1, y0):.1f}' r='2.2' "
            f"fill='{COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)}' "
            "fill-opacity='0.72'/>"
        )
    out.append("</svg>")
    return "".join(out)


def position_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """One line per driver, position 1 at the top: the race's shape."""
    rows = connection.execute(
        """
        SELECT driver_code, lap_number, position
        FROM silver_laps
        WHERE position IS NOT NULL
        ORDER BY driver_code, lap_number
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>No position data.</p>"

    colours = team_colours(connection)
    by_driver: dict[str, list[tuple[int, int]]] = {}
    for code, lap, position in rows:
        by_driver.setdefault(code, []).append((lap, position))

    laps = [row[1] for row in rows]
    positions = [row[2] for row in rows]
    first, last = min(laps), max(laps)
    top, bottom = min(positions), max(positions)
    height = 520
    x0, x1 = PAD_L, WIDTH - PAD_R
    y0, y1 = PAD_T + 6, height - PAD_B

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Position of every driver on every lap'>"]
    for position in range(top, bottom + 1, 2):
        y = scale(position, top, bottom, y0, y1)
        out.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' "
                   "stroke='#f1f1ef'/>")
        out.append(f"<text x='{x0 - 8}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='tick'>P{position}</text>")
    for lap in range(first, last + 1, 5):
        x = scale(lap, first, last, x0, x1)
        out.append(f"<text x='{x:.1f}' y='{y1 + 22}' text-anchor='middle' "
                   f"class='tick'>{lap}</text>")

    for code, points in by_driver.items():
        path = " ".join(
            f"{'M' if index == 0 else 'L'}{scale(lap, first, last, x0, x1):.1f} "
            f"{scale(position, top, bottom, y0, y1):.1f}"
            for index, (lap, position) in enumerate(points)
        )
        colour = colours.get(code, UNKNOWN_COLOUR)
        out.append(f"<path d='{path}' fill='none' stroke='{colour}' "
                   "stroke-width='1.7' stroke-opacity='0.85'/>")
        final_lap, final_position = points[-1]
        out.append(
            f"<text x='{scale(final_lap, first, last, x0, x1) + 7:.1f}' "
            f"y='{scale(final_position, top, bottom, y0, y1) + 4:.1f}' "
            f"class='series-label' fill='{colour}'>{esc(code)}</text>"
        )
    out.append("</svg>")
    return "".join(out)


def stint_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """A bar per stint, coloured by compound: the strategy of the whole field."""
    rows = connection.execute(
        """
        SELECT driver_code, stint, any_value(compound) AS compound,
               min(lap_number) AS first_lap, max(lap_number) AS last_lap
        FROM silver_laps
        GROUP BY driver_code, stint
        ORDER BY driver_code, stint
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>No stint data.</p>"

    order = [
        row[0]
        for row in connection.execute(
            "SELECT driver_code FROM gold_race_classification ORDER BY finishing_order"
        ).fetchall()
    ]
    index_of = {code: index for index, code in enumerate(order)}
    last_lap = max(row[4] for row in rows)
    row_height, gap = 17, 4
    height = len(order) * (row_height + gap) + PAD_T + PAD_B
    x0, x1 = PAD_L, WIDTH - PAD_R

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Tyre compound for every stint of every driver'>"]
    for code, index in index_of.items():
        y = PAD_T + index * (row_height + gap)
        out.append(f"<text x='{x0 - 10}' y='{y + 12}' text-anchor='end' "
                   f"class='series-label'>{esc(code)}</text>")
    for code, _stint, compound, first, last in rows:
        if code not in index_of:
            continue
        y = PAD_T + index_of[code] * (row_height + gap)
        bar_x = scale(first, 1, last_lap, x0, x1)
        bar_w = max(scale(last, 1, last_lap, x0, x1) - bar_x, 2)
        out.append(
            f"<rect x='{bar_x:.1f}' y='{y}' width='{bar_w:.1f}' "
            f"height='{row_height}' rx='2' "
            f"fill='{COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)}' "
            "fill-opacity='0.85'/>"
        )
    for lap in range(1, last_lap + 1, 5):
        x = scale(lap, 1, last_lap, x0, x1)
        out.append(f"<text x='{x:.1f}' y='{height - 14}' text-anchor='middle' "
                   f"class='tick'>{lap}</text>")
    out.append("</svg>")
    return "".join(out)


def weather_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """Track and air temperature, with rain marked where it fell."""
    if not has_table(connection, "silver_weather"):
        return "<p class='empty'>This session carries no weather data.</p>"
    rows = connection.execute(
        """
        SELECT session_time_seconds / 60 AS minute, track_temp_c, air_temp_c,
               is_raining, track_temp_sensor_failed
        FROM silver_weather
        ORDER BY session_time_seconds
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>No weather data.</p>"

    race = connection.execute(
        "SELECT min(session_time_seconds) / 60, max(session_time_seconds) / 60 "
        "FROM silver_laps WHERE session_time_seconds IS NOT NULL"
    ).fetchone()

    height = 360
    minutes = [row[0] for row in rows]
    values = [v for row in rows for v in (row[1], row[2]) if v is not None]
    low, high = min(values) - 2, max(values) + 2
    first, last = min(minutes), max(minutes)
    x0, x1 = PAD_L, WIDTH - PAD_R
    y0, y1 = PAD_T, height - PAD_B

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Track and air temperature through the session'>"]

    if race and race[0] is not None:
        rx0 = scale(race[0], first, last, x0, x1)
        rx1 = scale(race[1], first, last, x0, x1)
        out.append(f"<rect x='{rx0:.1f}' y='{y0}' width='{rx1 - rx0:.1f}' "
                   f"height='{y1 - y0}' fill='#f3f6fb'/>")
        out.append(f"<text x='{(rx0 + rx1) / 2:.1f}' y='{y0 + 14}' "
                   "text-anchor='middle' class='tick'>race</text>")

    for index in range(6):
        value = low + (high - low) * index / 5
        y = scale(value, low, high, y1, y0)
        out.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' "
                   "stroke='#ececea'/>")
        out.append(f"<text x='{x0 - 8}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='tick'>{value:.0f}°C</text>")

    for row in rows:
        if not row[3]:
            continue
        x = scale(row[0], first, last, x0, x1)
        out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{y0}' y2='{y1}' "
                   "stroke='#0067ad' stroke-width='2.5' stroke-opacity='0.3'/>")

    for column, colour, label in ((1, "#c2410c", "track"), (2, "#2563eb", "air")):
        path_parts, pen_down = [], False
        for row in rows:
            value = row[column]
            if value is None:
                pen_down = False
                continue
            x = scale(row[0], first, last, x0, x1)
            y = scale(value, low, high, y1, y0)
            path_parts.append(f"{'L' if pen_down else 'M'}{x:.1f} {y:.1f}")
            pen_down = True
        out.append(f"<path d='{' '.join(path_parts)}' fill='none' "
                   f"stroke='{colour}' stroke-width='1.8'/>")
        final = next(row[column] for row in reversed(rows) if row[column] is not None)
        out.append(f"<text x='{x1 + 8}' y='{scale(final, low, high, y1, y0) + 4:.1f}' "
                   f"class='series-label' fill='{colour}'>{label}</text>")

    for row in rows:
        if row[4]:
            x = scale(row[0], first, last, x0, x1)
            out.append(f"<circle cx='{x:.1f}' cy='{y1 - 6}' r='4' fill='none' "
                       "stroke='#b91c1c' stroke-width='1.6'/>")

    for minute in range(0, int(last) + 1, 20):
        x = scale(minute, first, last, x0, x1)
        out.append(f"<text x='{x:.1f}' y='{y1 + 22}' text-anchor='middle' "
                   f"class='tick'>{minute}'</text>")
    out.append("</svg>")
    return "".join(out)


def speed_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """Speed trap on the longest straight, per driver, as a range."""
    rows = connection.execute(
        """
        SELECT driver_code,
               min(speed_trap_kph) AS low,
               median(speed_trap_kph) AS mid,
               max(speed_trap_kph) AS high
        FROM silver_laps
        WHERE speed_trap_kph IS NOT NULL AND NOT is_in_lap AND NOT is_out_lap
        GROUP BY driver_code
        ORDER BY mid DESC
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>No speed trap data.</p>"

    colours = team_colours(connection)
    low = min(row[1] for row in rows) - 4
    high = max(row[3] for row in rows) + 4
    row_height, gap = 18, 6
    height = len(rows) * (row_height + gap) + PAD_T + PAD_B
    x0, x1 = PAD_L + 10, WIDTH - PAD_R

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Speed trap range for each driver'>"]
    for index, (code, lo, mid, hi) in enumerate(rows):
        y = PAD_T + index * (row_height + gap) + row_height / 2
        colour = colours.get(code, UNKNOWN_COLOUR)
        out.append(f"<text x='{x0 - 12}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='series-label'>{esc(code)}</text>")
        out.append(f"<line x1='{scale(lo, low, high, x0, x1):.1f}' "
                   f"x2='{scale(hi, low, high, x0, x1):.1f}' "
                   f"y1='{y:.1f}' y2='{y:.1f}' stroke='{colour}' "
                   "stroke-width='2' stroke-opacity='0.35'/>")
        out.append(f"<circle cx='{scale(mid, low, high, x0, x1):.1f}' "
                   f"cy='{y:.1f}' r='4' fill='{colour}'/>")
        out.append(f"<text x='{x1 + 10}' y='{y + 4:.1f}' class='tick'>"
                   f"{mid:.0f} km/h</text>")
    out.append("</svg>")
    return "".join(out)


def compound_legend(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        "SELECT compound, count(*) FROM gold_representative_laps "
        "WHERE is_representative GROUP BY compound ORDER BY 2 DESC"
    ).fetchall()
    items = "".join(
        f"<span class='key'><i style='background:"
        f"{COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)}'></i>"
        f"{esc(compound.title())} <b>{laps:,}</b></span>"
        for compound, laps in rows
    )
    return f"<div class='legend'>{items}</div>"


# --------------------------------------------------------------------------
# tabs
# --------------------------------------------------------------------------


def classification_table(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT classified_position, driver_code, driver_name, team_name, status,
               points, laps_completed, gap_to_winner_seconds,
               race_duration_seconds, is_classified, laps_down,
               time_behind_seconds, grid_position, positions_gained
        FROM gold_race_classification ORDER BY finishing_order
        """
    ).fetchall()
    colours = team_colours(connection)

    body = []
    for (position, code, name, team, status, points, laps, gap, duration,
         is_classified, laps_down, behind, grid, gained) in rows:
        if duration is not None:
            minutes, seconds = divmod(duration, 60)
            hours, minutes = divmod(int(minutes), 60)
            time_cell = f"{hours}:{minutes:02d}:{seconds:06.3f}"
        elif gap is not None:
            time_cell = f"+{gap:.3f}"
        elif laps_down:
            plural = "s" if laps_down > 1 else ""
            trailing = (f" <span class='muted-cell'>+{behind:.3f}</span>"
                        if behind is not None else "")
            time_cell = f"+{int(laps_down)} lap{plural}{trailing}"
        else:
            time_cell = "—"

        if gained is None:
            change = "—"
        elif gained > 0:
            change = f"<span class='up'>+{int(gained)}</span>"
        elif gained < 0:
            change = f"<span class='down'>{int(gained)}</span>"
        else:
            change = "0"

        klass = "" if is_classified else " class='unclassified'"
        swatch = (f"<i class='swatch' style='background:"
                  f"{colours.get(code, UNKNOWN_COLOUR)}'></i>")
        body.append(
            f"<tr{klass}><td class='pos'>{esc(position)}</td>"
            f"<td>{swatch}<b>{esc(code)}</b> {esc(name)}</td>"
            f"<td>{esc(team)}</td><td>{esc(status)}</td>"
            f"<td class='num'>{int(grid)}</td><td class='num'>{change}</td>"
            f"<td class='num'>{int(laps)}</td><td class='num'>{time_cell}</td>"
            f"<td class='num'>{points:g}</td></tr>"
        )

    return (
        "<table class='data'><thead><tr><th>Pos</th><th>Driver</th><th>Team</th>"
        "<th>Status</th><th>Grid</th><th>+/−</th><th>Laps</th>"
        "<th>Time / gap</th><th>Pts</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def summary_cards(connection: duckdb.DuckDBPyConnection) -> str:
    total, usable = connection.execute(
        "SELECT count(*), sum(is_representative::int) FROM gold_representative_laps"
    ).fetchone()
    wet, stints = connection.execute(
        "SELECT sum(is_wet_compound::int), count(*) FROM gold_stint_pace"
    ).fetchone()
    fastest = connection.execute(
        "SELECT driver_code, lap_time_seconds FROM gold_fastest_lap"
    ).fetchone()
    rain = (
        connection.execute(
            "SELECT sum(is_raining::int), count(*) FROM silver_weather"
        ).fetchone()
        if has_table(connection, "silver_weather")
        else None
    )
    messages = (
        connection.execute("SELECT count(*) FROM silver_race_control").fetchone()
        if has_table(connection, "silver_race_control")
        else None
    )

    cards = [
        ("Laps recorded", f"{total:,}"),
        ("Usable for pace", f"{usable:,}"),
        ("Stints on wet tyres", f"{wet} of {stints}"),
    ]
    if fastest:
        cards.append(("Fastest lap", f"{fastest[0]} {fmt_lap(fastest[1])}"))
    if rain and rain[0] is not None:
        cards.append(("Minutes with rain", f"{rain[0]} of {rain[1]}"))
    if messages:
        cards.append(("Race control messages", f"{messages[0]:,}"))

    return "<div class='cards'>" + "".join(
        f"<div class='card'><span class='card-value'>{esc(value)}</span>"
        f"<span class='card-label'>{esc(label)}</span></div>"
        for label, value in cards
    ) + "</div>"


# Tabs that only make sense when their source table was loaded. The body is
# built lazily, because building it runs the query: an eager f-string would
# hit a missing table before the filter below could drop the tab.
TAB_REQUIRES = {"weather": "silver_weather", "control": "silver_race_control"}


def tab_summary(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""{summary_cards(connection)}
<h2>Lap times through the race</h2>
<p class="note">One dot per lap, coloured by compound. Laps under a yellow flag,
deleted laps, in-laps and out-laps are excluded: none of them can be compared to
a clean lap.</p>
{lap_time_chart(connection)}
{compound_legend(connection)}
<h2>Classification</h2>
<p class="note">Rebuilt from raw timing and verified against the FIA's published
result. Greyed rows were not classified.</p>
{classification_table(connection)}"""


def tab_race(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>Position, lap by lap</h2>
<p class="note">One line per driver in team colours. FastF1 derives this
position by sorting each lap by the time it was set, so it is a reconstruction
rather than a figure the timing feed published.</p>
{position_chart(connection)}
<h2>Strategy</h2>
<p class="note">A bar per stint, coloured by compound. The whole field's
strategy in one picture.</p>
{stint_chart(connection)}
{compound_legend(connection)}"""


def tab_pace(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>Teammate gap</h2>
<p class="note">The only comparison that holds the car constant.
<code>is_comparable</code> is false when one driver ran far fewer laps, so the
gap measures two different races rather than two drivers.</p>
{table_of(connection, '''
    SELECT driver_code, team_name, teammate_code,
           round(delta_seconds, 3) AS delta_s,
           representative_laps, teammate_representative_laps,
           round(median_pace_seconds, 3) AS median_pace_s,
           is_comparable
    FROM gold_teammate_delta ORDER BY delta_seconds
''', scroll=False)}
<h2>Pace by stint</h2>
<p class="note"><code>lap_time_trend_s_per_lap</code> is the slope of lap time
against tyre age. It is not degradation: it also carries fuel burn and track
evolution, and <code>trend_is_negative</code> marks the stints where the tyre
was not the dominant term.</p>
{table_of(connection, '''
    SELECT driver_code, stint, compound, representative_laps,
           round(median_pace_seconds, 3) AS median_pace_s,
           round(consistency_seconds, 3) AS stddev_s,
           round(lap_time_trend_s_per_lap, 4) AS trend_s_per_lap,
           is_wet_compound, trend_is_negative
    FROM gold_stint_pace ORDER BY driver_code, stint
''')}
<h2>Sector times</h2>
<p class="note">Median sector times over representative laps.</p>
{table_of(connection, '''
    SELECT l.driver_code,
           round(median(l.sector1_seconds), 3) AS s1_s,
           round(median(l.sector2_seconds), 3) AS s2_s,
           round(median(l.sector3_seconds), 3) AS s3_s,
           count(*) AS laps
    FROM gold_representative_laps AS l
    WHERE l.is_representative
    GROUP BY l.driver_code ORDER BY s1_s + s2_s + s3_s
''', scroll=False)}"""


def tab_speed(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>Speed trap</h2>
<p class="warn"><b>Read this before using these numbers.</b> FastF1 forward-fills
speed trap values when the timing API skips repeated readings, so some of these
are the previous lap's value carried over rather than a measurement. And FastF1's
own documentation describes <code>SpeedST</code> as "maybe a speed trap on the
longest straight (?)" — they are not certain what it measures.</p>
{speed_chart(connection)}
<h2>All four traps</h2>
<p class="note">I1 and I2 sit in sectors 1 and 2, FL is the finish line, ST is
the longest straight. Median over laps that were neither in-laps nor out-laps.</p>
{table_of(connection, '''
    SELECT driver_code,
           round(median(speed_i1_kph), 1) AS i1_kph,
           round(median(speed_i2_kph), 1) AS i2_kph,
           round(median(speed_finish_kph), 1) AS finish_kph,
           round(median(speed_trap_kph), 1) AS trap_kph,
           count(*) AS laps
    FROM silver_laps
    WHERE NOT is_in_lap AND NOT is_out_lap
    GROUP BY driver_code ORDER BY trap_kph DESC
''', scroll=False)}"""


def tab_weather(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>Track and air temperature</h2>
<p class="note">Sampled once a minute across the whole session; the shaded band
is the race itself. Blue verticals mark minutes with rainfall. The red ring marks
a sample where the track sensor reported exactly 0&nbsp;°C, which at Interlagos
is a failure rather than a reading — it is stored as null and flagged.</p>
{weather_chart(connection)}
<h2>Every sample</h2>
{table_of(connection, '''
    SELECT round(session_time_seconds / 60, 1) AS minute,
           air_temp_c, track_temp_c, track_temp_sensor_failed,
           humidity_pct, pressure_mbar, is_raining,
           wind_speed_ms, wind_direction_deg
    FROM silver_weather ORDER BY session_time_seconds
''')}"""


def tab_control(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>Every message</h2>
<p class="note">Flags are track state; "Other" is the stewards talking. Penalties
<em>served during the race</em> appear here — penalties applied after the
chequered flag do not, because those live in the FIA's documents instead.</p>
{table_of(connection, '''
    SELECT lap_number, category, flag, scope, sector, driver_number,
           mentions_penalty, mentions_investigation, mentions_deleted_lap,
           message
    FROM silver_race_control ORDER BY lap_number, message_time
''')}"""


def tab_laps(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>All laps, all columns</h2>
<p class="note">The silver layer in full — nothing filtered, nothing hidden.
<code>is_reconstructed</code> marks laps FastF1 rebuilt rather than received.</p>
{table_of(connection, '''
    SELECT driver_code, lap_number, position, stint, compound, tyre_life,
           fresh_tyre, round(lap_time_seconds, 3) AS lap_time_s,
           round(sector1_seconds, 3) AS s1, round(sector2_seconds, 3) AS s2,
           round(sector3_seconds, 3) AS s3,
           speed_i1_kph, speed_i2_kph, speed_finish_kph, speed_trap_kph,
           track_status, is_personal_best, is_deleted, deleted_reason,
           is_accurate, is_reconstructed, is_in_lap, is_out_lap,
           round(session_time_seconds, 1) AS session_time_s
    FROM silver_laps ORDER BY lap_number, position
''')}"""


def tab_entries(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>Results, every column</h2>
{table_of(connection, '''
    SELECT driver_number, driver_code, driver_name, broadcast_name, driver_id,
           team_name, team_id, team_colour, country_code, grid_position,
           finishing_order, classified_position, status, points, laps_completed,
           round(time_seconds, 3) AS time_s, is_classified
    FROM silver_results ORDER BY finishing_order
''', scroll=False)}"""


TABS = (
    ("summary", "Summary", tab_summary),
    ("race", "Race shape", tab_race),
    ("pace", "Pace", tab_pace),
    ("speed", "Speed", tab_speed),
    ("weather", "Weather", tab_weather),
    ("control", "Race control", tab_control),
    ("laps", "Every lap", tab_laps),
    ("entries", "Entries", tab_entries),
)


def build_tabs(connection: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
    """Each tab: (slug, label, html). An optional tab is dropped, not emptied."""
    return [
        (slug, label, build(connection))
        for slug, label, build in TABS
        if slug not in TAB_REQUIRES or has_table(connection, TAB_REQUIRES[slug])
    ]


STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 20px 72px; background: #fbfbfa; color: #1a1a19;
       font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif; }
main { max-width: 1000px; margin: 0 auto; }
h1 { font-size: 25px; margin: 0 0 3px; letter-spacing: -0.015em; }
h2 { font-size: 16px; margin: 34px 0 5px; letter-spacing: -0.01em; }
p.sub { margin: 0 0 18px; color: #6b6b68; }
p.note { margin: 3px 0 13px; color: #6b6b68; font-size: 13.5px; max-width: 78ch; }
p.warn { margin: 3px 0 14px; padding: 11px 13px; background: #fff8e8;
         border: 1px solid #f0dfae; border-radius: 8px; font-size: 13.5px;
         color: #5c4a12; max-width: 86ch; }
code { background: #f1f1ed; padding: 1px 4px; border-radius: 3px; font-size: 92%; }
.cards { display: flex; flex-wrap: wrap; gap: 9px; margin: 4px 0 6px; }
.card { flex: 1 1 140px; background: #fff; border: 1px solid #e8e8e4;
        border-radius: 10px; padding: 11px 13px; }
.card-value { display: block; font-size: 19px; font-weight: 650; }
.card-label { display: block; color: #6b6b68; font-size: 12.5px; margin-top: 2px; }
svg { width: 100%; height: auto; background: #fff; border: 1px solid #e8e8e4;
      border-radius: 10px; }
.tick { font-size: 11px; fill: #85857f; }
.series-label { font-size: 11px; font-weight: 600; }
.legend { display: flex; gap: 15px; flex-wrap: wrap; margin: 7px 2px 0;
          font-size: 13px; color: #45453f; }
.key i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
         margin-right: 6px; vertical-align: -1px; }
i.swatch { display: inline-block; width: 3px; height: 13px; border-radius: 1px;
           margin-right: 7px; vertical-align: -2px; }
.scroller { max-height: 560px; overflow: auto; border: 1px solid #e8e8e4;
            border-radius: 10px; background: #fff; }
table.data { width: 100%; border-collapse: collapse; font-size: 12.5px;
             background: #fff; }
.scroller table.data { border: none; }
table.data:not(.scroller table) { border: 1px solid #e8e8e4; border-radius: 10px;
                                  overflow: hidden; }
th { position: sticky; top: 0; background: #fff; text-align: left;
     font-weight: 600; font-size: 11px; text-transform: uppercase;
     letter-spacing: 0.04em; color: #6b6b68; padding: 8px 10px;
     border-bottom: 1px solid #e0e0db; white-space: nowrap; }
td { padding: 6px 10px; border-bottom: 1px solid #f3f3ef; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.null { text-align: right; color: #c7c7c0; }
td.flag { color: #45453f; }
td.pos { font-weight: 650; width: 44px; }
tr.unclassified td { color: #9a9a93; }
.muted-cell { color: #9a9a93; font-size: 11.5px; }
.up { color: #15803d; } .down { color: #b91c1c; }
p.count { margin: 6px 2px 0; font-size: 12px; color: #9a9a93; }
footer { margin-top: 44px; padding-top: 15px; border-top: 1px solid #e8e8e4;
         color: #85857f; font-size: 12.5px; }
.empty { color: #85857f; font-style: italic; }
.tabs > input { position: absolute; opacity: 0; pointer-events: none; }
nav.tabbar { display: flex; flex-wrap: wrap; gap: 3px; margin: 2px 0 20px;
             border-bottom: 1px solid #e3e3df; }
nav.tabbar label { padding: 8px 13px; font-size: 13.5px; cursor: pointer;
                   color: #6b6b68; border-bottom: 2px solid transparent;
                   margin-bottom: -1px; user-select: none; }
nav.tabbar label:hover { color: #1a1a19; }
.panels > section { display: none; }
"""


def tab_css(slugs: list[str]) -> str:
    rules = []
    for slug in slugs:
        rules.append(
            f"#tab-{slug}:checked ~ nav.tabbar label[for='tab-{slug}'] "
            "{ color:#1a1a19; font-weight:600; border-bottom-color:#1a1a19; }"
        )
        rules.append(
            f"#tab-{slug}:checked ~ .panels > #panel-{slug} {{ display:block; }}"
        )
    return "\n".join(rules)


def render(connection: duckdb.DuckDBPyConnection, slug: str) -> str:
    tabs = build_tabs(connection)
    slugs = [tab[0] for tab in tabs]
    title = slug.replace("-", " ").title()

    radios = "".join(
        f"<input type='radio' name='tab' id='tab-{tab_slug}'"
        f"{' checked' if index == 0 else ''}>"
        for index, (tab_slug, _, _) in enumerate(tabs)
    )
    bar = "".join(
        f"<label for='tab-{tab_slug}'>{esc(label)}</label>"
        for tab_slug, label, _ in tabs
    )
    panels = "".join(
        f"<section id='panel-{tab_slug}'>{body}</section>"
        for tab_slug, _, body in tabs
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{STYLE}
{tab_css(slugs)}</style>
</head>
<body>
<main>
<h1>{esc(title)}</h1>
<p class="sub">Built from raw timing data. Every figure comes from the warehouse.</p>
<div class="tabs">{radios}<nav class="tabbar">{bar}</nav>
<div class="panels">{panels}</div></div>
<footer>Unofficial. Not affiliated with Formula 1. Generated by
f1-live-data-platform. No JavaScript: the tabs are CSS.</footer>
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
