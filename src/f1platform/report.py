"""Render one session as a single self-contained HTML page with tabs.

No CDN, no build step, no external asset: the tabs are CSS, the charts are SVG
written by Python, and the only script is a few inline lines that show a
tooltip and highlight one driver (see ADR 0004). The file opens offline and
survives being emailed.

The page is written in Portuguese, for the person who reads it. The raw-data
tab keeps the warehouse's own column names, so a figure on screen can be found
in a query without a dictionary.

Every figure is read from the warehouse, so a number here and a number in a
query cannot disagree.
"""

from __future__ import annotations

import argparse
import bisect
import html
import logging
import re
import sys
from datetime import timedelta
from pathlib import Path

import duckdb

LOGGER = logging.getLogger("f1platform.report")

DEFAULT_DB_PATH = Path("data/warehouse.duckdb")
DEFAULT_OUTPUT_DIR = Path("reports")


class SessionNotInWarehouseError(LookupError):
    """The warehouse exists but was never built for this session."""

# Formula 1's own compound colours, so a chart reads the way television does.
COMPOUND_COLOURS = {
    "SOFT": "#e8002d",
    "MEDIUM": "#f5c518",
    "HARD": "#8d8d8d",
    "INTERMEDIATE": "#2ea84f",
    "WET": "#0067ad",
}
COMPOUND_NAMES = {
    "SOFT": "Macio",
    "MEDIUM": "Médio",
    "HARD": "Duro",
    "INTERMEDIATE": "Intermediário",
    "WET": "Chuva forte",
}
STATUS_NAMES = {
    "Finished": "Terminou",
    "Lapped": "Retardatário",
    "Retired": "Abandonou",
    "Did not start": "Não largou",
    "Withdrew": "Retirou-se",
    "Disqualified": "Desclassificado",
}
SESSION_NAMES = {
    "r": "Corrida",
    "q": "Classificação",
    "s": "Sprint",
    "sq": "Classificação da Sprint",
    "fp1": "Treino Livre 1",
    "fp2": "Treino Livre 2",
    "fp3": "Treino Livre 3",
}
UNKNOWN_COLOUR = "#b0b0b0"

WIDTH = 940
PAD_L, PAD_R, PAD_T, PAD_B = 64, 150, 16, 40


def esc(value: object) -> str:
    return html.escape(str(value))


def tip(*lines: object) -> str:
    """A ``data-tip`` attribute: what the tooltip shows, one line per item."""
    text = "&#10;".join(esc(line) for line in lines if line not in (None, ""))
    return f" data-tip='{text}'"


def scale(value: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    if hi == lo:
        return (out_lo + out_hi) / 2
    return out_lo + (value - lo) / (hi - lo) * (out_hi - out_lo)


def fmt_lap(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:06.3f}"


def compound_name(compound: str | None) -> str:
    if compound is None:
        return "Desconhecido"
    return COMPOUND_NAMES.get(compound, compound.title())


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


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
        return "<p class='empty'>Nada nesta tabela para esta sessão.</p>"

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
        f"<p class='count'>{len(rows):,} linhas &times; {len(columns)} colunas</p>"
    )


def scope_to_session(connection: duckdb.DuckDBPyConnection, slug: str) -> str:
    """Point every table name at one session's rows, and say which tables.

    The warehouse holds many sessions. Rather than adding a filter to forty
    queries -- where one forgotten filter silently mixes two sessions -- each
    name becomes a temp view over that session alone.

    Every stored table carrying a `session_slug` is scoped, rather than a
    list kept by hand. A list is a thing to forget: the first version of this
    function had one, a qualifying table was added without it, and a page
    quietly rendered two qualifying sessions on top of each other.
    """
    catalog = connection.execute(
        "SELECT database_name FROM duckdb_databases() "
        "WHERE database_name NOT IN ('system', 'temp') LIMIT 1"
    ).fetchone()[0]
    tables = [
        row[0]
        for row in connection.execute(
            """
            SELECT t.table_name FROM duckdb_tables() AS t
            WHERE t.database_name = ? AND t.schema_name = 'main'
              AND EXISTS (
                  SELECT 1 FROM duckdb_columns() AS c
                  WHERE c.database_name = t.database_name
                    AND c.table_name = t.table_name
                    AND c.column_name = 'session_slug'
              )
            ORDER BY t.table_name
            """,
            [catalog],
        ).fetchall()
    ]
    # DuckDB will not take a bound parameter inside CREATE VIEW, so the slug
    # is quoted into the statement with its quotes doubled.
    literal = "'" + slug.replace("'", "''") + "'"
    for table in tables:
        connection.execute(
            f'CREATE OR REPLACE TEMP VIEW "{table}" AS SELECT * FROM '
            f'"{catalog}".main."{table}" WHERE session_slug = {literal}'
        )
    return catalog


def has_table(connection: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Whether this session has that table AND rows in it.

    Weather and race control are optional, and in a warehouse holding several
    sessions the table can exist because another session filled it.
    """
    present = connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
        [name],
    ).fetchone()[0]
    if not present:
        return False
    return bool(connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])


def team_colours(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    return {
        code: f"#{colour}"
        for code, colour in connection.execute(
            "SELECT driver_code, team_colour FROM silver_results"
        ).fetchall()
    }


# What a neutralisation looks like on the condition strip. Green draws
# nothing: an uneventful lap should leave the strip empty, not grey.
NEUTRALISATION = {
    "yellow": ("#e8c33a", "Bandeira amarela em algum setor"),
    "vsc": ("#d98324", "Safety car virtual"),
    "safety_car": ("#e8710a", "Safety car na pista"),
    "red": ("#b91c1c", "Bandeira vermelha"),
}


def session_profile(connection: duckdb.DuckDBPyConnection) -> dict:
    """One row describing what happened, or {} when it was not built."""
    if not has_table(connection, "gold_session_profile"):
        return {}
    cursor = connection.execute("SELECT * FROM gold_session_profile")
    row = cursor.fetchone()
    if row is None:
        return {}
    columns = [column[0] for column in cursor.description]
    return dict(zip(columns, row, strict=True))


def track_condition(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[list[tuple], list[str]]:
    """Per-lap condition, and which bands have anything to say.

    A band is drawn only when the session produced it. A dry race with no
    neutralisation gets no strip at all, rather than an empty one captioned
    with a phenomenon that did not happen.
    """
    if not has_table(connection, "gold_track_state"):
        return [], []
    rows = connection.execute(
        """
        SELECT lap_number, cars_on_track, cars_on_wet_tyres, wet_tyre_share,
               neutralisation
        FROM gold_track_state ORDER BY lap_number
        """
    ).fetchall()
    aid = (
        dict(connection.execute(
            "SELECT lap_number, aid_enabled FROM gold_overtake_aid"
        ).fetchall())
        if has_table(connection, "gold_overtake_aid")
        else {}
    )
    rows = [row + (aid.get(row[0]),) for row in rows]

    bands = []
    if any(row[3] for row in rows):
        bands.append("wet")
    if any(row[4] != "green" for row in rows):
        bands.append("neutralisation")
    # Only worth a band when the aid was taken away at some point: a race
    # where it was available throughout has nothing to show.
    if any(row[5] is False for row in rows):
        bands.append("overtake_aid")
    return rows, bands


def finishing_order(connection: duckdb.DuckDBPyConnection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT driver_code FROM gold_race_classification ORDER BY finishing_order"
        ).fetchall()
    ]


def heat_table(
    label_header: str,
    columns: list[str],
    rows: list[tuple[str, str, list[float | None]]],
    *,
    higher_is_better: bool,
    fmt,
    unit: str,
) -> str:
    """A table whose cells are shaded by distance from the best in the column.

    ``rows`` is (label, colour, values). The best cell shows the value itself
    in purple, as timing screens do; every other cell shows its gap to it.
    """
    if not rows:
        return "<p class='empty'>Sem dados.</p>"
    bests, worsts = [], []
    for index in range(len(columns)):
        present = [row[2][index] for row in rows if row[2][index] is not None]
        if not present:
            bests.append(None)
            worsts.append(None)
            continue
        best = max(present) if higher_is_better else min(present)
        gaps = sorted(abs(value - best) for value in present)
        bests.append(best)
        # Shade against the 90th percentile so one outlier does not flatten
        # everyone else into the same pale cell.
        worsts.append(gaps[max(int(len(gaps) * 0.9) - 1, 0)] or 1.0)

    head = "".join(f"<th class='num'>{esc(column)}</th>" for column in columns)
    body = []
    for label, colour, values in rows:
        cells = []
        for index, value in enumerate(values):
            if value is None or bests[index] is None:
                cells.append("<td class='null'>—</td>")
                continue
            gap = abs(value - bests[index])
            hover = tip(
                f"{label} · {columns[index]}",
                f"{fmt(value)} {unit}".strip(),
                f"Melhor da coluna: {fmt(bests[index])} {unit}".strip(),
            )
            if gap == 0:
                cells.append(f"<td class='num best'{hover}>{fmt(value)}</td>")
                continue
            alpha = 0.06 + 0.44 * min(gap / worsts[index], 1.0)
            sign = "−" if higher_is_better else "+"
            digits = 1 if higher_is_better else 3
            cells.append(
                f"<td class='num' style='background:rgba(220,60,40,{alpha:.2f})'"
                f"{hover}>{sign}{gap:.{digits}f}</td>"
            )
        swatch = f"<i class='swatch' style='background:{colour}'></i>"
        body.append(f"<tr><td>{swatch}<b>{esc(label)}</b></td>{''.join(cells)}</tr>")
    return (
        "<table class='data heat'><thead><tr>"
        f"<th>{esc(label_header)}</th>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def bar_list(items: list[tuple[str, str, float, str, str]]) -> str:
    """Horizontal bars: (label, colour, value, value_text, tip_attr)."""
    if not items:
        return "<p class='empty'>Nenhum registro.</p>"
    top = max(item[2] for item in items) or 1
    rows = "".join(
        f"<div class='bar-row'{hover}><span class='bar-label'>{esc(label)}</span>"
        f"<span class='bar-track'><i style='width:{value / top * 100:.1f}%;"
        f"background:{colour}'></i></span>"
        f"<span class='bar-value'>{esc(text)}</span></div>"
        for label, colour, value, text, hover in items
    )
    return f"<div class='bars'>{rows}</div>"


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------


def lap_time_chart(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT lap_number, lap_time_seconds, compound, driver_code, tyre_life,
               position
        FROM gold_representative_laps
        WHERE is_representative
        ORDER BY lap_number
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Nenhuma volta representativa.</p>"

    state, bands = track_condition(connection)
    strip = 30 + 26 * len(bands) if bands else 0
    height = 400 + strip
    times = sorted(row[1] for row in rows)
    low = times[int(len(times) * 0.005)]
    high = times[int(len(times) * 0.995) - 1]
    pad = (high - low) * 0.05
    low, high = low - pad, high + pad
    laps = [row[0] for row in rows]
    first, last = min(laps), max(laps)
    x0, x1 = PAD_L, WIDTH - PAD_R
    y0, y1 = PAD_T, height - PAD_B - strip

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' class='focusable' "
           "aria-label='Tempo de cada volta ao longo da corrida, por composto'>"]
    cell = (x1 - x0) / max(last - first, 1)
    for index, band in enumerate(bands):
        sy = y1 + 34 + index * 26
        icon, label = {
            "wet": ("💦", "pista molhada"),
            "neutralisation": ("🚩", "neutralizada"),
            "overtake_aid": ("🚫", "sem auxílio"),
        }[band]
        out.append(f"<text x='{x0 - 8}' y='{sy + 12}' text-anchor='end' "
                   f"class='tick'>{icon}</text>")
        out.append(f"<text x='{x1 + 10}' y='{sy + 12}' class='tick'>"
                   f"{esc(label)}</text>")
        out.append(f"<rect x='{x0}' y='{sy}' width='{x1 - x0}' height='16' "
                   "rx='3' fill='#f4f4f2'/>")
        for lap, cars, wet_cars, share, neutralisation, aid in state:
            if not first <= lap <= last:
                continue
            x = scale(lap, first, last, x0, x1) - cell / 2
            if band == "overtake_aid":
                if aid is not False:
                    continue
                colour, opacity = "#6d28d9", 0.85
                headline = "Auxílio de ultrapassagem bloqueado"
                detail = "A direção de prova desativou o recurso nesta volta"
            elif band == "wet":
                if not share:
                    continue
                colour, opacity = "#0067ad", 0.12 + share * 0.78
                detail = (f"{wet_cars} de {cars} carros em pneu intermediário "
                          f"ou de chuva ({share * 100:.0f}%)")
                headline = ("Pista molhada" if share >= 0.5
                            else "Pista secando ou começando a molhar")
            else:
                if neutralisation == "green":
                    continue
                colour, opacity = NEUTRALISATION[neutralisation][0], 0.9
                headline = NEUTRALISATION[neutralisation][1]
                detail = None
            hover = tip(f"Volta {lap}", headline, detail)
            out.append(
                f"<rect x='{x:.1f}' y='{sy}' width='{cell + 0.3:.1f}' "
                f"height='16' fill='{colour}' fill-opacity='{opacity:.2f}' "
                f"class='column'{hover}/>"
            )
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
    out.append(f"<text x='{x1 + 10}' y='{y1 + 22}' class='tick'>volta</text>")
    for lap_number, seconds, compound, code, tyre_life, position in rows:
        if not low <= seconds <= high:
            continue
        age = (f" · pneu com {plural(int(tyre_life), 'volta', 'voltas')}"
               if tyre_life is not None else "")
        place = f" · P{int(position)}" if position is not None else ""
        hover = tip(
            f"{code} · volta {lap_number}{place}",
            fmt_lap(seconds),
            f"{compound_name(compound)}{age}",
        )
        out.append(
            f"<circle cx='{scale(lap_number, first, last, x0, x1):.1f}' "
            f"cy='{scale(seconds, low, high, y1, y0):.1f}' r='2.6' "
            f"fill='{COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)}' "
            f"fill-opacity='0.72' data-d='{esc(code)}'{hover}/>"
        )
    out.append("</svg>")
    return "".join(out)


def position_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """One line per driver, position 1 at the top: the race's shape."""
    rows = connection.execute(
        """
        SELECT driver_code, lap_number, position, compound, tyre_life,
               lap_time_seconds, is_in_lap
        FROM silver_laps
        WHERE position IS NOT NULL
        ORDER BY driver_code, lap_number
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Sem dados de posição.</p>"

    colours = team_colours(connection)
    by_driver: dict[str, list[tuple]] = {}
    for row in rows:
        by_driver.setdefault(row[0], []).append(row[1:])

    # Lap 0 is the grid. The timing feed's first position is the END of lap
    # one, which hides the start -- often the biggest move of the race.
    # A grid position of 0 means a pit-lane start: there is no slot to draw.
    grid = {
        code: int(position)
        for code, position in connection.execute(
            "SELECT driver_code, grid_position FROM silver_results "
            "WHERE grid_position IS NOT NULL AND grid_position > 0"
        ).fetchall()
        if code in by_driver
    }

    laps = [row[1] for row in rows] + ([0] if grid else [])
    positions = [row[2] for row in rows] + list(grid.values())
    first, last = min(laps), max(laps)
    top, bottom = min(positions), max(positions)
    height = 540
    x0, x1 = PAD_L, WIDTH - PAD_R
    y0, y1 = PAD_T + 6, height - PAD_B

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' class='focusable' "
           "aria-label='Posição de cada piloto em cada volta'>"]
    for position in range(int(top), int(bottom) + 1):
        y = scale(position, top, bottom, y0, y1)
        out.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' "
                   "stroke='#f4f4f2'/>")
        out.append(f"<text x='{x0 - 8}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='tick'>P{position}</text>")
    for lap in range(first, last + 1, 5):
        x = scale(lap, first, last, x0, x1)
        label = "largada" if lap == 0 else lap
        out.append(f"<text x='{x:.1f}' y='{y1 + 22}' text-anchor='middle' "
                   f"class='tick'>{label}</text>")
    out.append(f"<text x='{(x0 + x1) / 2:.0f}' y='{height - 2}' "
               "text-anchor='middle' class='tick'>volta</text>")

    hits = []
    for code, points in by_driver.items():
        colour = colours.get(code, UNKNOWN_COLOUR)
        trace = [(point[0], point[1]) for point in points]
        if code in grid:
            trace.insert(0, (0, grid[code]))
            gx = scale(0, first, last, x0, x1)
            gy = scale(grid[code], top, bottom, y0, y1)
            after = (f"Fim da volta 1: P{int(points[0][1])}"
                     if points[0][0] == 1 else None)
            hover = tip(f"{code} · largada", f"P{grid[code]} no grid", after)
            out.append(f"<rect x='{gx - 3.5:.1f}' y='{gy - 3.5:.1f}' width='7' "
                       f"height='7' fill='{colour}' data-d='{esc(code)}'/>")
            hits.append(f"<circle cx='{gx:.1f}' cy='{gy:.1f}' r='6' "
                        f"fill='transparent' data-d='{esc(code)}'{hover}/>")
        path = " ".join(
            f"{'M' if index == 0 else 'L'}{scale(lap, first, last, x0, x1):.1f} "
            f"{scale(position, top, bottom, y0, y1):.1f}"
            for index, (lap, position) in enumerate(trace)
        )
        out.append(f"<path d='{path}' fill='none' stroke='{colour}' "
                   f"stroke-width='1.8' stroke-opacity='0.85' data-d='{esc(code)}'/>")
        for lap, position, compound, tyre_life, seconds, is_in_lap in points:
            x = scale(lap, first, last, x0, x1)
            y = scale(position, top, bottom, y0, y1)
            if is_in_lap:
                out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3' fill='#fff' "
                           f"stroke='{colour}' stroke-width='1.5' "
                           f"data-d='{esc(code)}'/>")
            age = (f" · {plural(int(tyre_life), 'volta', 'voltas')} de uso"
                   if tyre_life is not None else "")
            hover = tip(
                f"{code} · volta {lap}",
                f"P{int(position)}" + (" · entrou no box" if is_in_lap else ""),
                f"{compound_name(compound)}{age}",
                f"Tempo: {fmt_lap(seconds)}" if seconds is not None else None,
            )
            hits.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='5' "
                        f"fill='transparent' data-d='{esc(code)}'{hover}/>")
        final = points[-1]
        out.append(
            f"<text x='{scale(final[0], first, last, x0, x1) + 7:.1f}' "
            f"y='{scale(final[1], top, bottom, y0, y1) + 4:.1f}' "
            f"class='series-label' fill='{colour}' data-d='{esc(code)}'"
            f"{tip(code, 'Passe o mouse para destacar a corrida deste piloto')}>"
            f"{esc(code)}</text>"
        )
    out.extend(hits)
    out.append("</svg>")
    return "".join(out)


def stint_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """A bar per stint, coloured by compound: the strategy of the whole field."""
    rows = connection.execute(
        """
        SELECT l.driver_code, l.stint, any_value(l.compound) AS compound,
               min(l.lap_number) AS first_lap, max(l.lap_number) AS last_lap,
               count(*) AS laps, bool_or(l.fresh_tyre) AS fresh,
               min(l.tyre_life) AS life_at_start,
               any_value(p.median_pace_seconds) AS pace,
               any_value(p.representative_laps) AS usable
        FROM silver_laps AS l
        LEFT JOIN gold_stint_pace AS p
          ON p.driver_code = l.driver_code AND p.stint = l.stint
        GROUP BY l.driver_code, l.stint
        ORDER BY l.driver_code, l.stint
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Sem dados de stint.</p>"

    order = finishing_order(connection)
    index_of = {code: index for index, code in enumerate(order)}
    last_lap = max(row[4] for row in rows)
    row_height, gap = 19, 4
    height = len(order) * (row_height + gap) + PAD_T + PAD_B
    x0, x1 = PAD_L, WIDTH - PAD_R
    lap_width = (x1 - x0) / last_lap

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Composto de cada stint de cada piloto'>"]
    for code, index in index_of.items():
        y = PAD_T + index * (row_height + gap)
        out.append(f"<text x='{x0 - 10}' y='{y + 13}' text-anchor='end' "
                   f"class='series-label'>{esc(code)}</text>")
    stops: dict[str, int] = {}
    for (code, stint, compound, first, last, laps, fresh, life, pace,
         usable) in rows:
        if code not in index_of:
            continue
        stops[code] = stops.get(code, 0) + 1
        y = PAD_T + index_of[code] * (row_height + gap)
        bar_x = x0 + (first - 1) * lap_width
        bar_w = max((last - first + 1) * lap_width - 1.5, 2)
        tyre = "novo" if fresh else "usado"
        if not fresh and life is not None:
            tyre += f" (já tinha {int(life) - 1} voltas)"
        hover = tip(
            f"{code} · stint {int(stint)} · {compound_name(compound)}",
            f"Voltas {first} a {last} — {plural(laps, 'volta', 'voltas')}",
            f"Pneu {tyre}",
            (f"Ritmo mediano: {fmt_lap(pace)} em "
             f"{plural(int(usable), 'volta limpa', 'voltas limpas')}")
            if pace is not None else "Poucas voltas limpas para medir ritmo",
        )
        out.append(
            f"<rect x='{bar_x:.1f}' y='{y}' width='{bar_w:.1f}' "
            f"height='{row_height}' rx='3' class='hoverable' "
            f"fill='{COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)}' "
            f"fill-opacity='0.88'{hover}/>"
        )
        if bar_w > 24:
            out.append(f"<text x='{bar_x + bar_w / 2:.1f}' y='{y + 13}' "
                       f"text-anchor='middle' class='bar-text'>{laps}</text>")
    for code, index in index_of.items():
        y = PAD_T + index * (row_height + gap)
        count = max(stops.get(code, 1) - 1, 0)
        out.append(f"<text x='{x1 + 10}' y='{y + 13}' class='tick'>"
                   f"{plural(count, 'parada', 'paradas')}</text>")
    for lap in range(1, last_lap + 1, 5):
        x = x0 + (lap - 0.5) * lap_width
        out.append(f"<text x='{x:.1f}' y='{height - 16}' text-anchor='middle' "
                   f"class='tick'>{lap}</text>")
    out.append("</svg>")
    return "".join(out)


def position_change_bars(connection: duckdb.DuckDBPyConnection) -> str:
    """Who took places on track, and from whom."""
    rows = connection.execute(
        """
        SELECT passer, count(*) AS passes,
               list(passed ORDER BY lap_number, passed) AS victims,
               list(lap_number ORDER BY lap_number, passed) AS laps
        FROM gold_position_changes
        WHERE is_on_track
        GROUP BY passer ORDER BY passes DESC, passer
        """
    ).fetchall()
    colours = team_colours(connection)
    items = []
    for code, passes, victims, laps in rows:
        pairs = [f"Volta {lap}: passou {victim}"
                 for lap, victim in zip(laps, victims, strict=True)]
        shown = pairs[:10]
        if len(pairs) > len(shown):
            shown.append(f"… e mais {len(pairs) - len(shown)}")
        items.append((code, colours.get(code, UNKNOWN_COLOUR), passes,
                      plural(passes, "troca", "trocas"),
                      tip(f"{code} · trocas de posição em pista", *shown)))
    return bar_list(items)


def overtake_aid_note(connection: duckdb.DuckDBPyConnection) -> str:
    """The 2026 aid, if the race director ever switched it off."""
    if not has_table(connection, "gold_overtake_aid"):
        return ""
    rows = connection.execute(
        """
        SELECT a.aid_enabled,
               count(DISTINCT a.lap_number) AS laps,
               count(c.lap_number) FILTER (WHERE c.is_on_track) AS changes
        FROM gold_overtake_aid AS a
        LEFT JOIN gold_position_changes AS c ON c.lap_number = a.lap_number
        GROUP BY a.aid_enabled
        """
    ).fetchall()
    states = {row[0]: (row[1], row[2]) for row in rows}
    if False not in states or True not in states:
        return ""
    off_laps, off_changes = states[False]
    on_laps, on_changes = states[True]
    return f"""<p class="warn"><b>O recurso de 2026, e por que o número engana.</b>
A direção de prova desativou o auxílio de ultrapassagem em
{plural(off_laps, 'volta', 'voltas')} e o liberou em {on_laps}. Com ele
bloqueado houve <b>{off_changes / off_laps:.2f}</b> troca por volta; com ele
liberado, <b>{on_changes / on_laps:.2f}</b>. Antes de concluir que o recurso
atrapalha: as
voltas bloqueadas são justamente as de pista molhada, quando a diferença de
ritmo entre dois carros era enorme. Os dois efeitos estão grudados, e uma
corrida só não separa um do outro. Com uma temporada inteira, separa.</p>"""


def teammate_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """One row per team: who was quicker, and by how much."""
    rows = connection.execute(
        """
        SELECT team_name, driver_code, teammate_code, delta_seconds,
               median_pace_seconds, teammate_pace_seconds,
               representative_laps, teammate_representative_laps, is_comparable
        FROM gold_teammate_delta
        WHERE delta_seconds <= 0
        ORDER BY is_comparable DESC, delta_seconds DESC
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Nenhuma dupla com voltas suficientes.</p>"

    colours = team_colours(connection)
    seen, pairs = set(), []
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            pairs.append(row)
    comparable = [abs(row[3]) for row in pairs if row[8]]
    top = max(comparable) if comparable else 1.0
    row_height = 34
    height = len(pairs) * row_height + 34
    name_x, fast_x, bar_x0, bar_x1 = 14, 232, 244, WIDTH - 250

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Diferença de ritmo entre companheiros de equipe'>"]
    out.append(f"<text x='{fast_x}' y='18' text-anchor='end' class='tick'>"
               "mais rápido</text>")
    out.append(f"<text x='{bar_x0}' y='18' class='tick'>vantagem por volta →</text>")
    for index, (team, fast, slow, delta, pace, mate_pace, laps, mate_laps,
                ok) in enumerate(pairs):
        y = 28 + index * row_height
        colour = colours.get(fast, UNKNOWN_COLOUR)
        gap = abs(delta)
        width = scale(min(gap, top), 0, top, 3, bar_x1 - bar_x0)
        hover = tip(
            f"{team}: {fast} × {slow}",
            f"{fast}: {fmt_lap(pace)} de mediana em {laps} voltas limpas",
            f"{slow}: {fmt_lap(mate_pace)} de mediana em {mate_laps} voltas limpas",
            f"Diferença: {gap:.3f} s por volta" if ok
            else "Não comparável: um dos dois completou bem menos voltas",
        )
        out.append(f"<text x='{name_x}' y='{y + 17}' class='team-name'>"
                   f"{esc(team)}</text>")
        out.append(f"<text x='{fast_x}' y='{y + 17}' text-anchor='end' "
                   f"class='series-label big' fill='{colour}'>{esc(fast)}</text>")
        if ok:
            out.append(f"<rect x='{bar_x0}' y='{y + 5}' width='{width:.1f}' "
                       f"height='17' rx='3' fill='{colour}' class='hoverable'"
                       f"{hover}/>")
            label = f"+{gap:.3f} s"
        else:
            out.append(f"<rect x='{bar_x0}' y='{y + 5}' "
                       f"width='{bar_x1 - bar_x0}' height='17' rx='3' "
                       f"fill='#ececea' class='hoverable'{hover}/>")
            width = bar_x1 - bar_x0
            label = f"não comparável ({laps} × {mate_laps} voltas)"
        out.append(f"<text x='{bar_x0 + width + 9:.1f}' y='{y + 18}' "
                   f"class='value-label'>{esc(label)}  ·  "
                   f"<tspan class='muted'>{esc(slow)}</tspan></text>")
    out.append("</svg>")
    return "".join(out)


def stint_pace_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """A dot per stint, placed by median pace; dry and wet on separate scales."""
    rows = connection.execute(
        """
        SELECT driver_code, stint, compound, representative_laps,
               median_pace_seconds, consistency_seconds,
               lap_time_trend_s_per_lap, is_wet_compound
        FROM gold_stint_pace
        WHERE median_pace_seconds IS NOT NULL
        ORDER BY driver_code, stint
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Nenhum stint com voltas suficientes.</p>"

    order = [code for code in finishing_order(connection)
             if any(row[0] == code for row in rows)]
    index_of = {code: index for index, code in enumerate(order)}
    colours = team_colours(connection)
    groups = sorted({bool(row[7]) for row in rows})
    row_height, top_pad = 21, 40
    height = len(order) * row_height + top_pad + 34
    x0, x1 = PAD_L, WIDTH - 20
    panel_gap = 36
    panel_width = (x1 - x0 - panel_gap * (len(groups) - 1)) / len(groups)

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Ritmo mediano de cada stint'>"]
    for code, index in index_of.items():
        y = top_pad + index * row_height + row_height / 2
        out.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' "
                   "stroke='#f4f4f2'/>")
        out.append(f"<text x='{x0 - 10}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='series-label' fill='{colours.get(code, '#333')}'>"
                   f"{esc(code)}</text>")
    for position, is_wet in enumerate(groups):
        panel = [row for row in rows if bool(row[7]) == is_wet]
        paces = [row[4] for row in panel]
        low, high = min(paces), max(paces)
        pad = max((high - low) * 0.08, 0.2)
        low, high = low - pad, high + pad
        px0 = x0 + position * (panel_width + panel_gap) + 8
        px1 = px0 + panel_width - 16
        title = "Pneus de chuva" if is_wet else "Pneus de pista seca"
        out.append(f"<text x='{(px0 + px1) / 2:.1f}' y='16' text-anchor='middle' "
                   f"class='panel-title'>{title} — mais rápido à esquerda</text>")
        for index in range(5):
            value = low + (high - low) * index / 4
            x = scale(value, low, high, px0, px1)
            out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{top_pad - 6}' "
                       f"y2='{height - 30}' stroke='#ececea'/>")
            out.append(f"<text x='{x:.1f}' y='{height - 12}' text-anchor='middle' "
                       f"class='tick'>{fmt_lap(value)}</text>")
        for code, stint, compound, laps, pace, spread, trend, _wet in panel:
            y = top_pad + index_of[code] * row_height + row_height / 2
            radius = min(3.5 + laps ** 0.5 * 0.75, 10)
            if trend is None:
                trend_text = None
            else:
                direction = "ficando mais lento" if trend > 0 else "ficando mais rápido"
                trend_text = (f"Tendência: {trend:+.3f} s por volta "
                              f"({direction})")
            hover = tip(
                f"{code} · stint {int(stint)} · {compound_name(compound)}",
                f"Ritmo mediano: {fmt_lap(pace)}",
                f"{plural(int(laps), 'volta limpa', 'voltas limpas')}",
                f"Regularidade (desvio): {spread:.3f} s" if spread is not None
                else None,
                trend_text,
            )
            out.append(
                f"<circle cx='{scale(pace, low, high, px0, px1):.1f}' "
                f"cy='{y:.1f}' r='{radius:.1f}' class='hoverable' "
                f"fill='{COMPOUND_COLOURS.get(compound, UNKNOWN_COLOUR)}' "
                f"fill-opacity='0.82' stroke='#fff' stroke-width='1'{hover}/>"
            )
    out.append("</svg>")
    return "".join(out)


def sector_table(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT driver_code,
               median(sector1_seconds), median(sector2_seconds),
               median(sector3_seconds),
               min(sector1_seconds) + min(sector2_seconds) + min(sector3_seconds)
        FROM gold_representative_laps
        WHERE is_representative
        GROUP BY driver_code
        ORDER BY median(sector1_seconds) + median(sector2_seconds)
                 + median(sector3_seconds)
        """
    ).fetchall()
    colours = team_colours(connection)
    return heat_table(
        "Piloto",
        ["Setor 1", "Setor 2", "Setor 3", "Volta ideal"],
        [(row[0], colours.get(row[0], UNKNOWN_COLOUR), list(row[1:]))
         for row in rows],
        higher_is_better=False,
        fmt=lambda value: fmt_lap(value) if value >= 60 else f"{value:.3f}",
        unit="",
    )


SPEED_COLUMNS = (
    ("speed_i1_kph", "Setor 1 (I1)"),
    ("speed_i2_kph", "Setor 2 (I2)"),
    ("speed_finish_kph", "Linha de chegada (FL)"),
    ("speed_trap_kph", "Reta mais longa (ST)"),
)


def repeated_share(connection: duckdb.DuckDBPyConnection) -> list[tuple[str, float]]:
    """Share of readings identical to the same driver's previous lap.

    A real speed measured to the km/h rarely repeats exactly, so a high share
    is the fingerprint of forward-filled values rather than measurements.
    """
    shares = []
    for column, label in SPEED_COLUMNS:
        repeated, total = connection.execute(
            f"""
            WITH x AS (
                SELECT {column} AS v,
                       lag({column}) OVER (
                           PARTITION BY driver_code ORDER BY lap_number
                       ) AS p
                FROM silver_laps
            )
            SELECT coalesce(sum((v = p)::int), 0), count(v) FROM x
            """
        ).fetchone()
        shares.append((label, repeated / total * 100 if total else 0.0))
    return shares


def speed_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """Speed trap on the longest straight, per driver, as a range."""
    rows = connection.execute(
        """
        SELECT driver_code,
               min(speed_trap_kph) AS low,
               median(speed_trap_kph) AS mid,
               max(speed_trap_kph) AS high,
               count(speed_trap_kph) AS laps
        FROM silver_laps
        WHERE speed_trap_kph IS NOT NULL AND NOT is_in_lap AND NOT is_out_lap
        GROUP BY driver_code
        ORDER BY mid DESC
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Sem dados de radar de velocidade.</p>"

    colours = team_colours(connection)
    low = min(row[1] for row in rows) - 4
    high = max(row[3] for row in rows) + 4
    row_height, gap = 18, 6
    height = len(rows) * (row_height + gap) + PAD_T + PAD_B
    x0, x1 = PAD_L + 10, WIDTH - PAD_R

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Faixa de velocidade no radar da reta, por piloto'>"]
    for index in range(6):
        value = low + (high - low) * index / 5
        x = scale(value, low, high, x0, x1)
        out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{PAD_T - 4}' "
                   f"y2='{height - PAD_B}' stroke='#f1f1ef'/>")
        out.append(f"<text x='{x:.1f}' y='{height - 16}' text-anchor='middle' "
                   f"class='tick'>{value:.0f} km/h</text>")
    for index, (code, lo, mid, hi, laps) in enumerate(rows):
        y = PAD_T + index * (row_height + gap) + row_height / 2
        colour = colours.get(code, UNKNOWN_COLOUR)
        hover = tip(
            f"{code} · radar da reta",
            f"Mediana: {mid:.0f} km/h",
            f"Mínima {lo:.0f} · máxima {hi:.0f} km/h",
            f"{laps} voltas",
        )
        out.append(f"<text x='{x0 - 12}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='series-label'>{esc(code)}</text>")
        out.append(f"<line x1='{scale(lo, low, high, x0, x1):.1f}' "
                   f"x2='{scale(hi, low, high, x0, x1):.1f}' "
                   f"y1='{y:.1f}' y2='{y:.1f}' stroke='{colour}' "
                   "stroke-width='3' stroke-opacity='0.35' stroke-linecap='round'/>")
        out.append(f"<circle cx='{scale(mid, low, high, x0, x1):.1f}' "
                   f"cy='{y:.1f}' r='5' fill='{colour}'/>")
        out.append(f"<rect x='{x0}' y='{y - row_height / 2:.1f}' "
                   f"width='{x1 - x0}' height='{row_height}' "
                   f"fill='transparent'{hover}/>")
        out.append(f"<text x='{x1 + 10}' y='{y + 4:.1f}' class='tick'>"
                   f"<tspan class='strong'>{mid:.0f} km/h</tspan> mediana</text>")
    out.append("</svg>")
    return "".join(out)


def speed_table(connection: duckdb.DuckDBPyConnection) -> str:
    select = ", ".join(f"median({column})" for column, _ in SPEED_COLUMNS)
    rows = connection.execute(
        f"""
        SELECT driver_code, {select}
        FROM silver_laps
        WHERE NOT is_in_lap AND NOT is_out_lap
        GROUP BY driver_code
        ORDER BY median(speed_trap_kph) DESC NULLS LAST
        """
    ).fetchall()
    colours = team_colours(connection)
    return heat_table(
        "Piloto",
        [label for _, label in SPEED_COLUMNS],
        [(row[0], colours.get(row[0], UNKNOWN_COLOUR), list(row[1:]))
         for row in rows],
        higher_is_better=True,
        fmt=lambda value: f"{value:.0f}",
        unit="km/h",
    )


COMPASS = ("N", "NE", "L", "SE", "S", "SO", "O", "NO")
ARROWS = ("↓", "↙", "←", "↖", "↑", "↗", "→", "↘")


def compass(degrees: float | None) -> str:
    """Where the wind comes from, with an arrow showing where it blows to."""
    if degrees is None:
        return "—"
    index = int((degrees % 360) / 45 + 0.5) % 8
    return f"{COMPASS[index]} {ARROWS[index]}"


def race_window(connection: duckdb.DuckDBPyConnection) -> tuple[float, float] | None:
    row = connection.execute(
        "SELECT min(coalesce(lap_start_seconds, session_time_seconds)), "
        "max(session_time_seconds) FROM silver_laps "
        "WHERE session_time_seconds IS NOT NULL"
    ).fetchone()
    return (row[0], row[1]) if row and row[0] is not None else None


def leader_lap_ends(connection: duckdb.DuckDBPyConnection) -> list[float]:
    """When the winner finished each lap, to turn a clock time into a lap."""
    return [
        row[0]
        for row in connection.execute(
            """
            SELECT session_time_seconds FROM silver_laps
            WHERE session_time_seconds IS NOT NULL AND driver_code = (
                SELECT driver_code FROM gold_race_classification
                ORDER BY finishing_order LIMIT 1
            )
            ORDER BY lap_number
            """
        ).fetchall()
    ]


def weather_cards(connection: duckdb.DuckDBPyConnection) -> str:
    window = race_window(connection)
    where, scope = "", "na sessão inteira"
    if window and session_profile(connection).get("session_kind") != "qualifying":
        where = f"WHERE session_time_seconds BETWEEN {window[0]} AND {window[1]}"
        scope = "durante a corrida"
    row = connection.execute(
        f"""
        SELECT min(air_temp_c), max(air_temp_c),
               min(track_temp_c), max(track_temp_c),
               min(humidity_pct), max(humidity_pct),
               median(wind_speed_ms), max(wind_speed_ms),
               median(wind_direction_deg),
               coalesce(sum(is_raining::int), 0), count(*),
               median(pressure_mbar)
        FROM silver_weather {where}
        """
    ).fetchone()
    if not row or not row[10]:
        return ""
    (air_lo, air_hi, track_lo, track_hi, hum_lo, hum_hi, wind_mid, wind_hi,
     wind_dir, rain, samples, pressure) = row

    def span(lo, hi, unit):
        if lo is None:
            return "—"
        return f"{lo:.0f}{unit}" if round(lo) == round(hi) else f"{lo:.0f}–{hi:.0f}{unit}"

    cards = [
        ("🛣️", "Temperatura da pista", span(track_lo, track_hi, " °C")),
        ("🌡️", "Temperatura do ar", span(air_lo, air_hi, " °C")),
        ("💧", "Umidade", span(hum_lo, hum_hi, " %")),
        ("💨", "Vento", "—" if wind_mid is None else
         f"{wind_mid * 3.6:.0f} km/h (máx. {wind_hi * 3.6:.0f})"),
        ("🧭", "Direção do vento", compass(wind_dir)),
        ("⏲️", "Pressão", "—" if pressure is None else f"{pressure:.0f} mbar"),
    ]
    if rain:
        cards.insert(0, ("🌧️", "Minutos com água no sensor",
                         f"{rain} de {samples}"))
    body = "".join(
        f"<div class='card'><span class='card-icon'>{icon}</span>"
        f"<span class='card-value'>{esc(value)}</span>"
        f"<span class='card-label'>{esc(label)}</span></div>"
        for icon, label, value in cards
    )
    return (f"<div class='cards'>{body}</div>"
            f"<p class='note'>Valores medidos {scope}.</p>")


def weather_story(connection: duckdb.DuckDBPyConnection) -> str:
    """The session's weather in sentences, written from the readings.

    Each sentence exists only if its measurement does, so a still, dry,
    uneventful afternoon produces a short paragraph instead of a long one
    full of zeroes.
    """
    window = race_window(connection)
    where = ""
    if window:
        where = f"WHERE session_time_seconds BETWEEN {window[0]} AND {window[1]}"
    row = connection.execute(
        f"""
        SELECT min(track_temp_c), max(track_temp_c),
               first(track_temp_c ORDER BY session_time_seconds),
               last(track_temp_c ORDER BY session_time_seconds),
               min(air_temp_c), max(air_temp_c),
               min(humidity_pct), max(humidity_pct),
               median(wind_speed_ms) * 3.6, max(wind_speed_ms) * 3.6,
               median(wind_direction_deg),
               coalesce(sum(is_raining::INT), 0), count(*),
               coalesce(sum(track_temp_sensor_failed::INT), 0)
        FROM silver_weather {where}
        """
    ).fetchone()
    if not row or not row[12]:
        return ""
    (track_lo, track_hi, track_first, track_last, air_lo, air_hi, hum_lo,
     hum_hi, wind_mid, wind_hi, wind_dir, rain, samples, failed) = row
    kind = session_profile(connection).get("session_kind")
    scope = ("Durante a corrida" if window and kind != "qualifying"
             else "Ao longo da sessão")

    parts = []
    if track_first is not None and track_last is not None:
        direction = ("esfriou" if track_last < track_first - 1 else
                     "esquentou" if track_last > track_first + 1 else
                     "ficou estável")
        parts.append(
            f"{scope}, a pista {direction}: começou a {track_first:.0f} °C e "
            f"terminou a {track_last:.0f} °C, variando entre {track_lo:.0f} e "
            f"{track_hi:.0f} °C."
        )
    if air_lo is not None:
        parts.append(f"O ar ficou entre {air_lo:.0f} e {air_hi:.0f} °C")
        if hum_lo is not None:
            parts[-1] += (f" e a umidade entre {hum_lo:.0f} e {hum_hi:.0f} %")
        parts[-1] += "."
    if rain:
        parts.append(
            f"A estação registrou água caindo no sensor em "
            f"{plural(int(rain), 'minuto', 'minutos')} de {samples}. É um "
            "ponto só do circuito: para saber se a pista estava molhada, a "
            "faixa azul do gráfico de tempo de volta, lida dos pneus de "
            "todo o grid, é a medida melhor."
        )
    else:
        parts.append(
            f"A estação não registrou água caindo no sensor em nenhum dos "
            f"{samples} minutos medidos."
        )
    if wind_mid is not None:
        parts.append(
            f"O vento soprou de {COMPASS[int((wind_dir % 360) / 45 + 0.5) % 8]}, "
            f"com mediana de {wind_mid:.0f} km/h e rajada máxima de "
            f"{wind_hi:.0f} km/h."
        )
    if failed:
        one = int(failed) == 1
        parts.append(
            f"{plural(int(failed), 'leitura', 'leituras')} do sensor da pista "
            f"{'veio' if one else 'vieram'} como 0 °C e "
            f"{'foi descartada' if one else 'foram descartadas'}."
        )
    return f"<p class='story'>{' '.join(parts)}</p>"


def weather_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """Three aligned strips on one clock: temperature, humidity, wind."""
    if not has_table(connection, "silver_weather"):
        return "<p class='empty'>Esta sessão não traz dados de clima.</p>"
    rows = connection.execute(
        """
        SELECT session_time_seconds, track_temp_c, air_temp_c, humidity_pct,
               wind_speed_ms * 3.6, wind_direction_deg, is_raining,
               track_temp_sensor_failed, pressure_mbar
        FROM silver_weather
        ORDER BY session_time_seconds
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Sem dados de clima.</p>"

    window = race_window(connection)
    lap_ends = leader_lap_ends(connection)
    first, last = rows[0][0], rows[-1][0]
    x0, x1 = PAD_L, WIDTH - PAD_R
    strips = (
        ("🌡️ Temperatura (°C)", 150, ((1, "#c2410c", "pista"), (2, "#2563eb", "ar"))),
        ("💧 Umidade (%)", 84, ((3, "#0e7490", "umidade"),)),
        ("💨 Vento (km/h)", 84, ((4, "#6d28d9", "vento"),)),
    )
    gap, top = 30, 26
    height = top + sum(strip[1] for strip in strips) + gap * len(strips) + 26
    bottom = height - 30

    def x_of(seconds: float) -> float:
        return scale(seconds, first, last, x0, x1)

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Temperatura, umidade e vento ao longo da sessão'>"]
    if window:
        rx0, rx1 = x_of(max(window[0], first)), x_of(min(window[1], last))
        out.append(f"<rect x='{rx0:.1f}' y='{top - 8}' width='{rx1 - rx0:.1f}' "
                   f"height='{bottom - top + 8}' fill='#f5f7fb'/>")
        out.append(f"<text x='{(rx0 + rx1) / 2:.1f}' y='13' text-anchor='middle' "
                   "class='tick'>🏁 corrida</text>")
    step = (x1 - x0) / max(len(rows) - 1, 1)
    for row in rows:
        if row[6]:
            out.append(f"<rect x='{x_of(row[0]) - step / 2:.1f}' y='{top - 8}' "
                       f"width='{step + 0.4:.1f}' height='{bottom - top + 8}' "
                       "fill='#0067ad' fill-opacity='0.16'/>")

    y = top
    for title, strip_height, series in strips:
        values = [row[column] for row in rows for column, _, _ in series
                  if row[column] is not None]
        if not values:
            y += strip_height + gap
            continue
        low, high = min(values), max(values)
        pad = max((high - low) * 0.12, 1.0)
        # Only temperature can be negative; humidity and wind stop at zero.
        low, high = max(low - pad, 0.0) if series[0][0] != 1 else low - pad, high + pad
        y0, y1 = y, y + strip_height
        out.append(f"<text x='{x0}' y='{y0 - 6}' class='panel-title'>{title}</text>")
        for index in range(4):
            value = low + (high - low) * index / 3
            ty = scale(value, low, high, y1, y0)
            out.append(f"<line x1='{x0}' x2='{x1}' y1='{ty:.1f}' y2='{ty:.1f}' "
                       "stroke='#ececea'/>")
            out.append(f"<text x='{x0 - 8}' y='{ty + 4:.1f}' text-anchor='end' "
                       f"class='tick'>{value:.0f}</text>")
        for column, colour, label in series:
            parts, pen_down = [], False
            for row in rows:
                value = row[column]
                if value is None:
                    pen_down = False
                    continue
                parts.append(f"{'L' if pen_down else 'M'}{x_of(row[0]):.1f} "
                             f"{scale(value, low, high, y1, y0):.1f}")
                pen_down = True
            out.append(f"<path d='{' '.join(parts)}' fill='none' "
                       f"stroke='{colour}' stroke-width='1.8'/>")
            final = next(row[column] for row in reversed(rows)
                         if row[column] is not None)
            out.append(f"<text x='{x1 + 8}' "
                       f"y='{scale(final, low, high, y1, y0) + 4:.1f}' "
                       f"class='series-label' fill='{colour}'>{label}</text>")
        y += strip_height + gap

    for row in rows:
        if row[7]:
            out.append(f"<circle cx='{x_of(row[0]):.1f}' cy='{top + 144}' r='4' "
                       "fill='none' stroke='#b91c1c' stroke-width='1.6'/>")
    for minute in range(0, int(last / 60) + 1, 20):
        if minute * 60 < first:
            continue
        out.append(f"<text x='{x_of(minute * 60):.1f}' y='{height - 8}' "
                   f"text-anchor='middle' class='tick'>{minute} min</text>")

    for (seconds, track, air, humidity, wind, direction, raining, failed,
         pressure) in rows:
        lap = None
        if lap_ends and window and window[0] <= seconds <= window[1]:
            lap = min(bisect.bisect_left(lap_ends, seconds) + 1, len(lap_ends))
        moment = f"Minuto {seconds / 60:.0f}"
        if lap:
            moment += f" · volta {lap} do líder"
        if failed:
            track_text = "🛣️ Pista: sensor falhou nesta leitura"
        elif track is not None:
            track_text = f"🛣️ Pista: {track:.1f} °C"
        else:
            track_text = None
        hover = tip(
            moment,
            "🌧️ Água caindo no sensor" if raining else None,
            track_text,
            f"🌡️ Ar: {air:.1f} °C" if air is not None else None,
            f"💧 Umidade: {humidity:.0f} %" if humidity is not None else None,
            f"💨 Vento: {wind:.0f} km/h · {compass(direction)}"
            if wind is not None else None,
            f"⏲️ Pressão: {pressure:.0f} mbar" if pressure is not None else None,
        )
        out.append(f"<rect x='{x_of(seconds) - step / 2:.1f}' y='{top - 8}' "
                   f"width='{step + 0.4:.1f}' height='{bottom - top + 8}' "
                   f"fill='transparent' class='column'{hover}/>")
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
        f"{esc(compound_name(compound))} <b>{laps:,}</b> voltas</span>"
        for compound, laps in rows
    )
    return f"<div class='legend'>{items}</div>"


# --------------------------------------------------------------------------
# race control: messages are grouped into stories instead of listed
# --------------------------------------------------------------------------

CAR_RE = re.compile(r"\(([A-Z]{3})\)")
STAMP_RE = re.compile(r"\((\d{1,2}:\d{2}:\d{2})\)\s*$")
TURN_RE = re.compile(r"\bTURN (\d+)")
DELETED_RE = re.compile(r"AT TURN (\d+) LAP (\d+)")
PENALTY_RE = re.compile(r"(\d+) SECOND (TIME|STOP)")

REASONS = {
    "FALSE START - OUT OF POSITION": "Largada irregular — fora de posição",
    "FALSE START": "Queima de largada",
    "TRACK LIMITS": "Limites de pista",
    "LEAVING THE TRACK AND GAINING AN ADVANTAGE":
        "Saiu da pista e ganhou vantagem",
    "YELLOW FLAG INFRINGEMENT": "Desrespeito à bandeira amarela",
    "UNSAFE RELEASE": "Liberação insegura no box",
    "CAUSING A COLLISION": "Causou colisão",
    "REJOINING UNSAFELY": "Voltou à pista de forma insegura",
    "FORCING ANOTHER DRIVER OFF THE TRACK": "Forçou outro piloto para fora",
    "SPEEDING IN THE PIT LANE": "Excesso de velocidade no box",
    "FAILING TO FOLLOW RACE DIRECTORS INSTRUCTIONS – PRACTICE START INFRINGEMENT":
        "Descumpriu instruções do diretor de prova — treino de largada",
}

# kind -> (row label on the timeline, colour)
TIMELINE_ROWS = (
    ("track", "🚦 Pista", "#52525b"),
    ("yellow", "🟡 Amarelas", "#d4a106"),
    ("incident", "🔎 Incidentes", "#c2410c"),
    ("penalty", "⛔ Punições", "#b91c1c"),
    ("deleted", "❌ Voltas deletadas", "#7c3aed"),
    ("blue", "🔵 Azuis", "#2563eb"),
)


def classify_message(flag: str | None, message: str) -> str:
    """Which story a race control message belongs to."""
    text = message.upper()
    if flag == "BLUE":
        return "blue"
    if flag in ("YELLOW", "DOUBLE YELLOW"):
        return "yellow"
    if flag in ("CLEAR", "GREEN"):
        return "clear"
    if flag == "BLACK AND WHITE":
        return "incident"
    if "DELETED" in text:
        return "deleted"
    if "PENALTY" in text:
        return "penalty"
    if "INCIDENT" in text:
        return "incident"
    return "track"


def incident_stage(flag: str | None, message: str) -> tuple[str, str]:
    """(label, severity) for one step of an incident's life."""
    text = message.upper()
    if flag == "BLACK AND WHITE":
        return "Bandeira preta e branca (advertência)", "warn"
    if "PENALTY SERVED" in text:
        return "Punição cumprida", "served"
    if "PENALTY" in text:
        match = PENALTY_RE.search(text)
        if match:
            kind = "stop-and-go de " if match.group(2) == "STOP" else ""
            return f"Punição: {kind}{match.group(1)} s", "penalty"
        return "Punição", "penalty"
    if "NO FURTHER" in text:
        return "Sem punição", "clear"
    if "AFTER THE RACE" in text:
        return "Será investigado após a corrida", "pending"
    if "UNDER INVESTIGATION" in text:
        return "Sob investigação", "open"
    return "Anotado", "open"


def incident_reason(message: str) -> str | None:
    text = STAMP_RE.sub("", message).strip()
    if " - " not in text:
        return None
    reason = text.split(" - ", 1)[1].strip()
    return REASONS.get(reason.upper(), reason.capitalize())


def build_incidents(messages: list[tuple]) -> list[dict]:
    """Fold the stewards' messages into one record per incident.

    Messages about the same incident share the clock time it happened at and
    at least one car, which is how "noted", "under investigation", the
    penalty and "penalty served" find each other across twenty laps.
    """
    incidents: list[dict] = []
    for lap, flag, message, driver in messages:
        cars = set(CAR_RE.findall(message))
        if driver and not cars:
            cars = {driver}
        stamp = STAMP_RE.search(message)
        key = stamp.group(1) if stamp else f"lap-{lap}-{message}"
        found = next(
            (item for item in incidents
             if item["key"] == key and (item["cars"] & cars or not cars)),
            None,
        )
        if found is None:
            turn = TURN_RE.search(message)
            found = {"key": key, "cars": set(), "steps": [], "reason": None,
                     "turn": turn.group(1) if turn else None}
            incidents.append(found)
        found["cars"] |= cars
        found["reason"] = found["reason"] or incident_reason(message)
        label, severity = incident_stage(flag, message)
        found["steps"].append((lap, label, severity))
    return incidents


def control_messages(connection: duckdb.DuckDBPyConnection) -> list[tuple]:
    """(lap, flag, message, driver_code, sector, when) for every message."""
    return connection.execute(
        """
        SELECT c.lap_number, c.flag, c.message, r.driver_code, c.sector,
               c.message_time
        FROM silver_race_control AS c
        LEFT JOIN silver_results AS r
          ON CAST(r.driver_number AS VARCHAR) = CAST(c.driver_number AS VARCHAR)
        ORDER BY c.message_time
        """
    ).fetchall()


def timeline_axis(messages: list[tuple]) -> str:
    """Laps, or the clock when the feed sent no lap numbers.

    A race's messages carry a lap. A qualifying session's, in this data, do
    not: every one of the 87 messages from the 2026 Sao Paulo qualifying has
    a null lap. Falling back to the clock keeps the tab from going blank.
    """
    with_lap = sum(1 for message in messages if message[0] is not None)
    return "lap" if with_lap >= len(messages) / 2 else "clock"


def message_moment(axis: str, lap, when, base) -> tuple[float, str]:
    """Where a message sits on the axis, and how to name that spot.

    On the clock axis the unit is minutes since the first message, so the
    scale never deals in epoch seconds and the label is built back from the
    session's own timestamps.
    """
    if axis == "lap":
        return float(lap), f"Volta {int(lap)}"
    return round((when - base).total_seconds() / 60), when.strftime("%H:%M")


def control_timeline(messages: list[tuple]) -> str:
    """Every message as a mark on a lap or clock axis, one row per kind."""
    axis = timeline_axis(messages)
    base = min(message[5] for message in messages)
    grouped: dict[tuple[str, float], list[str]] = {}
    labels: dict[float, str] = {}
    for lap, flag, message, _driver, _sector, when in messages:
        kind = classify_message(flag, message)
        if kind == "clear" or (axis == "lap" and lap is None):
            continue
        at, label = message_moment(axis, lap, when, base)
        grouped.setdefault((kind, at), []).append(message)
        labels[at] = label
    if not grouped:
        return "<p class='empty'>Nenhuma mensagem nesta sessão.</p>"

    marks = sorted({at for _, at in grouped})
    first, last = (1, max(marks)) if axis == "lap" else (min(marks), max(marks))
    row_height = 34
    height = len(TIMELINE_ROWS) * row_height + 44
    x0, x1 = 170, WIDTH - 24
    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' "
           "aria-label='Linha do tempo da direção de prova'>"]
    step = 5 if axis == "lap" else max(round((last - first) / 8), 1)
    for value in range(int(first) + step, int(last) + 1, step):
        x = scale(value, first, last, x0, x1)
        out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='8' "
                   f"y2='{height - 30}' stroke='#f1f1ef'/>")
        text = (value if axis == "lap"
                else (base + timedelta(minutes=value)).strftime("%H:%M"))
        out.append(f"<text x='{x:.1f}' y='{height - 12}' text-anchor='middle' "
                   f"class='tick'>{esc(text)}</text>")
    out.append(f"<text x='{x0 - 12}' y='{height - 12}' text-anchor='end' "
               f"class='tick'>{'volta' if axis == 'lap' else 'hora'}</text>")
    for index, (kind, label, colour) in enumerate(TIMELINE_ROWS):
        y = 12 + index * row_height + row_height / 2
        out.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' "
                   "stroke='#e4e4e0'/>")
        out.append(f"<text x='14' y='{y + 4:.1f}' class='row-label'>{label}</text>")
        for (item_kind, at), texts in sorted(grouped.items()):
            if item_kind != kind:
                continue
            shown = texts[:6]
            extra = len(texts) - len(shown)
            hover = tip(
                f"{labels[at]} · {plural(len(texts), 'mensagem', 'mensagens')}",
                *shown,
                f"… e mais {extra}" if extra else None,
            )
            radius = min(4.5 + len(texts) * 0.9, 11)
            out.append(
                f"<circle cx='{scale(at, first, last, x0, x1):.1f}' cy='{y:.1f}' "
                f"r='{radius:.1f}' fill='{colour}' fill-opacity='0.78' "
                f"stroke='#fff' stroke-width='1' class='hoverable'{hover}/>"
            )
    out.append("</svg>")
    return "".join(out)


SEVERITY_ORDER = ("penalty", "pending", "warn", "open", "clear", "served")


def incident_cards(messages: list[tuple]) -> str:
    relevant = [
        (lap if lap is not None else when.strftime("%H:%M"), flag, message, driver)
        for lap, flag, message, driver, _sector, when in messages
        if classify_message(flag, message) in ("incident", "penalty")
    ]
    incidents = build_incidents(relevant)
    if not incidents:
        return "<p class='empty'>Nenhum incidente anotado.</p>"

    def outcome(item: dict) -> str:
        severities = {step[2] for step in item["steps"]}
        return next(name for name in SEVERITY_ORDER if name in severities)

    incidents.sort(key=lambda item: (SEVERITY_ORDER.index(outcome(item)),
                                     str(item["steps"][0][0])))
    cards = []
    for item in incidents:
        who = " × ".join(sorted(item["cars"])) or "Pista"
        place = f" · curva {item['turn']}" if item["turn"] else ""
        steps = " <span class='arrow'>→</span> ".join(
            f"<span class='step {severity}'>"
            f"<b>{f'V{lap}' if isinstance(lap, int) else esc(lap)}</b> "
            f"{esc(label)}</span>"
            for lap, label, severity in item["steps"]
        )
        reason = esc(item["reason"]) if item["reason"] else "Motivo não informado"
        cards.append(
            f"<div class='incident {outcome(item)}'>"
            f"<div class='incident-head'><b>{esc(who)}</b>{place}</div>"
            f"<div class='incident-reason'>{reason}</div>"
            f"<div class='steps'>{steps}</div></div>"
        )
    return f"<div class='incidents'>{''.join(cards)}</div>"


def deleted_laps_bars(messages: list[tuple], colours: dict[str, str]) -> str:
    per_driver: dict[str, list[str]] = {}
    for _lap, flag, message, driver, _sector, _when in messages:
        if classify_message(flag, message) != "deleted":
            continue
        cars = CAR_RE.findall(message)
        code = cars[0] if cars else (driver or "?")
        match = DELETED_RE.search(message.upper())
        where = (f"Volta {match.group(2)} — limites de pista na curva "
                 f"{match.group(1)}") if match else message
        per_driver.setdefault(code, []).append(where)
    items = [
        (code, colours.get(code, UNKNOWN_COLOUR), len(entries),
         plural(len(entries), "volta", "voltas"),
         tip(f"{code} · voltas deletadas", *entries))
        for code, entries in sorted(per_driver.items(),
                                    key=lambda pair: -len(pair[1]))
    ]
    return bar_list(items)


def blue_flag_bars(messages: list[tuple], colours: dict[str, str]) -> str:
    per_driver: dict[str, list[int]] = {}
    for lap, flag, _message, driver, _sector, _when in messages:
        if flag == "BLUE" and driver and lap is not None:
            per_driver.setdefault(driver, []).append(int(lap) if lap else 0)
    items = [
        (code, colours.get(code, UNKNOWN_COLOUR), len(laps),
         plural(len(laps), "bandeira", "bandeiras"),
         tip(f"{code} · bandeiras azuis",
             f"Entre as voltas {min(laps)} e {max(laps)}",
             "Mostrada a quem está para tomar uma volta"))
        for code, laps in sorted(per_driver.items(), key=lambda pair: -len(pair[1]))
    ]
    return bar_list(items)


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
            trailing = (f" <span class='muted-cell'>+{behind:.3f}</span>"
                        if behind is not None else "")
            time_cell = f"+{plural(int(laps_down), 'volta', 'voltas')}{trailing}"
        else:
            time_cell = "—"

        if gained is None:
            change = "—"
        elif gained > 0:
            change = f"<span class='up'>▲ {int(gained)}</span>"
        elif gained < 0:
            change = f"<span class='down'>▼ {abs(int(gained))}</span>"
        else:
            change = "="

        klass = "" if is_classified else " class='unclassified'"
        swatch = (f"<i class='swatch' style='background:"
                  f"{colours.get(code, UNKNOWN_COLOUR)}'></i>")
        body.append(
            f"<tr{klass}><td class='pos'>{esc(position)}</td>"
            f"<td>{swatch}<b>{esc(code)}</b> {esc(name)}</td>"
            f"<td>{esc(team)}</td><td>{esc(STATUS_NAMES.get(status, status))}</td>"
            f"<td class='num'>{int(grid)}</td><td class='num'>{change}</td>"
            f"<td class='num'>{int(laps)}</td><td class='num'>{time_cell}</td>"
            f"<td class='num'>{points:g}</td></tr>"
        )

    return (
        "<table class='data'><thead><tr><th>Pos</th><th>Piloto</th><th>Equipe</th>"
        "<th>Situação</th><th>Grid</th><th>Ganhou / perdeu</th><th>Voltas</th>"
        "<th>Tempo / diferença</th><th>Pts</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def podium(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT classified_position, driver_code, driver_name, team_name
        FROM gold_race_classification
        WHERE is_classified ORDER BY finishing_order LIMIT 3
        """
    ).fetchall()
    colours = team_colours(connection)
    medals = ("🥇", "🥈", "🥉")
    return "<div class='podium'>" + "".join(
        f"<div class='step-card' style='border-top-color:"
        f"{colours.get(code, UNKNOWN_COLOUR)}'><span class='medal'>{medals[index]}"
        f"</span><span class='card-value'>{esc(name)}</span>"
        f"<span class='card-label'>{esc(team)}</span></div>"
        for index, (_position, code, name, team) in enumerate(rows)
    ) + "</div>"


def summary_cards(connection: duckdb.DuckDBPyConnection) -> str:
    """The race in cards. Fixed cards every race has; the rest earn their place.

    A card reading "0 de 71 voltas com pista molhada" is a sentence about
    the last race this code was written for, not about this one.
    """
    profile = session_profile(connection)
    fastest = connection.execute(
        "SELECT driver_code, lap_time_seconds FROM gold_fastest_lap"
    ).fetchone()

    cards = []
    laps = profile.get("race_laps") or profile.get("laps_run")
    if laps:
        cards.append(("🏁", "Distância", plural(int(laps), "volta", "voltas")))
    if fastest:
        cards.append(("⚡", "Volta mais rápida",
                      f"{fastest[0]} {fmt_lap(fastest[1])}"))
    margin, down = (profile.get("winning_margin_seconds"),
                    profile.get("runner_up_laps_down"))
    if margin is not None:
        cards.append(("⏱️", "Margem da vitória", f"{margin:.3f} s"))
    elif down:
        cards.append(("⏱️", "Margem da vitória",
                      plural(int(down), "volta", "voltas")))
    if profile.get("lead_changes") is not None:
        cards.append(("🔄", "Trocas de liderança", f"{profile['lead_changes']}"))
    if profile.get("biggest_gainer") and (profile.get("biggest_gain") or 0) > 0:
        cards.append(("📈", "Quem mais subiu",
                      f"{profile['biggest_gainer']} +{int(profile['biggest_gain'])}"))
    if profile.get("pit_stops"):
        cards.append(("🔧", "Paradas nos boxes", f"{int(profile['pit_stops'])}"))
    if profile.get("on_track_position_changes") is not None:
        cards.append(("🔀", "Trocas de posição em pista",
                      f"{int(profile['on_track_position_changes'])}"))

    # Conditional: these only exist on the page when they happened on track.
    counted = profile.get("counted_laps") or 0
    for key, icon, label in (
        ("wet_laps", "💦", "Voltas com pista molhada"),
        ("safety_car_laps", "🚨", "Voltas com safety car"),
        ("vsc_laps", "🟠", "Voltas com safety car virtual"),
        ("red_flag_laps", "🟥", "Voltas com bandeira vermelha"),
    ):
        value = profile.get(key) or 0
        if value:
            cards.append((icon, label, f"{int(value)} de {counted}"))

    return "<div class='cards'>" + "".join(
        f"<div class='card'><span class='card-icon'>{icon}</span>"
        f"<span class='card-value'>{esc(value)}</span>"
        f"<span class='card-label'>{esc(label)}</span></div>"
        for icon, label, value in cards
    ) + "</div>"


def lap_count_note(connection: duckdb.DuckDBPyConnection) -> str:
    """How many laps the page is built on. Data quality, not a race fact."""
    total, usable = connection.execute(
        "SELECT count(*), sum(is_representative::int) FROM gold_representative_laps"
    ).fetchone()
    if not total:
        return ""
    both = (f"{total:,}".replace(",", "."), f"{usable or 0:,}".replace(",", "."))
    return (f"<p class='count'>Somando todos os pilotos, a sessão tem "
            f"<b>{both[0]}</b> voltas cronometradas, das quais "
            f"<b>{both[1]}</b> são limpas e entram nas medidas de ritmo.</p>")


# Tabs that only make sense when their source table was loaded. The body is
# built lazily, because building it runs the query: an eager f-string would
# hit a missing table before the filter below could drop the tab.
TAB_REQUIRES = {"weather": "silver_weather", "control": "silver_race_control"}

HOVER_HINT = "<span class='hint'>🖱️ Passe o mouse para ver os detalhes.</span>"


def incomplete_timing_note(connection: duckdb.DuckDBPyConnection) -> str:
    """Say so when the timing data holds fewer laps than the result counts.

    The 2026 Mexico City race was red-flagged and the timing data this
    pipeline receives stops at lap 56, while the classification credits the
    winner with 71. Rather than draw a 56-lap race and call it the race, the
    page says which number came from where.
    """
    profile = session_profile(connection)
    timed, classified = profile.get("laps_run"), profile.get("race_laps")
    if not timed or not classified or classified <= timed:
        return ""
    return f"""<p class="warn"><b>Faltam voltas na cronometragem.</b> O
resultado credita {plural(int(classified), 'volta', 'voltas')} ao vencedor,
mas os dados de cronometragem que recebemos vão até a volta {int(timed)} —
{plural(int(classified - timed), 'volta', 'voltas')} de diferença. Todo
gráfico desta página é desenhado com o que a cronometragem tem, e a tabela de
classificação é a publicada. Não preenchemos o buraco com estimativa.</p>"""


def tab_summary(connection: duckdb.DuckDBPyConnection) -> str:
    _state, bands = track_condition(connection)
    strips = {
        "wet": "A faixa azul embaixo mostra quanto do grid estava em pneu "
               "intermediário ou de chuva em cada volta — quanto mais escura, "
               "maior a parcela: é o estado da pista lido dos pneus.",
        "neutralisation": "Outra faixa marca as voltas em que a corrida "
                          "esteve neutralizada, na cor da bandeira.",
        "overtake_aid": "A faixa roxa marca as voltas em que a direção de "
                        "prova bloqueou o auxílio de ultrapassagem.",
    }
    condition_note = " ".join(strips[band] for band in bands)
    return f"""{podium(connection)}
{summary_cards(connection)}
{incomplete_timing_note(connection)}
<h2>Tempo de volta ao longo da corrida</h2>
<p class="note">Um ponto por volta, na cor do composto. Ficam de fora voltas
sob bandeira amarela, voltas deletadas e voltas de entrada ou saída do box:
nenhuma delas se compara com uma volta limpa. {HOVER_HINT} Parar sobre um ponto
destaca todas as voltas daquele piloto. {condition_note}</p>
{lap_time_chart(connection)}
{compound_legend(connection)}
{lap_count_note(connection)}
<h2>Classificação</h2>
<p class="note">Reconstruída a partir da cronometragem bruta e conferida com o
resultado publicado pela FIA. Linhas em cinza não foram classificadas.</p>
{classification_table(connection)}"""


def tab_race(connection: duckdb.DuckDBPyConnection) -> str:
    raw, on_track = connection.execute(
        "SELECT count(*), sum(is_on_track::int) FROM gold_position_changes"
    ).fetchone()
    pit_share = (raw - (on_track or 0)) / raw * 100 if raw else 0
    return f"""<h2>Posição, volta a volta</h2>
<p class="note">Uma linha por piloto, na cor da equipe, começando no grid de
largada (o quadradinho). A bolinha branca marca a volta em que ele entrou no
box. {HOVER_HINT} A linha do piloto fica em
destaque e as outras se apagam.</p>
{position_chart(connection)}
<h2>Estratégia</h2>
<p class="note">Uma barra por stint, na cor do composto; o número dentro da
barra é a quantidade de voltas. {HOVER_HINT}</p>
{stint_chart(connection)}
{compound_legend(connection)}
<h2>Trocas de posição em pista</h2>
<p class="note">Quantas vezes cada piloto terminou a volta à frente de alguém
que estava na sua frente na volta anterior. <b>Não é a contagem oficial de
ultrapassagens</b>: ficam de fora as trocas em que algum dos dois estava
entrando ou saindo do box — eram {pit_share:.0f}% do total bruto nesta corrida
— mas continuam de fora do nosso alcance a ultrapassagem desfeita dentro da
mesma volta e a posição real dentro da volta, porque só enxergamos a ordem no
fim de cada uma. {HOVER_HINT}</p>
{position_change_bars(connection)}
{overtake_aid_note(connection)}"""


def tab_pace(connection: duckdb.DuckDBPyConnection) -> str:
    scales = connection.execute(
        "SELECT count(DISTINCT is_wet_compound) FROM gold_stint_pace "
        "WHERE median_pace_seconds IS NOT NULL"
    ).fetchone()[0]
    split = ("Seco e molhado ficam em escalas separadas, porque não se "
             "comparam. " if scales > 1 else "")
    return f"""<h2>Diferença para o companheiro de equipe</h2>
<p class="note">A única comparação em que o carro é o mesmo. A barra mostra
quanto o piloto da esquerda foi mais rápido por volta, na mediana das voltas
limpas. Quando um dos dois completou bem menos voltas, a dupla aparece como
"não comparável": seriam duas corridas diferentes. {HOVER_HINT}</p>
{teammate_chart(connection)}
<h2>Ritmo por stint</h2>
<p class="note">Cada bolinha é um stint, posicionada pelo ritmo mediano e
colorida pelo composto; quanto maior, mais voltas limpas. {split}{HOVER_HINT} A "tendência" é
quanto o tempo de volta mudou a cada volta do stint — ela mistura desgaste de
pneu, queima de combustível e evolução da pista, por isso não chamamos de
degradação.</p>
{stint_pace_chart(connection)}
<h2>Setores</h2>
<p class="note">Mediana de cada setor nas voltas limpas. A célula roxa é o
melhor da coluna; as outras mostram quanto perderam para ele, e quanto mais
forte o vermelho, maior a perda. "Volta ideal" soma os três melhores setores
de cada piloto. {HOVER_HINT}</p>
{sector_table(connection)}"""


def tab_speed(connection: duckdb.DuckDBPyConnection) -> str:
    shares = repeated_share(connection)
    share_text = "; ".join(f"{label}: {share:.0f}%" for label, share in shares)
    return f"""<h2>Radar da reta mais longa</h2>
<p class="warn"><b>Leia antes de usar estes números.</b> São leituras reais dos
radares, mas com um porém: quando a cronometragem deixa de mandar uma leitura,
o FastF1 repete o valor da volta anterior. Medimos isso nesta corrida — parcela
de leituras idênticas à volta anterior do mesmo piloto: {esc(share_text)}. E a
própria documentação do FastF1 descreve o <code>SpeedST</code> como
"maybe a speed trap on the longest straight (?)". Sirva-se da ordem de
grandeza, não da casa decimal.</p>
<p class="note">Voltas de entrada e de saída do box ficam de fora.
{HOVER_HINT}</p>
<div class="legend range-key"><span>mínima</span><i class="range-line"></i>
<span class="range-dot"></span><b>mediana</b> — metade das voltas acima, metade
abaixo (não é a média)<i class="range-line"></i><span>máxima</span></div>
{speed_chart(connection)}
<h2>Os quatro radares</h2>
<p class="note">Mediana por piloto. A célula roxa é o mais veloz da coluna; as
outras mostram quantos km/h ficaram abaixo dele.</p>
{speed_table(connection)}"""


def tab_weather(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>A história do clima</h2>
{weather_story(connection)}
{weather_cards(connection)}
<h2>Linha do tempo</h2>
<p class="note">Uma leitura por minuto, do início da sessão ao fim. A faixa
clara é a corrida e as faixas azuis são os minutos em que o sensor registrou
chuva caindo. O anel vermelho marca uma leitura em que o sensor da pista
informou exatamente 0&nbsp;°C — isso é falha do sensor, não temperatura, e o
valor é guardado como nulo.
{HOVER_HINT}</p>
{weather_chart(connection)}"""


def tab_control(connection: duckdb.DuckDBPyConnection) -> str:
    messages = control_messages(connection)
    colours = team_colours(connection)
    moment = ("V = volta" if timeline_axis(messages) == "lap"
              else "marcado pelo horário")
    return f"""<h2>Linha do tempo da direção de prova</h2>
<p class="note">Cada bolinha reúne as mensagens daquele tipo naquele momento;
quanto maior, mais mensagens. Quando a cronometragem não manda o número da
volta — o que acontece na classificação — o eixo passa a ser o relógio.
{HOVER_HINT} O texto original, em inglês, aparece na caixinha.</p>
{control_timeline(messages)}
<h2>Incidentes e punições</h2>
<p class="note">Cada cartão conta a história de um incidente, do momento em
que foi anotado até a decisão ({moment}). Aqui entram só as punições
<em>aplicadas durante a sessão</em>; as decididas depois da bandeirada ficam
nos documentos da FIA.</p>
{incident_cards(messages)}
<div class="two-col"><div>
<h2>Voltas deletadas por piloto</h2>
<p class="note">Limites de pista. {HOVER_HINT}</p>
{deleted_laps_bars(messages, colours)}
</div><div>
<h2>Bandeiras azuis por piloto</h2>
<p class="note">Quem mais precisou abrir para os líderes.</p>
{blue_flag_bars(messages, colours)}
</div></div>"""


def details(title: str, body: str) -> str:
    return f"<details><summary>{esc(title)}</summary>{body}</details>"


def tab_raw(connection: duckdb.DuckDBPyConnection) -> str:
    blocks = [
        details("Todas as voltas, todas as colunas (silver_laps)", table_of(
            connection, """
            SELECT driver_code, lap_number, position, stint, compound, tyre_life,
                   fresh_tyre, round(lap_time_seconds, 3) AS lap_time_s,
                   round(sector1_seconds, 3) AS s1,
                   round(sector2_seconds, 3) AS s2,
                   round(sector3_seconds, 3) AS s3,
                   speed_i1_kph, speed_i2_kph, speed_finish_kph, speed_trap_kph,
                   track_status, is_personal_best, is_deleted, deleted_reason,
                   is_accurate, is_reconstructed, is_in_lap, is_out_lap,
                   round(session_time_seconds, 1) AS session_time_s
            FROM silver_laps ORDER BY lap_number, position
            """)),
        details("Resultado, todas as colunas (silver_results)", table_of(
            connection, """
            SELECT driver_number, driver_code, driver_name, broadcast_name,
                   driver_id, team_name, team_id, team_colour, country_code,
                   grid_position, finishing_order, classified_position, status,
                   points, laps_completed, round(time_seconds, 3) AS time_s,
                   is_classified
            FROM silver_results ORDER BY finishing_order
            """)),
        details("Ritmo por stint (gold_stint_pace)", table_of(
            connection, """
            SELECT driver_code, stint, compound, representative_laps,
                   round(median_pace_seconds, 3) AS median_pace_s,
                   round(consistency_seconds, 3) AS stddev_s,
                   round(lap_time_trend_s_per_lap, 4) AS trend_s_per_lap,
                   is_wet_compound, trend_is_negative
            FROM gold_stint_pace ORDER BY driver_code, stint
            """)),
        details("Companheiros de equipe (gold_teammate_delta)", table_of(
            connection, """
            SELECT driver_code, team_name, teammate_code,
                   round(delta_seconds, 3) AS delta_s, representative_laps,
                   teammate_representative_laps,
                   round(median_pace_seconds, 3) AS median_pace_s, is_comparable
            FROM gold_teammate_delta ORDER BY delta_seconds
            """)),
    ]
    if has_table(connection, "gold_qualifying_result"):
        blocks.append(details(
            "Classificação por segmento (gold_qualifying_result)", table_of(
                connection, """
            SELECT qualifying_position, driver_code, team_name,
                   round(q1_seconds, 3) AS q1_s, round(q2_seconds, 3) AS q2_s,
                   round(q3_seconds, 3) AS q3_s, reached_segment, eliminated_in,
                   round(gap_to_pole_seconds, 3) AS gap_to_pole_s,
                   q1_timed_laps, q2_timed_laps, q3_timed_laps,
                   matches_published
            FROM gold_qualifying_result ORDER BY qualifying_position
            """, scroll=False)))
    if has_table(connection, "silver_weather"):
        blocks.append(details("Clima, cada leitura (silver_weather)", table_of(
            connection, """
            SELECT round(session_time_seconds / 60, 1) AS minute, air_temp_c,
                   track_temp_c, track_temp_sensor_failed, humidity_pct,
                   pressure_mbar, is_raining, wind_speed_ms, wind_direction_deg
            FROM silver_weather ORDER BY session_time_seconds
            """)))
    if has_table(connection, "silver_race_control"):
        blocks.append(details(
            "Direção de prova, cada mensagem (silver_race_control)", table_of(
                connection, """
            SELECT lap_number, category, flag, scope, sector, driver_number,
                   mentions_penalty, mentions_investigation,
                   mentions_deleted_lap, message
            FROM silver_race_control ORDER BY lap_number, message_time
            """)))
    return f"""<h2>Dados brutos</h2>
<p class="note">Tudo o que o warehouse guarda, sem filtro, para conferir
qualquer número das outras abas. Os nomes das colunas são os do banco, em
inglês, para bater com as consultas. Clique para abrir.</p>
{''.join(blocks)}"""



# --------------------------------------------------------------------------
# qualifying
# --------------------------------------------------------------------------

SEGMENT_COLOURS = {1: "#64748b", 2: "#0ea5e9", 3: "#7c3aed"}


def qualifying_lap_chart(connection: duckdb.DuckDBPyConnection) -> str:
    """Every timed lap against the clock, coloured by segment.

    The x axis is the session's own clock rather than the lap number: in
    qualifying a driver's lap 8 and another's lap 8 happen minutes apart, and
    what the chart is really showing is the track getting quicker.
    """
    rows = connection.execute(
        """
        SELECT coalesce(l.lap_start_seconds, l.session_time_seconds) / 60,
               l.lap_time_seconds, s.segment_number, l.driver_code,
               l.compound, l.is_deleted, l.is_personal_best
        FROM silver_laps AS l
        JOIN gold_qualifying_segments AS s
          ON s.driver_code = l.driver_code AND s.lap_number = l.lap_number
        WHERE l.lap_time_seconds IS NOT NULL
          AND NOT l.is_in_lap AND NOT l.is_out_lap
        ORDER BY 1
        """
    ).fetchall()
    if not rows:
        return "<p class='empty'>Nenhuma volta cronometrada.</p>"

    pole = connection.execute(
        "SELECT driver_code, best_seconds FROM gold_qualifying_result "
        "ORDER BY qualifying_position LIMIT 1"
    ).fetchone()

    height = 420
    times = sorted(row[1] for row in rows)
    # A qualifying session is full of slow laps -- preparation, cooling,
    # running under a flag -- that are three times the pole time and would
    # squash every lap that matters into the bottom of the chart. The window
    # is the fast laps; the ones left out are counted under the chart.
    low = times[0]
    high = min(times[int(len(times) * 0.9)], low * 1.18)
    pad = (high - low) * 0.06
    low, high = low - pad, high + pad
    hidden = sum(1 for row in rows if not low <= row[1] <= high)
    minutes = [row[0] for row in rows]
    first, last = min(minutes), max(minutes)
    x0, x1 = PAD_L, WIDTH - PAD_R
    y0, y1 = PAD_T, height - PAD_B

    out = [f"<svg viewBox='0 0 {WIDTH} {height}' role='img' class='focusable' "
           "aria-label='Tempo de cada volta cronometrada ao longo da sessão'>"]
    for index in range(6):
        value = low + (high - low) * index / 5
        y = scale(value, low, high, y1, y0)
        out.append(f"<line x1='{x0}' x2='{x1}' y1='{y:.1f}' y2='{y:.1f}' "
                   "stroke='#ececea'/>")
        out.append(f"<text x='{x0 - 8}' y='{y + 4:.1f}' text-anchor='end' "
                   f"class='tick'>{fmt_lap(value)}</text>")

    # Each segment gets a band behind its laps, so the three sessions inside
    # the session are visible without reading the legend.
    for number in sorted({row[2] for row in rows}):
        inside = [row[0] for row in rows if row[2] == number]
        bx0 = scale(min(inside), first, last, x0, x1) - 3
        bx1 = scale(max(inside), first, last, x0, x1) + 3
        out.append(f"<rect x='{bx0:.1f}' y='{y0}' width='{bx1 - bx0:.1f}' "
                   f"height='{y1 - y0}' fill='{SEGMENT_COLOURS[number]}' "
                   "fill-opacity='0.05'/>")
        out.append(f"<text x='{(bx0 + bx1) / 2:.1f}' y='{y0 + 14}' "
                   f"text-anchor='middle' class='panel-title' "
                   f"fill='{SEGMENT_COLOURS[number]}'>Q{number}</text>")

    for minute in range(int(first // 10 * 10), int(last) + 1, 10):
        if minute < first:
            continue
        x = scale(minute, first, last, x0, x1)
        out.append(f"<text x='{x:.1f}' y='{y1 + 22}' text-anchor='middle' "
                   f"class='tick'>{minute} min</text>")

    for minute, seconds, number, code, compound, deleted, personal_best in rows:
        if not low <= seconds <= high:
            continue
        x = scale(minute, first, last, x0, x1)
        y = scale(seconds, low, high, y1, y0)
        is_pole = pole and code == pole[0] and abs(seconds - pole[1]) < 0.0005
        hover = tip(
            f"{code} · Q{number}" + (" · POLE" if is_pole else ""),
            fmt_lap(seconds),
            f"Minuto {minute:.0f} da sessão · {compound_name(compound)}",
            "Volta deletada" if deleted else
            ("Melhor volta pessoal" if personal_best else None),
        )
        colour = SEGMENT_COLOURS[number]
        if deleted:
            out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3.4' fill='none' "
                       f"stroke='{colour}' stroke-width='1.2' "
                       f"stroke-dasharray='1.5 1.5' data-d='{esc(code)}'"
                       f"{hover}/>")
            continue
        out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{3.6 if is_pole else 2.8}' "
                   f"fill='{colour}' fill-opacity='0.75' data-d='{esc(code)}'"
                   f"{hover}/>")
        if is_pole:
            out.append(f"<text x='{x + 8:.1f}' y='{y + 4:.1f}' "
                       f"class='series-label' fill='{colour}'>POLE "
                       f"{esc(code)}</text>")
    out.append("</svg>")
    if hidden:
        out.append(
            f"<p class='count'>{plural(hidden, 'volta', 'voltas')} acima de "
            f"{fmt_lap(high)} ficaram fora do gráfico — preparação, "
            "refrigeração e voltas sob bandeira, que esmagariam a escala.</p>"
        )
    return "".join(out)


def qualifying_table(connection: duckdb.DuckDBPyConnection) -> str:
    """The grid as it was decided, with each segment's time and where it ended."""
    rows = connection.execute(
        """
        SELECT qualifying_position, driver_code, driver_name, team_name,
               q1_seconds, q2_seconds, q3_seconds, best_seconds,
               gap_to_pole_seconds, eliminated_in,
               q1_timed_laps, q2_timed_laps, q3_timed_laps
        FROM gold_qualifying_result ORDER BY qualifying_position
        """
    ).fetchall()
    colours = team_colours(connection)
    bests = [
        connection.execute(
            f"SELECT min({column}) FROM gold_qualifying_result"
        ).fetchone()[0]
        for column in ("q1_seconds", "q2_seconds", "q3_seconds")
    ]

    body = []
    for (position, code, name, team, q1, q2, q3, _best, gap, eliminated,
         laps1, laps2, laps3) in rows:
        cells = []
        for index, (value, laps) in enumerate(
            ((q1, laps1), (q2, laps2), (q3, laps3))
        ):
            if value is None:
                cells.append("<td class='num null'>—</td>")
                continue
            fastest = bests[index] is not None and abs(value - bests[index]) < 0.0005
            hover = tip(
                f"{code} · Q{index + 1}",
                fmt_lap(value),
                (f"Melhor de {plural(int(laps), 'volta cronometrada', 'voltas cronometradas')}"
                 if laps else None),
                f"+{value - bests[index]:.3f} s para o melhor do segmento"
                if not fastest and bests[index] is not None else "Melhor do segmento",
            )
            klass = "num best" if fastest else "num"
            cells.append(f"<td class='{klass}'{hover}>{fmt_lap(value)}</td>")

        klass = f" class='out-q{eliminated}'" if eliminated else ""
        swatch = (f"<i class='swatch' style='background:"
                  f"{colours.get(code, UNKNOWN_COLOUR)}'></i>")
        # A Q1 time and a Q3 time were set on different tracks: in this wet
        # session Q1 ran four seconds slower, so a gap across segments would
        # flatter whoever was knocked out earliest. Only Q3 is compared.
        if q3 is None:
            to_pole = "<span class='muted-cell'>caiu no "
            to_pole += f"Q{eliminated}</span>" if eliminated else "—</span>"
        elif gap == 0:
            to_pole = "pole"
        else:
            to_pole = f"+{gap:.3f}"
        body.append(
            f"<tr{klass}><td class='pos'>{int(position)}</td>"
            f"<td>{swatch}<b>{esc(code)}</b> {esc(name)}</td>"
            f"<td>{esc(team)}</td>{''.join(cells)}"
            f"<td class='num'>{to_pole}</td></tr>"
        )
    return (
        "<table class='data'><thead><tr><th>Pos</th><th>Piloto</th>"
        "<th>Equipe</th><th class='num'>Q1</th><th class='num'>Q2</th>"
        "<th class='num'>Q3</th><th class='num'>Para a pole</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def qualifying_cards(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        """
        SELECT driver_code, driver_name, team_name, best_seconds,
               gap_to_pole_seconds, eliminated_in
        FROM gold_qualifying_result ORDER BY qualifying_position
        """
    ).fetchall()
    cards = []
    if rows:
        cards.append(("🏆", "Pole", f"{rows[0][0]} {fmt_lap(rows[0][3])}"))
    if len(rows) > 1 and rows[1][4] is not None:
        cards.append(("⏱️", "Para o segundo", f"{rows[1][4]:.3f} s"))
    cuts = {}
    for row in rows:
        if row[5]:
            cuts[row[5]] = cuts.get(row[5], 0) + 1
    for segment in sorted(cuts):
        cards.append(("✂️", f"Eliminados no Q{segment}", f"{cuts[segment]}"))
    cards.append(("🧮", "Pilotos", f"{len(rows)}"))
    wet_share = connection.execute(
        "SELECT avg(wet_tyre_share) FROM gold_track_state"
    ).fetchone()[0] if has_table(connection, "gold_track_state") else None
    if wet_share:
        cards.append(("💦", "Pista",
                      "molhada o tempo todo" if wet_share > 0.99
                      else f"molhada em {wet_share * 100:.0f}% das voltas"))
    reds = connection.execute(
        "SELECT count(*) FROM silver_race_control "
        "WHERE upper(message) LIKE '%RED FLAG%'"
    ).fetchone()[0] if has_table(connection, "silver_race_control") else 0
    if reds:
        cards.append(("🟥", "Bandeiras vermelhas", f"{reds}"))
    return "<div class='cards'>" + "".join(
        f"<div class='card'><span class='card-icon'>{icon}</span>"
        f"<span class='card-value'>{esc(value)}</span>"
        f"<span class='card-label'>{esc(label)}</span></div>"
        for icon, label, value in cards
    ) + "</div>"


def qualifying_podium(connection: duckdb.DuckDBPyConnection) -> str:
    rows = connection.execute(
        "SELECT driver_code, driver_name, team_name, best_seconds "
        "FROM gold_qualifying_result ORDER BY qualifying_position LIMIT 3"
    ).fetchall()
    colours = team_colours(connection)
    labels = ("Pole", "2º", "3º")
    return "<div class='podium'>" + "".join(
        f"<div class='step-card' style='border-top-color:"
        f"{colours.get(code, UNKNOWN_COLOUR)}'><span class='medal'>"
        f"{'🏆' if index == 0 else '🥈' if index == 1 else '🥉'}</span>"
        f"<span class='card-value'>{esc(name)}</span>"
        f"<span class='card-label'>{esc(team)} · {labels[index]} · "
        f"{fmt_lap(best)}</span></div>"
        for index, (code, name, team, best) in enumerate(rows)
    ) + "</div>"


def qualifying_teammates(connection: duckdb.DuckDBPyConnection) -> str:
    """The duel, judged in the deepest segment both drivers reached."""
    rows = connection.execute(
        """
        WITH pairs AS (
            SELECT a.team_name, a.driver_code AS fast, b.driver_code AS slow,
                   CASE WHEN a.q3_seconds IS NOT NULL AND b.q3_seconds IS NOT NULL
                             THEN 3
                        WHEN a.q2_seconds IS NOT NULL AND b.q2_seconds IS NOT NULL
                             THEN 2
                        WHEN a.q1_seconds IS NOT NULL AND b.q1_seconds IS NOT NULL
                             THEN 1 END AS segment,
                   a.q1_seconds AS a1, a.q2_seconds AS a2, a.q3_seconds AS a3,
                   b.q1_seconds AS b1, b.q2_seconds AS b2, b.q3_seconds AS b3
            FROM gold_qualifying_result AS a
            JOIN gold_qualifying_result AS b
              ON b.team_name = a.team_name
             AND b.qualifying_position > a.qualifying_position
        )
        SELECT team_name,
               CASE WHEN a_time <= b_time THEN fast ELSE slow END AS quicker,
               CASE WHEN a_time <= b_time THEN slow ELSE fast END AS slower,
               segment,
               least(a_time, b_time)    AS fast_time,
               greatest(a_time, b_time) AS slow_time
        FROM (
            SELECT *,
                   CASE segment WHEN 3 THEN a3 WHEN 2 THEN a2 ELSE a1 END AS a_time,
                   CASE segment WHEN 3 THEN b3 WHEN 2 THEN b2 ELSE b1 END AS b_time
            FROM pairs
        ) WHERE segment IS NOT NULL
        ORDER BY slow_time - fast_time DESC
        """
    ).fetchall()
    colours = team_colours(connection)
    items = []
    for team, fast, slow, segment, fast_time, slow_time in rows:
        gap = slow_time - fast_time
        items.append((
            f"{fast} × {slow}", colours.get(fast, UNKNOWN_COLOUR), abs(gap),
            f"+{gap:.3f} s",
            tip(f"{team} · comparados no Q{segment}",
                f"{fast}: {fmt_lap(fast_time)}",
                f"{slow}: {fmt_lap(slow_time)}",
                f"Último segmento em que os dois cravaram tempo: Q{segment}"),
        ))
    return bar_list(items)


def qualifying_sector_table(connection: duckdb.DuckDBPyConnection) -> str:
    """Best sector of each driver, and the ideal lap they add up to."""
    rows = connection.execute(
        """
        SELECT driver_code, min(sector1_seconds), min(sector2_seconds),
               min(sector3_seconds),
               min(sector1_seconds) + min(sector2_seconds) + min(sector3_seconds)
        FROM silver_laps
        WHERE NOT is_deleted AND NOT is_in_lap AND NOT is_out_lap
        GROUP BY driver_code
        HAVING min(sector1_seconds) IS NOT NULL
        ORDER BY 5
        """
    ).fetchall()
    colours = team_colours(connection)
    return heat_table(
        "Piloto",
        ["Melhor S1", "Melhor S2", "Melhor S3", "Volta ideal"],
        [(row[0], colours.get(row[0], UNKNOWN_COLOUR), list(row[1:]))
         for row in rows],
        higher_is_better=False,
        fmt=lambda value: fmt_lap(value) if value >= 60 else f"{value:.3f}",
        unit="",
    )


def segment_note(connection: duckdb.DuckDBPyConnection) -> str:
    """Say how the segments were worked out, and whether the guess held."""
    rows = connection.execute(
        """
        SELECT s.segment, count(DISTINCT s.driver_code), count(*)
        FROM gold_qualifying_segments AS s GROUP BY s.segment ORDER BY s.segment
        """
    ).fetchall()
    checked, matched, unpublished = connection.execute(
        "SELECT count(*) FILTER (WHERE has_published_times), "
        "sum(matches_published::int) FILTER (WHERE has_published_times), "
        "count(*) FILTER (WHERE NOT has_published_times) "
        "FROM gold_qualifying_result"
    ).fetchone()
    missing = [
        row[0]
        for row in connection.execute(
            "SELECT driver_code FROM gold_qualifying_result "
            "WHERE NOT has_published_times ORDER BY driver_code"
        ).fetchall()
    ]
    shape = ", ".join(
        f"{segment} com {plural(drivers, 'piloto', 'pilotos')} e "
        f"{plural(laps, 'volta', 'voltas')}"
        for segment, drivers, laps in rows
    )
    verdict = (
        f"o melhor tempo de cada um dos {checked} pilotos em cada segmento bateu "
        "com o tempo publicado no resultado, ao milésimo"
        if matched == checked else
        f"<b>{checked - (matched or 0)} piloto(s) não bateram com o tempo "
        "publicado</b>, então trate a divisão por segmento com desconfiança"
    )
    gap = ""
    if unpublished:
        gap = (f" Fica de fora da conferência quem o resultado não traz tempo "
               f"nenhum: {esc(', '.join(missing))} — a cronometragem tem as "
               "voltas, o resultado não as publica.")
    return f"""<p class="note">A cronometragem não diz a qual segmento cada
volta pertence: ela manda voltas soltas. Nós deduzimos olhando a pista
esvaziar — minutos sem ninguém marcando tempo separam um bloco de corrida do
seguinte — e então amarramos cada bloco ao segmento cujos tempos publicados os
pilotos cravaram ali. Numerar os blocos em ordem não serve: uma bandeira
vermelha parte um segmento em dois. Deu {shape}. Para conferir a dedução,
{verdict}.{gap}</p>"""


def tab_qualifying_summary(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""{qualifying_podium(connection)}
{qualifying_cards(connection)}
<h2>Como ficou o grid</h2>
<p class="note">O tempo de cada piloto em cada segmento. A célula roxa é o
melhor daquele segmento; linhas apagadas são quem caiu ali. A última coluna
compara só quem chegou ao Q3: um tempo de Q1 e um de Q3 foram feitos em
pistas diferentes. A diferença dentro de cada segmento está na caixinha de
cada tempo. {HOVER_HINT}</p>
{qualifying_table(connection)}
{segment_note(connection)}"""


def tab_qualifying_laps(connection: duckdb.DuckDBPyConnection) -> str:
    return f"""<h2>Tempo de volta ao longo da sessão</h2>
<p class="note">Cada ponto é uma volta cronometrada, posicionada pelo relógio
da sessão e colorida pelo segmento. É aqui que se vê a pista evoluindo: o
mesmo piloto, meia hora depois, anda vários segundos mais rápido. Círculos
vazados e tracejados são voltas deletadas. {HOVER_HINT}</p>
{qualifying_lap_chart(connection)}
<h2>Duelo entre companheiros</h2>
<p class="note">Comparados no último segmento em que os dois cravaram tempo —
comparar um tempo de Q3 com um de Q1 seria comparar pistas diferentes.
{HOVER_HINT}</p>
{qualifying_teammates(connection)}
<h2>Setores</h2>
<p class="note">O melhor setor de cada piloto na sessão inteira, e a volta
ideal que eles somam. A célula roxa é o melhor da coluna. {HOVER_HINT}</p>
{qualifying_sector_table(connection)}"""

TABS_RACE = (
    ("summary", "🏁 Resumo", tab_summary),
    ("race", "📈 Corrida", tab_race),
    ("pace", "⏱️ Ritmo", tab_pace),
    ("speed", "🚀 Velocidade", tab_speed),
    ("weather", "🌦️ Clima", tab_weather),
    ("control", "🚩 Direção de prova", tab_control),
    ("raw", "🗂️ Dados brutos", tab_raw),
)

TABS_QUALIFYING = (
    ("summary", "🏁 Resumo", tab_qualifying_summary),
    ("laps", "⏱️ Voltas", tab_qualifying_laps),
    ("speed", "🚀 Velocidade", tab_speed),
    ("weather", "🌦️ Clima", tab_weather),
    ("control", "🚩 Direção de prova", tab_control),
    ("raw", "🗂️ Dados brutos", tab_raw),
)


def tabs_for(connection: duckdb.DuckDBPyConnection) -> tuple:
    """A qualifying session is not a short race: it gets its own page."""
    kind = session_profile(connection).get("session_kind")
    return TABS_QUALIFYING if kind == "qualifying" else TABS_RACE


def build_tabs(connection: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
    """Each tab: (slug, label, html). An optional tab is dropped, not emptied."""
    return [
        (slug, label, build(connection))
        for slug, label, build in tabs_for(connection)
        if slug not in TAB_REQUIRES or has_table(connection, TAB_REQUIRES[slug])
    ]


STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 20px 72px; background: #fbfbfa; color: #1a1a19;
       font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif; }
main { max-width: 1040px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 3px; letter-spacing: -0.015em; }
h2 { font-size: 17px; margin: 34px 0 5px; letter-spacing: -0.01em; }
p.sub { margin: 0 0 18px; color: #6b6b68; }
p.story { margin: 3px 0 15px; font-size: 15px; max-width: 86ch;
          color: #33332f; }
p.note { margin: 3px 0 13px; color: #6b6b68; font-size: 13.5px; max-width: 86ch; }
p.warn { margin: 3px 0 14px; padding: 11px 13px; background: #fff8e8;
         border: 1px solid #f0dfae; border-radius: 8px; font-size: 13.5px;
         color: #5c4a12; max-width: 92ch; }
.hint { color: #1d4ed8; white-space: nowrap; }
code { background: #f1f1ed; padding: 1px 4px; border-radius: 3px; font-size: 92%; }
.cards { display: grid; gap: 9px; margin: 4px 0 6px;
         grid-template-columns: repeat(auto-fill, minmax(158px, 1fr)); }
.card { background: #fff; border: 1px solid #e8e8e4;
        border-radius: 10px; padding: 11px 13px; }
.card-icon { display: block; font-size: 19px; line-height: 1.1; margin-bottom: 3px; }
.card-value { display: block; font-size: 18px; font-weight: 650; }
.card-label { display: block; color: #6b6b68; font-size: 12.5px; margin-top: 2px; }
.podium { display: flex; flex-wrap: wrap; gap: 9px; margin: 0 0 9px; }
.step-card { flex: 1 1 200px; background: #fff; border: 1px solid #e8e8e4;
             border-top: 4px solid #999; border-radius: 10px; padding: 11px 14px; }
.medal { font-size: 22px; float: right; }
svg { width: 100%; height: auto; background: #fff; border: 1px solid #e8e8e4;
      border-radius: 10px; }
.tick { font-size: 11px; fill: #85857f; }
.series-label { font-size: 11px; font-weight: 600; }
.series-label.big { font-size: 13px; }
.team-name { font-size: 12.5px; fill: #45453f; }
.value-label { font-size: 12px; font-weight: 600; fill: #1a1a19; }
.value-label .muted { fill: #85857f; font-weight: 500; }
.panel-title { font-size: 12px; font-weight: 600; fill: #45453f; }
.row-label { font-size: 12.5px; fill: #1a1a19; }
.bar-text { font-size: 10.5px; font-weight: 700; fill: #fff; pointer-events: none;
            paint-order: stroke; stroke: rgba(0,0,0,.35); stroke-width: 2px; }
.hoverable:hover { stroke: #1a1a19; stroke-width: 1.5; fill-opacity: 1; }
.column:hover { fill: rgba(26,26,25,.07); }
svg.focus [data-d] { opacity: .1; transition: opacity .08s; }
svg.focus [data-d].on { opacity: 1; }
svg.focus path.on { stroke-width: 3.2; }
svg.focus circle.on[fill-opacity] { r: 4; fill-opacity: 1; }
.tick .strong { font-weight: 700; fill: #1a1a19; }
.range-key { align-items: center; gap: 8px; margin: 0 2px 9px; }
.range-line { display: inline-block; width: 46px; height: 3px; border-radius: 2px;
              background: #b9b9b3; }
.range-dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%;
             background: #1a1a19; }
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
th.num { text-align: right; }
td { padding: 6px 10px; border-bottom: 1px solid #f3f3ef; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.null { text-align: right; color: #c7c7c0; }
td.flag { color: #45453f; }
td.pos { font-weight: 650; width: 44px; }
table.heat td { border-bottom: 2px solid #fff; border-right: 2px solid #fff; }
table.heat td.best { background: #6d28d9; color: #fff; font-weight: 700; }
td.best { background: #6d28d9; color: #fff; font-weight: 700; }
tr.out-q1 td { color: #a3a39c; } tr.out-q1 td.best { color: #fff; }
tr.out-q2 td { color: #74746d; } tr.out-q2 td.best { color: #fff; }
tr.unclassified td { color: #9a9a93; }
.muted-cell { color: #9a9a93; font-size: 11.5px; }
.up { color: #15803d; } .down { color: #b91c1c; }
p.count { margin: 6px 2px 0; font-size: 12px; color: #9a9a93; }
.incidents { display: grid; gap: 9px;
             grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); }
.incident { background: #fff; border: 1px solid #e8e8e4; border-left: 4px solid
            #c7c7c0; border-radius: 10px; padding: 10px 13px; font-size: 13px; }
.incident.penalty { border-left-color: #b91c1c; }
.incident.pending { border-left-color: #d97706; }
.incident.warn { border-left-color: #1a1a19; }
.incident.clear { border-left-color: #15803d; }
.incident-head { font-size: 14px; }
.incident-reason { color: #6b6b68; margin: 1px 0 7px; }
.steps { line-height: 2; }
.step { display: inline-block; padding: 0 7px; border-radius: 999px;
        background: #f1f1ed; font-size: 12px; white-space: nowrap; }
.step.penalty { background: #fee2e2; color: #7f1d1d; }
.step.pending { background: #fef3c7; color: #78350f; }
.step.clear, .step.served { background: #dcfce7; color: #14532d; }
.arrow { color: #b0b0aa; }
.two-col { display: grid; gap: 0 28px;
           grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); }
.bars { background: #fff; border: 1px solid #e8e8e4; border-radius: 10px;
        padding: 9px 13px; }
.bar-row { display: flex; align-items: center; gap: 9px; padding: 3px 0;
           font-size: 12.5px; }
.bar-row:hover { background: #f7f7f4; }
.bar-label { width: 38px; font-weight: 650; }
.bar-track { flex: 1; height: 13px; background: #f3f3ef; border-radius: 3px;
             overflow: hidden; }
.bar-track i { display: block; height: 100%; border-radius: 3px; }
.bar-value { width: 96px; text-align: right; color: #45453f;
             font-variant-numeric: tabular-nums; }
details { background: #fff; border: 1px solid #e8e8e4; border-radius: 10px;
          margin: 8px 0; padding: 0 13px; }
details[open] { padding-bottom: 12px; }
summary { cursor: pointer; padding: 11px 0; font-weight: 600; font-size: 14px; }
footer { margin-top: 44px; padding-top: 15px; border-top: 1px solid #e8e8e4;
         color: #85857f; font-size: 12.5px; }
.empty { color: #85857f; font-style: italic; }
#tip { position: fixed; display: none; z-index: 20; pointer-events: none;
       max-width: 430px; padding: 8px 11px; border-radius: 8px;
       background: #1a1a19; color: #fff; font-size: 12.5px; line-height: 1.5;
       white-space: pre-line; box-shadow: 0 6px 20px rgba(0,0,0,.22); }
#tip b { display: block; font-size: 13px; }
.tabs > input { position: absolute; opacity: 0; pointer-events: none; }
nav.tabbar { display: flex; flex-wrap: wrap; gap: 3px; margin: 2px 0 20px;
             border-bottom: 1px solid #e3e3df; }
nav.tabbar label { padding: 8px 13px; font-size: 13.5px; cursor: pointer;
                   color: #6b6b68; border-bottom: 2px solid transparent;
                   margin-bottom: -1px; user-select: none; }
nav.tabbar label:hover { color: #1a1a19; }
.panels > section { display: none; }
"""

# The page's only script. It reads what Python already wrote into data-tip and
# data-d; it computes nothing, fetches nothing, and the page is complete --
# every chart and every number -- with it switched off.
TOOLTIP_SCRIPT = """
(function () {
  var box = document.getElementById('tip'), lit = null;
  function clear() {
    if (!lit) return;
    lit.svg.classList.remove('focus');
    lit.items.forEach(function (n) { n.classList.remove('on'); });
    lit = null;
  }
  function focus(el) {
    var svg = el.closest('svg.focusable'), d = el.getAttribute('data-d');
    if (!svg || !d) { clear(); return; }
    if (lit && lit.svg === svg && lit.d === d) return;
    clear();
    var items = [];
    svg.querySelectorAll('[data-d]').forEach(function (n) {
      if (n.getAttribute('data-d') === d) { n.classList.add('on'); items.push(n); }
    });
    svg.classList.add('focus');
    lit = { svg: svg, d: d, items: items };
  }
  function show(event) {
    var el = event.target.closest ? event.target.closest('[data-tip]') : null;
    if (!el) { box.style.display = 'none'; clear(); return; }
    var lines = el.getAttribute('data-tip').split('\\n');
    box.textContent = '';
    var head = document.createElement('b');
    head.textContent = lines[0];
    box.appendChild(head);
    box.appendChild(document.createTextNode(lines.slice(1).join('\\n')));
    box.style.display = 'block';
    var x = event.clientX + 14, y = event.clientY + 16;
    if (x + box.offsetWidth > window.innerWidth - 8)
      x = event.clientX - box.offsetWidth - 14;
    if (y + box.offsetHeight > window.innerHeight - 8)
      y = event.clientY - box.offsetHeight - 12;
    box.style.left = Math.max(x, 6) + 'px';
    box.style.top = Math.max(y, 6) + 'px';
    focus(el);
  }
  document.addEventListener('mousemove', show);
  document.addEventListener('click', show);
  document.addEventListener('scroll', function () {
    box.style.display = 'none';
  }, true);
})();
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


def session_title(slug: str) -> str:
    """'2026-sao-paulo-grand-prix-r' -> 'Sao Paulo Grand Prix 2026 — Corrida'."""
    parts = slug.split("-")
    session = SESSION_NAMES.get(parts[-1].lower())
    if session and len(parts) > 2 and parts[0].isdigit():
        event = " ".join(parts[1:-1]).title()
        return f"{event} {parts[0]} — {session}"
    return slug.replace("-", " ").title()


def render(connection: duckdb.DuckDBPyConnection, slug: str) -> str:
    tabs = build_tabs(connection)
    slugs = [tab[0] for tab in tabs]
    title = session_title(slug)

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
<html lang="pt-BR">
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
<p class="sub">Construído a partir da cronometragem bruta. Todo número desta
página sai do warehouse.</p>
<div class="tabs">{radios}<nav class="tabbar">{bar}</nav>
<div class="panels">{panels}</div></div>
<footer>Não oficial. Sem vínculo com a Formula 1. Gerado por
f1-live-data-platform. Arquivo único: abre sem internet.</footer>
</main>
<div id="tip" role="tooltip"></div>
<script id="tooltip">{TOOLTIP_SCRIPT}</script>
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
        scope_to_session(connection, session_slug)
        if not connection.execute("SELECT count(*) FROM silver_laps").fetchone()[0]:
            raise SessionNotInWarehouseError(
                f"{session_slug} is not in {db_path} -- run f1-build for it first"
            )
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
    except (FileNotFoundError, SessionNotInWarehouseError) as error:
        LOGGER.error("%s", error)
        return 1

    LOGGER.info("report written to %s (%.0f KB)", path, path.stat().st_size / 1024)
    return 0


if __name__ == "__main__":
    sys.exit(main())
