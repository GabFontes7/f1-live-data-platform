# F1 Live Data Platform

Records the Formula 1 live timing stream into a raw, replayable log, then turns it
into a tested warehouse and a public race replay.

**Status: phase 0 — the recorder.** The next live sessions are the Las Vegas Grand
Prix (19–21 Nov 2026) and Abu Dhabi (4–6 Dec 2026). After that the feed goes quiet
until March, so capturing a session now is the one deadline this project has.

## Why a raw log

Every byte the feed sends is written to disk before anything parses it.

That single decision is what makes the rest of the project possible on a few hours a
week. A recorded session can be replayed on a Tuesday night, so development never
waits for a race weekend. Reprocessing is idempotent: change the parser, replay the
same bytes. And when the feed's format changes — it is unofficial, it will — the cost
is a parser rewrite, not lost data.

See [ADR 0001](docs/adr/0001-raw-log-is-the-source-of-truth.md).

## Quick start

```bash
pip install -e .

# days before a session: confirm the login works
f1-record --check-auth

# during a live session
f1-record --session 2026-las-vegas-fp1

# after a session: bronze, then the warehouse
f1-ingest --year 2026 --event "Sao Paulo Grand Prix" --session R
f1-build --session 2026-sao-paulo-grand-prix-r
pytest -q
```

The last command runs the acceptance test, which rebuilds the FIA's published
classification for that race from the raw data: the podium, the three time
penalties applied after the flag, both unclassified drivers, and the fastest lap.
It runs against the committed fixture in CI and against your own ingested copy
when you have one.

### Authentication

The live timing feed requires a free Formula 1 account. Create one at
[account.formula1.com](https://account.formula1.com), then either let the recorder
prompt you once, or set the credentials so it never asks:

```bash
export F1_USERNAME="you@example.com"   # Windows: $env:F1_USERNAME = "..."
export F1_PASSWORD="..."
```

FastF1 stores the resulting token on disk and reuses it, so a reconnect in the middle
of a session does not prompt again. Run `--check-auth` well before a session: a
credential problem discovered at lights out is a session lost.

`--no-auth` exists as a fallback, but FastF1 warns it may work for only some sessions
or return empty or partial data, which is a poor trade on a session that cannot be
recorded twice.

The recorder writes to `data/raw/<session>/<session>.txt` and reconnects on its own
until you stop it with Ctrl+C. Start it before the session, leave it alone, stop it
when the session ends.

Replaying a recorded session uses FastF1's own reader, so no custom format is
invented here:

```python
from fastf1.livetiming.data import LiveTimingData
import fastf1

livedata = LiveTimingData("data/raw/2026-las-vegas-fp1/2026-las-vegas-fp1.txt")
session = fastf1.get_session(2026, "Las Vegas Grand Prix", "FP1")
session.load(livedata=livedata)
```

## Roadmap

| Phase | What it delivers |
| --- | --- |
| 0 | **Recorder.** Capture a live session into a raw append-only log |
| 1 | Vertical slice: raw → DuckDB → dbt, reproducing an official race classification |
| 2 | Star schema with slowly changing dimensions, published dbt docs |
| 3 | Per-driver post-race analysis: stint-corrected pace, teammate deltas, degradation |
| 4 | Live stream processing: gaps, tyre degradation, undercut windows |
| 5 | Public navigable race replay |
| 6 | Structured extraction from race control messages, with a measured eval set |

Every phase ends shippable. Nothing here costs money to run.

## Data sources

- **[FastF1](https://github.com/theOehrly/Fast-F1)** — primary source for historical
  timing, telemetry, weather and race control, and the live timing recorder used here.
- **[OpenF1](https://github.com/br-g/openf1)** — second, independent source for
  historical sessions from 2023 onward, used to cross-check the numbers derived from
  FastF1. Its free tier covers historical data; real-time access is a paid tier, which
  is why the live path here is our own.

## Disclaimer

Unofficial and unaffiliated with Formula 1. All F1-related trademarks are owned by
Formula One Licensing B.V. Personal and educational use only.
