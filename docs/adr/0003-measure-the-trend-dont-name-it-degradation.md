# ADR 0003 — Measure the trend, don't name it degradation

**Status:** accepted
**Date:** 2026-11-13

## Context

Comparing driver pace needs two decisions before any number is produced:
which laps count, and what the lap-time slope within a stint actually measures.

Both were tested against the 2026 São Paulo Grand Prix, and the race answered
them more usefully than reasoning would have. It was wet: 41 of 79 stints ran
intermediates, and the winner's first two stints both show lap times *falling*
as the tyre aged.

## Decision

### Which laps count

A lap is representative only when `TrackStatus` is exactly `'1'` — green from
start to finish — and the lap is not deleted, not an in-lap, and not an out-lap.

`TrackStatus` concatenates every status seen during the lap, so `'12'` means a
yellow appeared partway. FastF1's own accuracy check accepts `'1'`, `'2'`,
`'12'` and `'21'` and describes itself as "slightly paranoid". Pace measurement
is stricter than lap validity: a yellow costs time in the sector it covers, so a
lap that touched one cannot be compared to one that did not.

In São Paulo this keeps 1,256 of 1,435 laps.

### What the slope measures

The slope of lap time against tyre age is reported as
`lap_time_trend_s_per_lap`, and deliberately not as degradation, because it is
the sum of at least three effects:

* **tyre wear**, which makes laps slower — the only one the word degradation implies;
* **fuel burn**, which makes every car quicker as the tank empties and never stops;
* **track evolution**, which on a drying track is larger than both.

Separating them needs a fuel-correction coefficient this pipeline has no
evidence for. So it reports the total it can measure and names it accordingly.
`trend_is_negative` marks stints where the tyre was not the dominant term; in
São Paulo that is 41 of 78, exactly the wet stints.

### When a teammate delta means anything

The delta carries `is_comparable`, false when one driver ran less than 70% of
the other's representative laps.

Stroll retired on lap 30 and his delta to Alonso reads −10.032s. The arithmetic
is right and the interpretation would be wrong: it measures a sick car, not a
slower driver. Flagging it is better than dropping it, because the row is true
and the reader deserves to see why it is not usable.

## Consequences

Numbers this pipeline publishes are narrower than the ones a confident
pipeline would publish, and they survive being questioned.

A stint of fewer than four representative laps reports its lap count and
nothing else. A slope fitted to three laps is noise with a decimal point.

Fuel correction stays an open question. When it is added it belongs in its own
column beside the raw trend, not folded into it, so the measurement and the
assumption never become indistinguishable.

## Alternatives considered

*Accept yellow-flag laps, as FastF1's accuracy check does.* Rejected. That
check answers "is this lap time real?", which is a different question from
"can this lap be compared?".

*Apply a standard fuel-correction coefficient from the literature.* Rejected
for now. The figure varies by circuit and car, and a borrowed constant would
turn a measurement into an assumption wearing a measurement's name — which is
the exact failure this ADR exists to avoid.

*Drop non-comparable teammate rows.* Rejected. A reader who cannot see
Stroll's row cannot see why it was excluded.
