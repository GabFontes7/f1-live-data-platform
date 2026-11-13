# ADR 0002 — Retry the network, not the bug

**Status:** accepted
**Date:** 2026-11-13

## Context

The recorder is meant to be started before a session and left alone for two hours. A
network hiccup must not end the recording, so the supervisor reconnects on its own.

The first version treated every failure the same way and reconnected after all of
them. A pre-flight run found what that costs: an argument the installed FastF1 version
no longer accepts raised on construction, the supervisor caught it, slept five
seconds, and raised the identical error again. It did that twenty-four times in two
minutes and would have continued all night. The recording that mattered would have
captured nothing, and the terminal full of identical tracebacks buried the one line
that explained why.

## Decision

The supervisor tells two kinds of failure apart.

A **clean exit** means the client ran and then went quiet. A finished session looks
like this, and so does a silent connection drop. It always reconnects, without limit
unless `--max-reconnects` says otherwise.

An **exception** means the client could not run at all. After
`--max-consecutive-errors` of those in a row (default 3) the recorder stops and says
that this looks like a bug rather than a network problem.

The counter resets on any clean exit, so one bad run between good ones is forgiven.

## Consequences

A misconfiguration is visible in fifteen seconds instead of being buried in a loop.

A genuine outage longer than three reconnects will now end the recording. This is
accepted: a feed that refuses three times in a row, each time by raising rather than
going quiet, is not a feed that is about to start working.

The distinction is testable without a network, because the supervisor takes its runner
as an argument. The tests assert the loop stops, which is the behaviour that failed in
practice.

## Alternatives considered

*Cap total reconnects instead.* Rejected. It would also cap the legitimate case — a
recorder started an hour early, waiting through a silent feed.

*Inspect the exception type and only stop on `ValueError`.* Rejected as too clever. A
broken install, a missing dependency and a bad argument all arrive as different types,
and the pattern worth reacting to is repetition, not identity.
