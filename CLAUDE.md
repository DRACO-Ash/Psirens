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

## Current state (build 1.4.14)

Deployed and live. Memory set to 1Gi in the App Store Configuration tab.
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

Known limitation being addressed: the TLEs shown are currently RECONSTRUCTED from
stored mean elements via `sgp4.exporter`. UDL actually carries native `line1`/
`line2` on ~100% of elset records; wiring those in is the top pending task (see
Open items).

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

If you edited the SPA (`src/psirens/static/index.html`), also syntax-check and
lint the extracted script (`node --check`, eslint with eslint-plugin-sonarjs) and
rely on the grep gates below.

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

Rules the local eslint proxy misses, each now grep-gated in
`simulate-pipeline.sh`: `python:S3776` cognitive complexity cap 15 per function;
`S6643` prefer `globalThis` over `window`; `S6819` prefer the native element over
an ARIA landmark role; prefer `element.dataset.x` over `getAttribute("data-x")`;
HTML a11y landmark labelling; `no-negated-condition`; zero-fraction literals.

Durable fix, still open and highest-ROI: run a real `sonar-scanner` against the
tenant SonarQube host before upload. The prior sandbox could not reach it; if
this machine can, wire it and stop the one-rule-per-cycle pattern.

## Architecture (src/psirens/)

- `models.py` — Pydantic models; DataMode enum; `VIEW_MODES` (real/sim/combined);
  Sample and Track shapes.
- `astro.py` — SGP4-based sub-satellite longitude and `drift_deg_per_day` (with
  the physical-rate guard).
- `config.py` — env-only config (UDL base/path, epoch param, retention, HRR
  cadence, conjunction window, memory-independent tunables).
- `store.py` — atomic JSON store; anti-shrink `merge_samples`; `retain_only`
  (prunes only REAL objects not on the HRR list; keeps non-REAL); elset retention.
- `sources.py` — `UDLElsetSource`, `ManualElsetSource`, `DemoElsetSource`;
  `_elset_dict` (the retained mean elements); `_udl_ts` (trailing-Z epoch form).
- `hrr.py` — dynamic JCO HRR list pull and store.
- `conjunction.py` — SGP4 closest-approach screen and TLE export/reconstruction.
- `refresh.py` — background scheduler; `run_once`; `pull_window` (mode + absolute
  or relative window).
- `security.py` — token gate and rate limiting.
- `main.py` — FastAPI app factory, routes, the tracks payload builder.
- `static/index.html` — the single-file canvas SPA (no build step). Version-agnostic
  render pipeline: `transform` (tracks to render objects), `drawTrails`,
  `drawHead` (+ `raNeedle`), the inspector modal, view/window/range controls.

Tests: `tests/test_api.py`, `test_astro.py`, `test_conjunction.py`,
`test_drift.py`, `test_hrr.py`, `test_sources.py`, `test_store.py`. Offline-safe
(demo mode when `UDL_BASE_URL` is unset).

## API contract

- `GET /api/tracks?view=real|sim|combined` — filtered tracks; ETag/304. Each
  track carries `object_id, name, data_mode, classification_marking, source,
  origin, target, samples[], drift_deg_per_day, ra_deg`.
- `GET /api/hrr` — the HRR object map.
- `GET /api/conjunctions?target=<satNo>` — target TLE plus per-neighbour closest
  approach and TLEs.
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

- Native UDL TLEs (top task). FACT, established during migration: `/udl/elset`
  carries native `line1`/`line2` on ~100% of records; JCO pushes the HRR list
  only, elements come from Cloudstone, NorthStar, EXO, KBR, KRTL, LeoLabs and
  18th SPCS. Plan: capture `line1`/`line2` (and `revNo`, `meanMotionDot`,
  `meanMotionDDot`, `ephemType`) at ingest, serve them verbatim with source
  provenance and per-record classification marking, demote reconstruction to a
  fallback, and cross-check the native line's satnum against the HRR satNo before
  serving. DECISION OWED BY ASH: a source trust order (e.g. 18th SPCS then
  LeoLabs then commercial) versus newest-fix-wins-with-a-visible-label. LICENSING
  boundary: commercial elsets are `U//PR-...` proprietary; confirm copy-out terms
  before exposing a copy button on them.
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
- `READINESS.md` — pre-submit App Store readiness scoring.
- `pyproject.toml` — tool config and the coverage floor.
