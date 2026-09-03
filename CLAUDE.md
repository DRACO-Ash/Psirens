# CLAUDE.md — PSIRENS engineering context

Read this file first, every session. Then read `CONTEXT-001.md` (the LEARNED
register of verified facts). Together they are the operating state so no session
starts cold and no hard-won fact is re-derived.

Owner: Ash Higgins, Technical Director, Bluestaq Ltd. Classification: Not
Classified.

## Operating contract (all sessions)

You are the Principal Python Engineer and Quality Architecture Lead for this
repo. Three modes:
- Advisory (default): questions, reviews, trade-offs. Answer first, tight, risks
  early, one clear next step.
- Build: full component or service. Full toolchain and tests.
- Script: portable single-file stdlib-only tooling (probes, retrievers). Trigger
  words: portable, stdlib only, single file, no dependencies, or any UDL/
  Space-Track retrieval task. Standards are in `CONTEXT-001.md` Section 3.

Universal rules, non-negotiable:
- Verify before claiming completion. Evidence over self-report.
- Never fabricate a value, field name, API behaviour, date, or citation. Unknown
  means TBC, re-verify, and name the owner. Ask Ash rather than guess.
- Mark fact, inference and speculation in analytical output.
- State assumptions inline and proceed; stop only for genuine blockers.
- Credentials are never hardcoded, printed, or echoed.
- UK English, no em-dashes, lead with the point, no filler.

## What PSIRENS is

Plotted Satellite Inclination and Raan ENgagement Screening. A geostationary-belt
space-domain-awareness dashboard. It ingests UDL element sets, computes each
object's sub-satellite longitude and inclination at every element-set epoch, and
renders the belt as a longitude vs inclination plot with historic trails,
drift-direction heads, RA (RAAN) bearing needles, and a floating inspector.
Server archetype: FastAPI backend plus a single-file canvas SPA
(`src/psirens/static/index.html`). Deployed on the Bluestaq App Store. Slug
`psirens` (lowercase); display name PSIRENS.

## Current state (repo 1.6.3; DEPLOYED 1.6.2)

Keep these apart. **1.6.2 is the build on the App Store**: uploaded, passed all TEN
pipeline stages including Deploy, status Active. **1.6.3 is committed here but
has never been uploaded**; it fixes the co-planar modal resize (see Open items).
Memory set to 1Gi in the App Store Configuration tab.
Version string lives in TWO places, keep them in step: `src/psirens/main.py`
(`version="..."`) and `pyproject.toml` (`version = "..."`).

Shipped features:
- Dynamic JCO HRR high-interest list pulled from `/udl/notification`; the plot is
  HRR-scoped for REAL data.
- Three views: Real World, Simulation (SIMULATED only, zero REAL), Combined.
- Operator-defined pulls via `POST /api/pull`: a relative window (6h/24h/3d/7d/
  30d chips) or an absolute start/stop scenario range (UTC). SIM pulls skip the
  HRR filter and prune; REAL/combined pull HRR-filtered and pruned.
- Floating inspector with per-neighbour closest approach (SGP4 screen) and
  copyable TLEs; a per-object RA bearing needle on every marker.
- Deep zoom to 0.05deg longitude / 0.01deg inclination with precision-aware axis
  labels.
- Trail and drift sanitised against physically impossible motion (see LEARNED).
- NATIVE UDL TLEs (1.5.0). `line1`/`line2` are captured at ingest, validated
  (shape, both checksums, and the embedded catalogue number against the record's
  own `satNo`) and served verbatim with source provenance, epoch, rev number and
  per-record classification marking. Reconstruction via `sgp4.exporter` is now
  the labelled FALLBACK, used only where no usable native line exists.
- Source selection policy (1.5.0). `TLE_SOURCE_PRIORITY`, default
  `commercial,government,unknown`: the first class holding a fix wins, newest
  epoch within it. `any` collapses to plain newest-fix-wins. Owner decision,
  23 August 2026: newest commercial first.
- Copy-out gate (1.5.0). The copy control is offered only for a plain
  unclassified, caveat-free marking. A proprietary commercial line (`U//PR-...`)
  is displayed with the control withheld and the reason stated. Fail-closed.
- Co-Planar view (1.6.0, reworked 1.6.2). An inspector button opens a modal
  chart of coplanar angle (the mutual inclination between two orbital planes)
  against absolute longitude difference, for every stored object within
  `COPLANAR_HALF_WIDTH_DEG` (default 20) of the primary. Small angle plus small
  longitude separation is the co-orbital / RPO geometry an operator watches for.
  BOTH axes are logarithmic. The coplanar angle went log in 1.6.2 because the
  objects that matter cluster within a degree of zero and a linear axis piled
  them against the origin. A perfectly coplanar object has an angle of exactly
  zero, which no log axis can place: it is clamped to `CO_XMIN` (0.01deg) and
  sits on the left edge, which reads as "coplanar" and is correct. Points are
  labelled with the satellite name. The modal is draggable and resizable, and
  redraws on resize via a ResizeObserver. It closes with the object panel, but
  closing it leaves the panel open. The canvas is absolutely positioned and the
  modal body carries `min-height:0`/`min-width:0`: see the resize note in Open
  items before touching that CSS.

## Pushing to origin

Owner rule, 3 September 2026: once a build has a GREEN TEN-GATE DEPLOY on the
App Store, the repository may be pushed to origin without asking. Before that
milestone, commit locally and leave the push to Ash.

## Golden verify loop (run before ANY packaging or upload)

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
sh simulate-pipeline.sh        # MUST print "SIMULATION GREEN"
```

`simulate-pipeline.sh` reproduces the App Store python template test stage
EXACTLY (install `requirements.txt` only, then `pytest --cov`), then runs local
grep gates that pre-empt SonarQube rules the eslint proxy cannot see. A green
local loop that skips this has failed the platform before. Coverage floor is 80%
(enforced in `pyproject.toml` addopts and in the sim).

The SPA is linted automatically: `simulate-pipeline.sh` extracts the inline
script, runs `node --check`, then eslint with eslint-plugin-sonarjs. That step
carries a CANARY (a deliberate `sonarjs/no-invariant-returns` violation) and
fails if the canary is not flagged, because an eslint run from the wrong
working directory silently ignores the file and reports success. If node is
absent the step prints a loud SKIPPED warning rather than passing quietly.

## App Store deploy contract (understand before packaging)

None of this is fully discoverable from the platform docs; each line cost a build
or upload cycle to learn. Build compliant from the start.
- Container archetype is auto-detected from the `Dockerfile`.
- Read the `PORT` env var (default 8080) and bind `0.0.0.0`. Do NOT set
  `ENV PORT`; the platform injects it.
- Run as non-root numeric user uid 10001. `GET /` and `GET /healthz` (and
  `/readyz`) must return 200 unauthenticated.
- Memory floor is 256Mi; set resources in the Configuration tab, not the
  Dockerfile. PSIRENS needs the headroom and runs at 1Gi.
- Two requirements files: the platform TEST stage runs `pip install -r
  requirements.txt` then pytest, so all test tooling lives there; the Docker
  image installs `requirements-runtime.txt` (lean, no test tooling).
- "App Build — Queued" is normal, not a failure.
- Upload via "Upload New Version" from GitLab `vanguard-engineering/app-store-apps/psirens`.

## SonarQube quality gate (five conditions, all on OVERALL code, 0% violations)

- violations = 0 (bugs, vulnerabilities, code smells); one Blocker drops to E.
- coverage at least 80%; security rating A; reliability rating A; security-review
  rating A (at least 30% of hotspots reviewed).
- On failure the pipeline uploads `sonar-issues.json` and
  `sonar-quality-gate.json` naming the exact condition and file:line. Read those
  first.
- `sonar-project.properties`: `sonar.sources=src` (the SPA `index.html` IS
  analysed for issues); the SPA is excluded from coverage and duplication only.

Rules the local eslint proxy misses, each now gated in `simulate-pipeline.sh`:
`python:S3776` cognitive complexity cap 15 per function; `python:S107` parameter
cap 13 per function (this one failed the 1.5.0 upload on `_elset_dict` at 18;
group related arguments into a value object rather than widening a signature);
prefer `throw error` over `return Promise.reject(error)` inside a `then`
callback (failed the 1.5.2 upload; MEASURED: eslint-plugin-sonarjs does NOT
carry this rule even with all 279 enabled, so only a real scanner or the grep
gate catches it);
FACT, 2 September 2026: 1.5.3 CLEARED Code Quality outright, the first PSIRENS
build to do so. The gates below, each added after the failure that taught it,
are what got it through; keep adding one per rule learned.
`S6643` prefer `globalThis` over `window`; `S6819` prefer the native element over
an ARIA landmark role; prefer `element.dataset.x` over `getAttribute("data-x")`;
HTML a11y landmark labelling; `no-negated-condition`; zero-fraction literals.

Durable fix, still worth doing though no longer urgent now 1.5.3 has passed
clean: run a real `sonar-scanner` against the tenant SonarQube host before
upload. The prior sandbox could not reach it; if
this machine can, wire it and stop the one-rule-per-cycle pattern.

## Architecture (src/psirens/)

- `models.py` — Pydantic models; DataMode enum; `VIEW_MODES` (real/sim/combined);
  Sample and Track shapes; `display_name` (HRR name, then a stored name that is
  not just the id, then the id alone: never the id twice).
- `astro.py` — SGP4-based sub-satellite longitude and `drift_deg_per_day` (with
  the physical-rate guard).
- `config.py` — env-only config (UDL base/path, epoch param, retention, HRR
  cadence, conjunction window, memory-independent tunables).
- `store.py` — atomic JSON store; anti-shrink `merge_samples`; `retain_only`
  (prunes only REAL objects not on the HRR list; keeps non-REAL); elset retention
  (`_retain_elset` newest overall, `_retain_candidates` newest per provider,
  anti-shrink so a pull missing one provider never erases it).
- `sources.py` — `UDLElsetSource`, `ManualElsetSource`, `DemoElsetSource`;
  `_elset_dict` (the retained mean elements plus native `line1`/`line2`, `source`,
  `rev_no`, `mean_motion_dot`, `mean_motion_ddot`, `ephem_type`); `_udl_ts`
  (trailing-Z epoch form); `_record_elset` (files each fix under `elset`, newest
  overall, and `elset_candidates`, newest per provider).
- `tle.py` — native line validation (checksum and satNo cross-check), source
  classification, the selection policy, and the copy-out gate.
- `hrr.py` — dynamic JCO HRR list pull and store. THE NAMING AUTHORITY: a UDL
  record's stored name comes from `origObjectId`, which this tenant leaves
  empty, so it collapses to the catalogue number. `names()` returns satNo ->
  common name for display, tolerant of the key shape and treating a name equal
  to the id as absent. `_normalise_names` guarantees a `name` key at cache load
  because the cache is otherwise read verbatim and the bundled snapshot and a
  raw JCO record disagree on the key.
- `coplanar.py` — coplanar angle from inclination and RAAN, the longitude-
  difference wrap, and `coplanar_for` (the neighbourhood payload). Reads the
  plane from `elset`, the newest fix overall, which is what the plot head and RA
  needle use; it deliberately does NOT go through the TLE selection policy, so
  changing `TLE_SOURCE_PRIORITY` cannot move the plot and the chart apart.
- `conjunction.py` — SGP4 closest-approach screen; `tle_for` (native first,
  reconstruction fallback, with provenance). The element set that is served is
  also the one propagated, so the displayed line always produces the shown range.
- `refresh.py` — background scheduler; `run_once`; `pull_window` (mode + absolute
  or relative window).
- `security.py` — token gate and rate limiting.
- `main.py` — FastAPI app factory, routes, the tracks payload builder.
- `static/index.html` — the single-file canvas SPA (no build step). Version-agnostic
  render pipeline: `transform` (tracks to render objects), `drawTrails`,
  `drawHead` (+ `raNeedle`), the inspector modal, view/window/range controls.

Tests: `tests/test_api.py`, `test_astro.py`, `test_conjunction.py`,
`test_coplanar.py`, `test_drift.py`, `test_hrr.py`, `test_sources.py`,
`test_store.py`, `test_tle.py`.
Offline-safe (demo mode when `UDL_BASE_URL` is unset). TLE fixtures are produced
by the exporter, never hand-typed, so every "valid" line genuinely checksums.

`tools/udl_elset_probe.py` — Script-mode, stdlib-only live probe of `/udl/elset`.
Checks the four assumptions the native-TLE work rests on (line presence, satNo
cross-check, source classification, marking distribution). Needs Ash's
credentials and network; `--self-test` runs offline. NOT part of the deploy zip.

## API contract

- `GET /api/tracks?view=real|sim|combined` — filtered tracks; ETag/304. Each
  track carries `object_id, name, data_mode, classification_marking, source,
  origin, target, samples[], drift_deg_per_day, ra_deg`.
- `GET /api/hrr` — the HRR object map.
- `GET /api/conjunctions?target=<satNo>` — target TLE plus per-neighbour closest
  approach and TLEs.
- `GET /api/coplanar?target=<satNo>` — the primary's plane plus, for each object
  within the half-width, its coplanar angle and longitude difference. 400 when
  `target` is absent.
- `POST /api/pull?mode=<real|sim|combined>&hours=N` OR
  `&start=<ISO>&end=<ISO>` — operator pull; token-gated and rate-limited.
- `POST /api/refresh` — force HRR + REAL refresh.
- `GET /api/meta`, `/healthz`, `/readyz`, `/favicon.ico`, `/static/{name}`.

## Packaging and release

1. Bump the version in BOTH `src/psirens/main.py` and `pyproject.toml`.
2. `sh package-appstore.sh <version>` writes a flat `psirens-appstore-<version>.zip`
   with Dockerfile, both requirements files, pyproject, sonar config, README,
   src, tests at the archive ROOT (no wrapping folder). CLAUDE.md and other dev
   docs are deliberately NOT in the deploy zip.
3. Upload the flat zip via "Upload New Version".

## Conventions

- ruff / black / mypy strict per `pyproject.toml`. pytest at 80% coverage floor.
- No new runtime dependency without a recorded reason. `sgp4==2.27` is the one
  deliberate runtime dependency (validated Vallado propagator).
- The SPA is one file, canvas-rendered, no framework and no build step. Keep every
  JS and Python function under cognitive complexity 15. No `window.` (use
  `globalThis`/`navigator`), no ARIA landmark roles where a native element exists,
  no `getAttribute("data-...")` (use `dataset`), no zero-fraction literals.
- Stores are archive-not-delete; merges are anti-shrink (never blank an existing
  field with a missing one).

## Environment notes for Claude Code

- This repo is now the source of truth. The prior chat sandbox reset its working
  tree each session and rebuilt from a zip; that caveat no longer applies here.
- Live UDL and Space-Track need Ash's credentials
  (`~/.config/phase_offset/credentials.ini`, section `[udl]`) and network. Tests
  do not: they run offline in demo mode. Do not commit credentials.
- Local UI check: seed `DATA_DIR=/tmp/psi/history.json` and `hrr.json`, run
  `DATA_DIR=/tmp/psi PYTHONPATH=src uvicorn psirens.main:app --port 8123`, drive
  with Playwright headless. `UDL_BASE_URL=http://127.0.0.1:1/` prevents the demo
  fallback from merging over a seed.

## Open items and pending decisions

- Native UDL TLEs: SHIPPED in 1.5.1, pending live verification. (1.5.0 was
  rejected by the SonarQube gate on `python:S107` and never deployed.) Built and
  verified offline against exporter-produced fixtures and a seeded store driven
  headlessly. NOT yet checked against live UDL: the build environment is denied
  egress to `unifieddatalibrary.com` by organisation policy (403 on CONNECT) and
  holds no credentials. Run `python3 tools/udl_elset_probe.py --hours 6` on a
  networked workstation before the next upload; it reports each assumption as MET
  or NOT MET and names any provider that falls outside the classification table.
- Conjunction robustness: FIXED in 1.5.2. A malformed target element set used
  to raise out of `/api/conjunctions` as an unhandled 500 with a NON-JSON body,
  which the SPA could only report as "Conjunction service unavailable". The
  neighbour path was always guarded; the target's `build_satrec` was not. This
  predates the native-TLE work (identical in 1.4.14). Now: `tle.is_propagatable`
  filters unusable element sets before selection (so a broken candidate never
  shadows a usable legacy fix), the target build is guarded, every path returns
  JSON with a `detail`, and the SPA checks `response.ok` and renders the stated
  reason instead of a catch-all.
- Object naming: FIXED in 1.6.1. The neighbourhood list and the coplanar legend
  showed the catalogue number as the name ("63157 63157"), because both served
  `obj["name"]` straight from the store while the plot and watchlist already
  joined the HRR list client-side. Now `/api/conjunctions` and `/api/coplanar`
  resolve names server-side via `HrrStore.names()`, so the API is correct for
  any consumer and all five display sites agree. Coplanar CHART POINT labels
  still show the id alone, matching the reference render, with the name in the
  legend beside them; say if you want names on the points too.
- Co-Planar modal resize: FIXED in 1.6.3. The modal resized in one direction
  only and appeared to zoom into the plot, clipping data. Cause: `.comodal-bd`
  is a flex item, so it defaulted to `min-height:auto`, which takes the CANVAS
  ELEMENT'S width/height ATTRIBUTES as a floor. `sizeCo` sets those from the
  measured box on every redraw, so the body could only ratchet larger, never
  smaller; it overflowed the modal and `overflow:hidden` clipped the plot to its
  top slice. Measured: resizing the modal to 800px tall gave a 910px body, and
  shrinking to 420px left the body at 910px. Now the canvas is
  `position:absolute;inset:0` so it contributes no intrinsic size at all, the
  body carries `min-height:0`/`min-width:0`, and the ResizeObserver redraws only
  on a real size change so it cannot feed its own events. LESSON, recorded
  because it nearly slipped: the first check asserted only that the canvas CSS
  box changed, which it always did. A resize check must assert the BACKING STORE
  matches the box AND that the body shrinks as well as grows.
- Co-Planar chart furniture (two observations for Ash, nothing changed). With
  the angle axis now logarithmic the dashed threshold arcs sweep much wider,
  because they are drawn as screen-space ellipses whose intercepts sit at the
  threshold values; on log-log axes that shape is decorative rather than a
  locus. Axis-aligned bands would be geometrically honest. Separately, now that
  every point carries its satellite name the legend largely repeats them and
  can sit over a point. Both are design calls, so they were left alone.
- Copy-out gate breadth (open question for Ash). The gate fails closed on ANY
  caveat, not only `PR`. A record marked `U//DS-...` is therefore displayed but
  not copyable. If DS-caveated records should be copyable, say so and it is a
  one-line change in `tle.is_copyable`.
- Ingest-time sample sanitisation. The physical-rate guard currently lives only
  on the trail/drift display; moving it to ingest would also clean the inspector
  Track-span readout and the head position from one place, and remove the
  duplicated 18deg/day constant in `astro.py` and `index.html`.
- Flaky `/api/pull` test isolation: RESOLVED in 1.4.14. The background scheduler
  raced the pull for the single-flight lock in tests; it is now gated by
  `SCHEDULER_ENABLED` (default True in production, False in the test fixture) and
  a direct scheduler test keeps the loop covered.
- `README.md` predates several features (RA needles, Simulation view, timescale/
  range pull, conjunctions, deep zoom). Refresh when convenient.

## Pointers

- `CONTEXT-001.md` — the LEARNED register (UDL API and notification/HRR, SGP4 TLE
  export and conjunction, drift/trail physical ceilings, the elset native-TLE and
  multi-provider facts) plus the full App Store deploy-contract section. Read it.
- `DEPENDENCY-GATES.md` — the five supply-chain gates (Secret Detection,
  Dependencies, SAST, Dependency Scanning, Container Scan). All have always
  passed, so unlike the SonarQube section it is inference from a passing
  signal, tagged FACT / INFERENCE / TBC. Read it before bumping a dependency
  or touching the Dockerfile base image, and convert its TBC items to FACT
  the first time one of those gates fails.
- `READINESS.md` — pre-submit App Store readiness scoring.
- `pyproject.toml` — tool config and the coverage floor.
