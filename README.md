# PSIRENS

**Plotted Satellite Inclination and Raan ENgagement Screening**

A space-domain-awareness dashboard for the geostationary belt. PSIRENS ingests
UDL element sets, computes each object's sub-satellite longitude and inclination
at every element-set epoch, and renders the belt as a longitude vs inclination
plot: historic trails, drift-direction heads sized by drift rate, and dashed
links to each object's labelled target. It is built as a server-archetype
application for the Bluestaq App Store.

Classification: Not Classified.

---

## What it shows

Each object is plotted at its sub-satellite longitude (x) and inclination (y).

- **Trails** are the object's historic track over the retention window, so a
  relocating satellite draws a long horizontal streak and an inclination
  manoeuvre draws a vertical one.
- **Heads** mark the latest position. A head is a triangle pointing in the
  direction of longitude drift, sized by drift rate, or a dot when on station.
- **Target links** are dashed lines from an object's head to its labelled
  target, whether that target is another tracked object or a fixed longitude
  slot. This is how an inspector or a relocating satellite closing on a slot is
  read at a glance.
- **Colour** encodes the data mode: REAL, SIMULATED, TEST, EXERCISE.

### Hero tabs

- **Real World** shows REAL objects only.
- **Combined (Real + SIM)** shows all four data modes, with per-mode toggle
  chips so you can bring individual modes in and out.

---

## Features

- Ingests UDL element sets across all four data modes (REAL, SIMULATED, TEST,
  EXERCISE).
- Manual temporary element sets: inject an ad-hoc object with an explicit
  target and a time-to-live, added and removed live, expiring on its own.
- Per-object historic trails over a configurable window (90 days by default),
  refreshed hourly.
- Sub-satellite longitude computed with the validated Vallado SGP4 propagator,
  not a hand-rolled approximation.
- Classification banner driven by the records' own markings.
- From-scratch HTML canvas renderer: trails, drift heads, target links, hover
  tooltips, name and NORAD filtering. No heavy front-end dependencies.
- Deterministic offline demo belt for development and testing with no UDL
  access.

---

## Architecture

A FastAPI application behind a pluggable element-set retriever, an on-disk
history store so trails survive restarts, an hourly background refresh, and a
served single-page dashboard that renders the belt client-side.

```
src/psirens/
  main.py       createApp factory, ASGI app, all routes, health, lifespan
  config.py     environment-only configuration and storage-path resolution
  models.py     domain models and boundary validation (Pydantic)
  astro.py      GMST and sub-satellite longitude via SGP4 (validated)
  store.py      atomic JSON store, anti-shrink merge, prune, health probe
  sources.py    ElsetSource protocol: UDL, manual, and demo sources
  refresh.py    single-flight refresh job and the hourly scheduler
  security.py   constant-time token compare, rate limiting, single-flight
  static/       the served single-page dashboard and icons
tests/          astro, store, sources, and in-process API tests
```

---

## Requirements

- Python 3.11 or newer (developed and tested on 3.12 and 3.14).
- Runtime dependencies are pinned in `requirements.txt`. The one non-obvious
  dependency is `sgp4`, the standard Vallado propagator, a deliberate and
  recorded choice over a bespoke propagator.

---

## Quick start

From the project root.

**Windows (PowerShell):**
```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
$env:PYTHONPATH="src"
.venv\Scripts\python.exe -m uvicorn psirens.main:app --port 8080
```

**macOS / Linux:**
```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
PYTHONPATH=src python -m uvicorn psirens.main:app --port 8080
```

Open `http://localhost:8080`. With no UDL variables set, the app draws the
offline demo belt so you can see trails, drift arrows, and the exercise-to-
target link before pointing it at a real tenant.

The test suite is the integrity check: expect 41 passing at roughly 93%
coverage.

---

## Configuration

All configuration is read from the environment. Nothing is committed. For a
code-defaults run the only variables you need are the three UDL settings.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Injected by the platform. Do not set it yourself. |
| `ALLOWED_ORIGIN` | empty | CORS origin. Required if `TEAM_TOKEN` is set. |
| `TEAM_TOKEN` | empty | Bearer token gating state-changing routes. Empty means single-user, writes open. |
| `RETENTION_DAYS` | `90` | Trail window in days. |
| `REFRESH_SECONDS` | `3600` | Refresh cadence. |
| `LON_MIN` / `LON_MAX` | `-180` / `180` | Longitude plot bounds. |
| `INC_MIN` / `INC_MAX` | `0` / `15` | Inclination plot bounds. |
| `DATA_DIR` | `/tmp/psirens-data` | Storage path. The FILE_STORAGE add-on injects `STORAGE_MOUNT_PATH` instead. |
| `UDL_BASE_URL` | empty | UDL base URL. Empty runs the demo belt; set it to enable live pulls. |
| `UDL_USER` / `UDL_PASSWORD` | empty | UDL Basic-auth credentials. |
| `UDL_ELSET_PATH` | `/udl/elset` | Element-set endpoint path. **TBC against tenant.** |
| `UDL_EPOCH_PARAM` | `epoch` | Epoch range query parameter. **TBC against tenant.** |
| `UDL_ACCEPT` | `application/json` | Accept header for the list endpoint. **TBC against tenant.** |
| `UDL_TARGET_FIELD` | `tags` | Which labelled field carries the intended target. **TBC against tenant.** |

### UDL settings are TBC

The verified UDL knowledge base only covers the electro-optical observation
endpoint. Nothing about the element-set endpoint is assumed. Before relying on
live data, confirm the endpoint path, the epoch range syntax, the Accept
header, and above all which labelled field carries the target, against the
tenant, and set the matching variables. Setting `UDL_BASE_URL` turns the demo
belt off, so if a wire setting is wrong the plot is empty; the pod log states
exactly which setting or credential to check.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The dashboard (single-page app). |
| GET | `/healthz` | Liveness plus a real storage-write probe; 503 with errno on failure. |
| GET | `/readyz` | Readiness and last-refresh time. |
| GET | `/api/tracks?view=real\|combined&modes=...` | Object tracks for a view, with ETag and 304. |
| GET | `/api/meta` | Views, cadence, retention, last refresh, manual count. |
| POST | `/api/manual-elset` | Add a temporary manual element set. |
| DELETE | `/api/manual-elset/{id}` | Remove a manual element set. |
| POST | `/api/refresh` | Trigger a refresh now. |
| GET | `/favicon.ico`, `/static/{asset}` | Icons and the web manifest. |

Reads of the belt data are open by design (Not Classified). State-changing
routes are gated by `TEAM_TOKEN` when it is set, and rate limited.

---

## Data sources

- **UDL** (`UDLElsetSource`): pulls element sets over the retention window with
  Basic auth. Wire details are environment-overridable and TBC (see above).
  A UDL failure is never fatal; the store keeps its last-good state.
- **Manual** (`ManualElsetSource`): operator-supplied temporary element sets
  with an explicit target and a TTL. Deleted elsets leave the plot immediately.
- **Demo** (`DemoElsetSource`): a deterministic synthetic belt with station-kept
  anchors, a longitude drifter, a relocating satellite, an inclined-orbit
  object, and an exercise inspector closing on a target. Used offline and in
  tests.

---

## Deployment (Bluestaq App Store)

Package and validate:
```
sh simulate-pipeline.sh 1.0.0   # reproduces the platform test stage
sh package-appstore.sh 1.0.0    # flat upload zip, Dockerfile at the root
```

The container contract: reads `PORT` (default 8080), binds `0.0.0.0`, returns
200 unauthenticated at `/` and `/healthz`, runs as non-root uid 10001, image
flattened to a single layer. The operator Environment Variables tab stays empty
for a code-defaults run, save for the UDL settings.

Deploy sequence: upload the package, set and apply the UDL environment
variables, run the deploy gate, confirm, submit, and watch the pipeline stages.

Persistence: with no storage add-on, data lives in `/tmp` and resets on
restart. Add the FILE_STORAGE add-on for durable trails, which for a non-root
container needs `securityContext.fsGroup` set by an operations request.

Outbound access to the UDL host must be permitted from the deployed pod, or the
app runs healthy but shows an empty plot.

---

## The physics, briefly

Sub-satellite longitude is derived by building an SGP4 satellite record from the
mean elements, propagating to the element-set epoch, and rotating the resulting
position into an Earth-fixed frame by Greenwich Mean Sidereal Time. Both steps
are validated in the tests: GMST against Vallado's worked example, and
sub-satellite longitude against derivable geostationary cases. Longitude drift
rate uses the shortest angular difference so a crossing of the plus or minus 180
degree seam does not register as a spurious jump.

---

## Testing

```
python -m pytest -q
```

Forty-one tests covering the astrodynamics, the store's durability and merge
contracts, the sources including a mocked UDL transport, and the API in-process.
Coverage floor is 80%; the suite runs at roughly 93%. Tests are offline-safe
via the demo source and a mocked HTTP client, so they need no UDL access.

---

## Known limitations

- Storage defaults to ephemeral `/tmp`; trails reset on restart unless the
  FILE_STORAGE add-on is attached.
- The UDL wire settings are TBC until confirmed against the tenant.
- A TTL-expired manual element set stops refreshing but its last samples remain
  in the trail history until an explicit delete or the retention window prunes
  them.
- The container image build is exercised for the first time by the platform's
  build stage; it is not built by the test pipeline.

---

## About

Built for Bluestaq Limited as a bespoke space-domain-awareness capability.
Not Classified.
