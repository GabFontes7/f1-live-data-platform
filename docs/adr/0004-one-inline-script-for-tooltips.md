# 0004 — One inline script, for tooltips only

## Status

Accepted.

## Context

The report was first built with no JavaScript at all: CSS tabs, SVG written by
Python. That made a strong promise — the file opens offline and can be emailed.

Then the report was read by its first real user. A dot on the lap-time chart
is one of 1,256; without a way to ask "whose lap is this, which lap, what
time", the chart shows a shape and answers no question. The same was true of
the position lines and the strategy bars. SVG's native `<title>` tooltip was
considered: it needs no script, but it appears after a delay, cannot be styled
or span several lines reliably, and cannot highlight one driver's line among
twenty-two.

## Decision

The page carries exactly one inline script. It does two things: shows the text
Python already wrote into `data-tip`, and dims every mark whose `data-d` is
not the hovered driver. It computes nothing and fetches nothing.

What the "no JavaScript" rule was protecting is kept, and is what the tests
now assert: a single file, no external asset, no network request, and every
chart and number present in the HTML with the script disabled.

## Consequences

- The promise changes from "no JavaScript" to "self-contained": one file,
  nothing fetched. The test checks one `<script`, no `src=`, no URL.
- Any number shown in a tooltip is formatted in Python from the warehouse,
  like every other number on the page. The script must never derive a figure.
- A second script, or a library, needs a new decision record.
