# 0005 — A section must earn its place from the data

## Status

Accepted.

## Context

The first real session this pipeline processed was the 2026 São Paulo race:
wet start, dry middle, wet end. The report was written while looking at it,
and the page absorbed that race's shape. Rain became furniture: a card
counting wet laps, a band under the lap chart, a sentence explaining that dry
and wet pace sit on separate scales.

Rendering a synthetic dry copy of the same session showed the cost. Two of
seven headline cards read "0 de 71" and "0 de 79". The band was a uniform
pale stripe, captioned with an explanation of a phenomenon that had not
happened. The pace tab still contrasted two scales while drawing one.

The page was describing the race the code was written for, not the race in
the database.

## Decision

A section appears only when the session's own data gives it something to say.

`gold_session_profile` holds one row per session answering what happened:
laps, pit stops, lead changes, the biggest climb, the winning margin, and how
many laps were wet, neutralised by a safety car, by a virtual safety car or
by a red flag. The report reads it and chooses.

Headline cards split in two. Fixed ones, which every race has: distance,
fastest lap, winning margin, lead changes, biggest climber, pit stops.
Conditional ones, which appear at a count above zero: wet laps, safety car
laps, VSC laps, red flag laps.

The strip under the lap chart generalises from "wet" to "what was the track
doing": one band for the share of the grid on wet-weather tyres, one for
neutralisations, each drawn only if its band has content, and no strip at all
when neither does. Explanatory sentences are assembled from the same facts,
so the caption cannot describe a band that is not there.

## Consequences

- Two synthetic fixtures guard this in CI: a dry copy of the São Paulo laps,
  which must mention no rain anywhere, and a copy carrying safety car codes,
  which must grow the band and the card. Reintroducing the bias fails a test.
- `gold_session_profile` is also the table that will let one session be
  compared with another once more than one is loaded.
- Qualifying will need its own profile columns; this decision covers the
  shape, not yet that session type.
