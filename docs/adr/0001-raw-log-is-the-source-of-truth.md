# ADR 0001 — The raw log is the source of truth

**Status:** accepted
**Date:** 2026-11-13

## Context

The F1 live timing feed is available only while a session is running. Sessions happen
roughly every other weekend, and after the Abu Dhabi Grand Prix on 6 December 2026
there is nothing live until March 2027.

This project is built on a few hours a week. A development cycle that depends on a
live session would be a two-week loop in the best case and a four-month gap in the
worst, which is long enough to kill the project.

The feed is also unofficial. Its message format can change without notice, including
mid-season.

## Decision

Every byte received from the feed is appended to a raw log on disk before anything
parses it. The recorder performs no interpretation of its own, and subscribes to all
20 topics the feed publishes rather than a chosen subset.

The raw log is treated as immutable. Nothing downstream ever writes back to it.

FastF1's own `SignalRClient` does the receiving. Its former `debug` mode, which stored
the complete SignalR envelope rather than the message payload, was removed in FastF1
3.x and now raises on use, so the payload is what gets stored. Writing our own client
to recover the envelope was considered and rejected for phase 0: the envelope carries
transport framing, not race data, and the deadline is the next live session.

## Consequences

**Development stops depending on the calendar.** One recorded session can be replayed
indefinitely. FastF1 supports this natively through `LiveTimingData`, so the replay
path needs no custom format.

**Reprocessing is idempotent.** Changing the parser means replaying the same bytes,
not re-collecting data that no longer exists.

**A format change costs a parser, not the data.** If the feed changes shape, every
session already captured is still complete and can be re-parsed by the new code.

**Storage grows.** Subscribing to every topic costs more disk than picking the few a
feature needs today. This is accepted: disk is cheap and the data is irreplaceable,
and a topic not captured during a session can never be captured again.

## Alternatives considered

*Parse on arrival and store structured records.* Rejected. A parser bug or a format
change would silently destroy data that cannot be collected again, and every parser
change would require a new live session to test against.

*Rely on FastF1's post-session download.* Partially kept — it remains the primary
source for historical work. But it offers nothing to replay for the streaming layer,
and the project has had server-side availability gaps reported in the past, so having
our own copy is the point.
