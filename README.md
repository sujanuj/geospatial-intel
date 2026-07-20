# geospatial-intel

A real-time geospatial intelligence platform: a FastAPI + WebSocket backend
simulates 35 moving objects (ships, aircraft, ground vehicles) and streams
their positions to a dark-theme Leaflet.js command-and-control frontend once
per second.

Built as a portfolio project targeting Forward Deployed Engineer-style roles
— the kind of work that involves real-time data pipelines, live map
visualization, and systems that have to survive contact with actual
production bugs, not just a demo recording.

This README follows the same approach as [`lsmdb`](../lsmdb): real measured
numbers, the actual bugs hit during development, and an honest list of what
this project does *not* do — not a sanitized changelog.

---

## What it does

- Simulates 35 objects — 15 ships, 10 aircraft, 10 ground vehicles — each
  with a position, heading, speed, status (`active` / `warning` / `threat`),
  and a 20-point movement trail.
- Broadcasts every object's updated position to all connected clients once
  per second over a WebSocket (`/ws`).
- Resolves every object's country from its *live* position using offline
  reverse geocoding — not a static label assigned at spawn time.
- Renders everything on a dark, C2-style Leaflet map: live markers, a
  scrolling object list, type/status filters, a detail panel, and a
  threat-count banner.
- Exposes REST endpoints (`/api/objects`, `/api/objects/{id}`, `/api/stats`)
  for anything that doesn't need the live stream.
- Supports a small query DSL for filtering objects by structured criteria
  (`type:ship AND status:threat`) — over REST via `?q=`, and per-connection
  on the live WebSocket stream. See **Development phases, Phase 4**.
- Supports circular geofences with live enter/exit alerting — draw a zone
  on the map, get notified the instant something crosses into or out of
  it. See **Development phases, Phase 5**.

## Stack

- **Backend:** FastAPI, `asyncio`, native WebSockets (`starlette`)
- **Frontend:** Leaflet.js, vanilla JS/HTML/CSS (no build step)
- **Geocoding:** [`reverse_geocoder`](https://pypi.org/project/reverse-geocoder/)
  — offline, ~40k-place k-d tree index, no network call or API key
- **Runtime:** Python 3.14, tested on Apple M5 / macOS

---

## Running it locally

```bash
git clone https://github.com/sujanuj/geospatial-intel.git
cd geospatial-intel
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open `http://localhost:8000`.

### Running the tests

```bash
pip install pytest pytest-asyncio httpx
python3 -m pytest tests/ -v
```

128 tests, covering `simulator.py`'s motion model, status distribution, and
reverse-geocoding integration (`tests/test_simulator.py`), the query DSL
itself — tokenizing, operator precedence, every error path
(`tests/test_query.py`) — geofencing: haversine correctness, containment,
and enter/exit transition detection (`tests/test_geofence.py`) — plus
`main.py`'s REST endpoints, WebSocket behavior, and query/geofence
integration, including regression tests for the `connected_clients`
`UnboundLocalError` bug below (`tests/test_main.py`).

---

## Development phases

### Phase 1 — Core real-time platform

`simulator.py` (in-memory object fleet + motion model), `main.py` (FastAPI
app, WebSocket broadcast loop, REST endpoints), `frontend/index.html`
(Leaflet UI: map, object list, filters, detail panel, threat banner).

### Phase 2 — Correctness pass

Phase 1 ran and looked plausible on first glance, which is exactly when bugs
hide best. Phase 2 was going through it critically and fixing what broke
under scrutiny — see **Bugs found & fixed** below.

### Phase 3 — Test suite and deprecation cleanup

Added a 33-test `pytest` suite covering the correctness issues found in
Phase 2, so they can't silently regress. Also replaced the deprecated
`@app.on_event("startup")` handler with FastAPI's `lifespan` context
manager — functionally identical, but it removed the last `DeprecationWarning`
from the test run and gave the broadcast loop a clean, explicit shutdown
path (`task.cancel()` + `await task`) instead of leaving it to die
mid-iteration on process exit.

### Phase 4 — Query DSL over the object stream

Added a small filter language, built from scratch (tokenizer +
recursive-descent parser + evaluator, no parser library), so objects can be
queried by structured criteria instead of only the fixed type/status toggle
buttons:

```
type:ship AND status:threat
country:Iran OR country:Iraq
speed>500 AND type:aircraft
NOT (status:active) AND altitude>30000
name:"Ocean Pioneer"
```

`:` does a case-insensitive substring match on string fields (`type`,
`status`, `country`, `name`, `id`); `=` and `!=` do exact match; numeric
fields (`speed`, `altitude`, `heading`, `lat`, `lon`) additionally support
`>`, `>=`, `<`, `<=`. `AND`/`OR`/`NOT` combine clauses with standard
precedence (`NOT` binds tightest, then `AND`, then `OR`), and parentheses
override it.

Applies in two places:
- **REST:** `GET /api/objects?q=<query>` — returns a 400 with a specific
  error message (e.g. `unknown field 'bogus' — known fields are: ...`) on
  a malformed query, not a generic 500.
- **WebSocket, per connection:** sending `{"type": "filter", "query":
  "..."}` narrows what *that specific connection* receives on every
  subsequent broadcast tick — two clients can have two different active
  filters at the same time. `{"type": "clear_filter"}` resets to the
  unfiltered stream. The frontend's sidebar Query box drives this.

40 additional tests in `tests/test_query.py` cover the DSL directly
(tokenizing, operator precedence, every error path), and `tests/test_main.py`
gained REST and WebSocket integration tests for the filtering behavior —
83 tests total.

### Phase 5 — Geofencing with live enter/exit alerts

The WebSocket handler had a `geofence` message type since Phase 1, but it
only ever sent back a fixed acknowledgment — no geofence was actually
created or evaluated against anything. This phase made it real.

`geofence.py` implements circular geofences (center + radius, not
arbitrary polygons — this covers the common "alert me within N km of this
point" case without needing a point-in-polygon algorithm or a map-drawing
library). Distance is computed with the haversine formula, implemented
directly rather than pulled from a geo library, and verified against known
real-world distances (NYC↔London ≈ 5570 km, SF↔LA ≈ 560 km) and the
antimeridian wraparound case (two points straddling ±180° longitude
compute as *close*, not as nearly the full circumference apart).

`GeofenceManager` tracks, per (geofence, object) pair, whether that object
was inside that geofence on the *previous* tick. Comparing that against
the current tick is what turns "is X currently inside Y" (a snapshot fact,
cheap to get wrong by re-deriving it naively) into "X just entered/exited
Y" (an actual event) — that comparison is the entire point of the feature.
One deliberate, documented behavior: an object already inside a
geofence the moment it's created fires an immediate "enter" alert, since
there's no prior state to compare against. That's treated as correct, not
a bug — a real alert system should surface "this thing is already in the
zone you just drew" right away, not stay silent because no crossing was
technically observed.

Unlike the query DSL (per-connection), geofences are **global**: creating
or deleting one broadcasts to every connected client immediately via a new
`broadcast_to_all()` helper, outside the normal 1-second tick, since a
geofence represents a real zone of interest everyone watching the map
should see right away. Enter/exit alerts are computed once per tick
against the *unfiltered* object list and included in every client's
message (filtered or not) — an object shouldn't need to match someone's
personal query filter to trigger a geofence alert; the two features are
independent.

New surface area:
- `POST /api/geofences`, `GET /api/geofences`, `DELETE /api/geofences/{id}`,
  `GET /api/alerts` (rolling log of the most recent enter/exit events).
- WebSocket `{"type": "geofence", "action": "create"|"delete"|"list", ...}`.
- Frontend: a "Draw Geofence" button — click it, then click the map to
  place a center, set a name and radius, confirm. Active geofences render
  as dashed circles on the map; a new "Recent Alerts" panel in the sidebar
  shows a live-updating log of enter/exit events as they happen.

35 new tests in `tests/test_geofence.py` (haversine correctness,
containment, the full enter→stay→exit→stay transition sequence, every
validation error path) plus REST and WebSocket integration tests in
`tests/test_main.py` — 128 tests total.

---

## Bugs found & fixed

Real bugs hit during development, in the order they surfaced. Kept here
instead of squashed out of the commit history, because "how you found and
fixed it" is more informative than "it works."

### 1. Stale import, wrong dependency (`dotenv` / `app.redis_client`)

First run of `main.py` failed on `ModuleNotFoundError: No module named
'dotenv'`. Fixed with `pip install python-dotenv`. Second run failed again,
this time on `from app.redis_client import get_redis, close_redis` —
`ModuleNotFoundError: No module named 'app'`. That import pointed at a
Redis-backed module that was never created; the file on disk didn't match
the version that was actually meant to ship. Root cause was a stale local
copy, not a missing dependency — swapping in the correct `main.py` (no
Redis, no `dotenv`, fully self-contained) resolved it. Lesson: a
`ModuleNotFoundError` on your *own* package (`app.*`) is a different failure
mode than a missing third-party package, and worth diagnosing separately
before reaching for `pip install`.

### 2. `UnboundLocalError` in the broadcast loop

```python
connected_clients -= dead
```

inside `broadcast_updates()` threw `UnboundLocalError: cannot access local
variable 'connected_clients'` — on a line that only *reads* the variable
(`if connected_clients:`), several lines above the offending statement.
Cause: Python decides a name is local to a function based on whether it's
assigned *anywhere* in that function body, regardless of execution order.
`-=` rebinds the name, which made `connected_clients` local for the entire
function, breaking the earlier read of what was meant to be the module-level
set. Fixed by mutating in place instead of rebinding:

```python
connected_clients.difference_update(dead)
```

### 3. Markers clustering into a handful of visible pins

Report: only ~6 markers visible on a map meant to hold 35 objects. Two
compounding causes, neither of which was in the marker-rendering code
itself:

- `map.panTo()` on object selection recenters the view without adjusting
  zoom, so panning far enough (e.g. to a US-based object at `zoom: 3`) can
  push entire continents off-screen. Fixed by adding `worldCopyJump: true`
  to the Leaflet config, so panning stays within one continuous world
  view instead of stranding markers on a disconnected duplicate copy.
- Vehicles were jittered only `±0.5°` (~55km) around 8 shared city start
  points — invisible at world zoom, so 10 vehicles rendered as a couple of
  overlapping pins. Widened jitter ranges (ships `±5°→±8°`, aircraft
  `±10°→±15°`, vehicles `±0.5°→±3°`) so objects spread out distinctly
  without landing implausibly far from their named region.

### 4. Country label didn't match position

Each object's `country` field was picked with `random.choice(COUNTRIES)` —
fully independent of where the object actually was. A ship at 37°N/120°W
(Central California) could be labeled `India`. Fixed properly, not
cosmetically: wired in `reverse_geocoder` (offline, no API key) to resolve
every object's real country from its live lat/lon. Verified against known
coordinates, e.g. a ship at `51.25°, 23.26°` (Persian Gulf) correctly
resolves to `UAE`; one at `41.43°, 1.51°` (near Barcelona) resolves to
`Spain`.

Known tradeoff, documented rather than hidden: `reverse_geocoder` always
returns the *nearest populated place*, even over open ocean — a ship in the
open Indian Ocean gets labeled with whatever coastal country/city is
geographically closest, not a maritime "no country" state. That's an
approximation, not authoritative maritime boundary data. Country is also
only refreshed every 5 broadcast ticks (not every tick): the geocode call is
synchronous CPU work (~50ms for all 35 objects after a one-time ~0.6s index
load), and running it every second would block the asyncio event loop for a
noticeable fraction of each broadcast cycle. Position updates every tick;
country updates every 5 ticks — a deliberate accuracy/performance tradeoff,
not an oversight.

### 5. 1000x unit-conversion bug — objects were effectively motionless

The most consequential bug, and the least visually obvious one, because
*something* was moving — just not by a measurable amount. Original code:

```python
speed_deg_per_sec = obj.speed * 0.000514 / 111000  # knots to deg/s
```

`0.000514` correctly converts knots to km/s, but `111000` is meters per
degree, not km per degree (which is `111`) — a 1000x error. This was masked
by three different, undocumented per-type multipliers (`*100` for ships,
`*50` for aircraft, `*20` for vehicles) that made objects move at *some*
nonzero rate, just not one derived from anything. Measured before the fix,
a 15-knot ship moved **0.0000008° per tick** — about 9 centimeters per
second on the map. Invisible on any human timescale.

Fixed by correcting the unit conversion and replacing the three ad hoc
multipliers with one documented `SIM_SPEED_MULTIPLIER = 30` applied
uniformly across all object types, so relative speed differences between
ships/aircraft/vehicles now reflect their real-world ratios instead of
arbitrary per-type tuning. Measured after the fix, on the same simulator
instance:

| Type      | Speed (measured) | Distance per 1s tick (measured) |
|-----------|-------------------|----------------------------------|
| Ship      | 16.3 kt           | 0.32 km                         |
| Aircraft  | 400.4 kt          | 10.17 km                        |
| Vehicle   | 47.4 mph          | 0.90 km                         |

All measured directly from `simulator.py`, not estimated.

---

## Known limitations

- **Objects aren't constrained to realistic domains.** Ships can drift over
  land, vehicles can drift into ocean — there's no coastline or road-network
  constraint on movement. Widening the jitter ranges in bug #3 made this
  slightly more visible, not less.
- **Status distribution has normal sampling variance.** `random_status()`
  targets a 70% active / 20% warning / 10% threat split, and each tick also
  gives every object a 1% independent chance to re-roll. Over a short
  session, seeing e.g. 9/35 objects flagged as threats (26%, vs. the
  expected ~10%) is within normal variance for 35 independent draws, not
  necessarily a bug — worth checking against a longer run before assuming
  the distribution is broken.
- **Tests share global state across a run, matching production design.**
  `simulator` and `connected_clients` in `main.py` are module-level
  singletons, not dependency-injected — same as the real running app. The
  `pytest` suite (`tests/`) inherits that: tests aren't fully isolated from
  each other the way they'd be with a fresh app instance per test, and
  tests that need a clean slate reset the relevant global explicitly rather
  than getting isolation for free. This is a deliberate fidelity tradeoff,
  not an oversight — but it does mean test order could theoretically matter
  in ways it wouldn't with a properly injected simulator instance.
- **Per-client query filtering is O(clients) per broadcast tick.** Each
  connection with an active filter gets its own `filter_objects()` call and
  its own serialized JSON message every second, instead of one shared
  broadcast. Fine at demo scale (a handful of connections); would need
  batching or a smarter diffing strategy to stay cheap with many concurrent
  filtered clients.
- **The query DSL's `!=` doesn't compose intuitively with `:` on multi-value
  fields.** `country!=Iran` excludes exact matches on "Iran" but a value
  like `country:"North Korea"` still needs quoting for the multi-word case
  — there's no negated-substring operator, so "doesn't contain" isn't
  directly expressible, only "isn't exactly equal to."
- **Geofences are circles only, not arbitrary polygons.** A real restricted
  zone (a country's borders, a coastline, an airport's actual footprint)
  is essentially never a perfect circle. This is a deliberate scope
  tradeoff — a circle needs only a center and radius, and covers the
  common "alert me within N km of this point" case, but it can't represent
  an irregular boundary. Proper polygon support would need a
  point-in-polygon algorithm and a map-drawing UI (e.g. Leaflet.draw)
  instead of a single map click.
- **Geofence alert delivery isn't guaranteed if a client is briefly
  disconnected.** Alerts are pushed live during the broadcast tick in
  which they occur; a client that's mid-reconnect at that exact moment
  won't retroactively receive it, though `GET /api/alerts` does keep a
  200-entry rolling log any client can poll to catch up.
- **Single-process, in-memory state.** All 35 objects live in one Python
  process's memory. There's no persistence, no multi-instance broadcast
  fan-out (e.g. via Redis pub/sub), and restarting the server resets the
  entire fleet to new random start positions. Fine for a demo; would need
  real architecture work to run as an actual multi-user service.
- **Reverse geocoding is nearest-place, not boundary-accurate.** See bug #4
  above — it's a reasonable approximation for a demo, not authoritative
  geospatial data.

## Project structure

```
geospatial-intel/
├── main.py              # FastAPI app, WebSocket broadcast loop, REST endpoints
├── simulator.py          # Object fleet, motion model, reverse geocoding
├── query.py               # Query DSL: tokenizer, parser, evaluator
├── geofence.py             # Circular geofences, haversine distance, enter/exit alerting
├── frontend/
│   └── index.html        # Leaflet map UI (map, list, filters, query box, geofence drawing, alert log, detail panel)
├── tests/
│   ├── test_simulator.py
│   ├── test_query.py
│   ├── test_geofence.py
│   └── test_main.py
├── pytest.ini
├── requirements.txt
└── README.md
```(`tests/test_query.py`) — plus `main.py`'s REST endpoints, WebSocket
behavior, and query-filter integration, including regression tests for the
`connected_clients` `UnboundLocalError` bug below (`tests/test_main.py`).

---

## Development phases

### Phase 1 — Core real-time platform

`simulator.py` (in-memory object fleet + motion model), `main.py` (FastAPI
app, WebSocket broadcast loop, REST endpoints), `frontend/index.html`
(Leaflet UI: map, object list, filters, detail panel, threat banner).

### Phase 2 — Correctness pass

Phase 1 ran and looked plausible on first glance, which is exactly when bugs
hide best. Phase 2 was going through it critically and fixing what broke
under scrutiny — see **Bugs found & fixed** below.

### Phase 3 — Test suite and deprecation cleanup

Added a 33-test `pytest` suite covering the correctness issues found in
Phase 2, so they can't silently regress. Also replaced the deprecated
`@app.on_event("startup")` handler with FastAPI's `lifespan` context
manager — functionally identical, but it removed the last `DeprecationWarning`
from the test run and gave the broadcast loop a clean, explicit shutdown
path (`task.cancel()` + `await task`) instead of leaving it to die
mid-iteration on process exit.

### Phase 4 — Query DSL over the object stream

Added a small filter language, built from scratch (tokenizer +
recursive-descent parser + evaluator, no parser library), so objects can be
queried by structured criteria instead of only the fixed type/status toggle
buttons:

```
type:ship AND status:threat
country:Iran OR country:Iraq
speed>500 AND type:aircraft
NOT (status:active) AND altitude>30000
name:"Ocean Pioneer"
```

`:` does a case-insensitive substring match on string fields (`type`,
`status`, `country`, `name`, `id`); `=` and `!=` do exact match; numeric
fields (`speed`, `altitude`, `heading`, `lat`, `lon`) additionally support
`>`, `>=`, `<`, `<=`. `AND`/`OR`/`NOT` combine clauses with standard
precedence (`NOT` binds tightest, then `AND`, then `OR`), and parentheses
override it.

Applies in two places:
- **REST:** `GET /api/objects?q=<query>` — returns a 400 with a specific
  error message (e.g. `unknown field 'bogus' — known fields are: ...`) on
  a malformed query, not a generic 500.
- **WebSocket, per connection:** sending `{"type": "filter", "query":
  "..."}` narrows what *that specific connection* receives on every
  subsequent broadcast tick — two clients can have two different active
  filters at the same time. `{"type": "clear_filter"}` resets to the
  unfiltered stream. The frontend's sidebar Query box drives this.

40 additional tests in `tests/test_query.py` cover the DSL directly
(tokenizing, operator precedence, every error path), and `tests/test_main.py`
gained REST and WebSocket integration tests for the filtering behavior —
83 tests total.

---

## Bugs found & fixed

Real bugs hit during development, in the order they surfaced. Kept here
instead of squashed out of the commit history, because "how you found and
fixed it" is more informative than "it works."

### 1. Stale import, wrong dependency (`dotenv` / `app.redis_client`)

First run of `main.py` failed on `ModuleNotFoundError: No module named
'dotenv'`. Fixed with `pip install python-dotenv`. Second run failed again,
this time on `from app.redis_client import get_redis, close_redis` —
`ModuleNotFoundError: No module named 'app'`. That import pointed at a
Redis-backed module that was never created; the file on disk didn't match
the version that was actually meant to ship. Root cause was a stale local
copy, not a missing dependency — swapping in the correct `main.py` (no
Redis, no `dotenv`, fully self-contained) resolved it. Lesson: a
`ModuleNotFoundError` on your *own* package (`app.*`) is a different failure
mode than a missing third-party package, and worth diagnosing separately
before reaching for `pip install`.

### 2. `UnboundLocalError` in the broadcast loop

```python
connected_clients -= dead
```

inside `broadcast_updates()` threw `UnboundLocalError: cannot access local
variable 'connected_clients'` — on a line that only *reads* the variable
(`if connected_clients:`), several lines above the offending statement.
Cause: Python decides a name is local to a function based on whether it's
assigned *anywhere* in that function body, regardless of execution order.
`-=` rebinds the name, which made `connected_clients` local for the entire
function, breaking the earlier read of what was meant to be the module-level
set. Fixed by mutating in place instead of rebinding:

```python
connected_clients.difference_update(dead)
```

### 3. Markers clustering into a handful of visible pins

Report: only ~6 markers visible on a map meant to hold 35 objects. Two
compounding causes, neither of which was in the marker-rendering code
itself:

- `map.panTo()` on object selection recenters the view without adjusting
  zoom, so panning far enough (e.g. to a US-based object at `zoom: 3`) can
  push entire continents off-screen. Fixed by adding `worldCopyJump: true`
  to the Leaflet config, so panning stays within one continuous world
  view instead of stranding markers on a disconnected duplicate copy.
- Vehicles were jittered only `±0.5°` (~55km) around 8 shared city start
  points — invisible at world zoom, so 10 vehicles rendered as a couple of
  overlapping pins. Widened jitter ranges (ships `±5°→±8°`, aircraft
  `±10°→±15°`, vehicles `±0.5°→±3°`) so objects spread out distinctly
  without landing implausibly far from their named region.

### 4. Country label didn't match position

Each object's `country` field was picked with `random.choice(COUNTRIES)` —
fully independent of where the object actually was. A ship at 37°N/120°W
(Central California) could be labeled `India`. Fixed properly, not
cosmetically: wired in `reverse_geocoder` (offline, no API key) to resolve
every object's real country from its live lat/lon. Verified against known
coordinates, e.g. a ship at `51.25°, 23.26°` (Persian Gulf) correctly
resolves to `UAE`; one at `41.43°, 1.51°` (near Barcelona) resolves to
`Spain`.

Known tradeoff, documented rather than hidden: `reverse_geocoder` always
returns the *nearest populated place*, even over open ocean — a ship in the
open Indian Ocean gets labeled with whatever coastal country/city is
geographically closest, not a maritime "no country" state. That's an
approximation, not authoritative maritime boundary data. Country is also
only refreshed every 5 broadcast ticks (not every tick): the geocode call is
synchronous CPU work (~50ms for all 35 objects after a one-time ~0.6s index
load), and running it every second would block the asyncio event loop for a
noticeable fraction of each broadcast cycle. Position updates every tick;
country updates every 5 ticks — a deliberate accuracy/performance tradeoff,
not an oversight.

### 5. 1000x unit-conversion bug — objects were effectively motionless

The most consequential bug, and the least visually obvious one, because
*something* was moving — just not by a measurable amount. Original code:

```python
speed_deg_per_sec = obj.speed * 0.000514 / 111000  # knots to deg/s
```

`0.000514` correctly converts knots to km/s, but `111000` is meters per
degree, not km per degree (which is `111`) — a 1000x error. This was masked
by three different, undocumented per-type multipliers (`*100` for ships,
`*50` for aircraft, `*20` for vehicles) that made objects move at *some*
nonzero rate, just not one derived from anything. Measured before the fix,
a 15-knot ship moved **0.0000008° per tick** — about 9 centimeters per
second on the map. Invisible on any human timescale.

Fixed by correcting the unit conversion and replacing the three ad hoc
multipliers with one documented `SIM_SPEED_MULTIPLIER = 30` applied
uniformly across all object types, so relative speed differences between
ships/aircraft/vehicles now reflect their real-world ratios instead of
arbitrary per-type tuning. Measured after the fix, on the same simulator
instance:

| Type      | Speed (measured) | Distance per 1s tick (measured) |
|-----------|-------------------|----------------------------------|
| Ship      | 16.3 kt           | 0.32 km                         |
| Aircraft  | 400.4 kt          | 10.17 km                        |
| Vehicle   | 47.4 mph          | 0.90 km                         |

All measured directly from `simulator.py`, not estimated.

---

## Known limitations

- **Objects aren't constrained to realistic domains.** Ships can drift over
  land, vehicles can drift into ocean — there's no coastline or road-network
  constraint on movement. Widening the jitter ranges in bug #3 made this
  slightly more visible, not less.
- **Status distribution has normal sampling variance.** `random_status()`
  targets a 70% active / 20% warning / 10% threat split, and each tick also
  gives every object a 1% independent chance to re-roll. Over a short
  session, seeing e.g. 9/35 objects flagged as threats (26%, vs. the
  expected ~10%) is within normal variance for 35 independent draws, not
  necessarily a bug — worth checking against a longer run before assuming
  the distribution is broken.
- **Tests share global state across a run, matching production design.**
  `simulator` and `connected_clients` in `main.py` are module-level
  singletons, not dependency-injected — same as the real running app. The
  `pytest` suite (`tests/`) inherits that: tests aren't fully isolated from
  each other the way they'd be with a fresh app instance per test, and
  tests that need a clean slate reset the relevant global explicitly rather
  than getting isolation for free. This is a deliberate fidelity tradeoff,
  not an oversight — but it does mean test order could theoretically matter
  in ways it wouldn't with a properly injected simulator instance.
- **Per-client query filtering is O(clients) per broadcast tick.** Each
  connection with an active filter gets its own `filter_objects()` call and
  its own serialized JSON message every second, instead of one shared
  broadcast. Fine at demo scale (a handful of connections); would need
  batching or a smarter diffing strategy to stay cheap with many concurrent
  filtered clients.
- **The query DSL's `!=` doesn't compose intuitively with `:` on multi-value
  fields.** `country!=Iran` excludes exact matches on "Iran" but a value
  like `country:"North Korea"` still needs quoting for the multi-word case
  — there's no negated-substring operator, so "doesn't contain" isn't
  directly expressible, only "isn't exactly equal to."
- **Single-process, in-memory state.** All 35 objects live in one Python
  process's memory. There's no persistence, no multi-instance broadcast
  fan-out (e.g. via Redis pub/sub), and restarting the server resets the
  entire fleet to new random start positions. Fine for a demo; would need
  real architecture work to run as an actual multi-user service.
- **Reverse geocoding is nearest-place, not boundary-accurate.** See bug #4
  above — it's a reasonable approximation for a demo, not authoritative
  geospatial data.

## Project structure

```
geospatial-intel/
├── main.py              # FastAPI app, WebSocket broadcast loop, REST endpoints
├── simulator.py          # Object fleet, motion model, reverse geocoding
├── query.py               # Query DSL: tokenizer, parser, evaluator
├── frontend/
│   └── index.html        # Leaflet map UI (map, list, filters, query box, detail panel)
├── tests/
│   ├── test_simulator.py
│   ├── test_query.py
│   └── test_main.py
├── pytest.ini
├── requirements.txt
└── README.md
```
