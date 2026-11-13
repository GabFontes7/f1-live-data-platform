# 0006 — Count position changes, not overtakes

## Status

Accepted. Extends 0003 (measure the trend, don't name it degradation).

## Context

"How many overtakes were there" is one of the first questions anyone asks of
a race, and the data looks like it answers it: `silver_laps` carries a
position for every driver on every lap, so a car that was behind and is now
ahead has passed someone.

Measuring it on the 2026 São Paulo race gives 219 such swaps. Of those, 151
involve a car on an in-lap or an out-lap: the swap is a pit cycle, not a
pass. A number published as "overtakes" would have been wrong by a factor of
three.

Three more limits survive even after filtering pit laps:

* position in `silver_laps` is FastF1's reconstruction, derived by sorting
  each lap by the time it was set, not a figure the timing feed published;
* the comparison is between two end-of-lap snapshots, so a pass that is
  handed back within the same lap is invisible, and so is a pass followed by
  a re-pass;
* the first lap has no previous lap to compare against, so places taken at
  the start are a different measurement, read from the grid.

Formula 1's own overtake counts are built from positional GPS data, with
rules about which changes count.

## Decision

The table is `gold_position_changes`, the column is `is_on_track`, and the
page says "trocas de posição em pista" with the pit-cycle share stated in
the text. The word "ultrapassagem" appears only to say that this is not it.

`gold_overtake_aid` carries the 2026 overtake aid's state forward lap by lap
from the race control messages, so the page can show which laps had it. The
page reports passes per lap with the aid on and off, and states plainly that
the two periods also differ in track conditions, so the comparison does not
yet separate the aid from the weather.

## Consequences

- A real overtake count needs the position data FastF1 already downloads and
  this pipeline discards. Ingesting it is the upgrade path, and the same data
  draws the circuit map.
- The aid comparison becomes a genuine question across a season rather than
  one race. The profile table makes that comparison possible once more than
  one race is loaded.
