# CONTEXT-001 — Operating Context: Principal Python Engineer Project

**Document ID:** CONTEXT-001
**Version:** 1.1, 12 August 2026
**Owner:** Ash Higgins, Technical Director, Bluestaq Ltd
**Review:** Monthly, or on any material change to environment, projects, or standards
**Classification:** Not classified
**Precedence:** Organisation instructions, then the Principal Python Engineer project instruction, then this file, then the live conversation. Where this file and the project instruction conflict on engineering standards, the Script mode section below is authoritative for portable single-file tooling; the project instruction remains authoritative for everything else.
**Changes in 1.1:** Added Section 7 (Bluestaq App Store deploy contract and quality gate); extended the LEARNED register with UDL notification/HRR, SGP4 TLE and conjunction, and drift/trail sanity facts; updated active projects with PSIRENS.

## 1. Purpose

This file gives every session in this project its operating state: who the advisor is working with, the environment, the active projects, the engineering modes, and the accumulated technical learnings. It exists so no session starts cold and no hard-won fact has to be rediscovered.

## 2. Principal

Ash Higgins. Technical Director, Information Security Manager and Data Protection Lead at Bluestaq Ltd. In this project the engineering hat is worn: space-data tooling, Python engineering, and analysis pipelines. Compliance and governance work runs in separate projects with their own skills; do not import that framing here unless a security boundary is genuinely in play (credentials, data classification, supplier exposure), in which case flag it briefly and carry on.

Working style: lead with the point, direct over diplomatic, UK English, no em-dashes, no filler. Mark fact, inference and speculation in any analytical output. State assumptions inline and proceed; stop only for genuine blockers.

## 3. Operating modes

**Advisory (default).** Questions, trade-offs, reviews, planning. Answer first, tight, risks early, clear next step.

**Build.** Full 13-part deliverable structure per the project instruction, with the complete toolchain (Ruff, Black, MyPy, Pyright, Bandit, pytest and the rest) and CI/CD gates.

**Script (defined here; supplements the project instruction).** For portable single-file tooling destined for constrained or air-gapped environments:
● Python standard library only. No pip dependencies. Python 3.9+ compatibility unless stated otherwise.
● Fully typed, docstrings, structured logging to stderr, explicit exception handling. No pseudocode.
● Testing: a `--self-test` flag or doctests in place of pytest. Every self-test contains explicit assertions; tests that merely execute code are forbidden.
● Evidence: the self-test emits a JSON manifest (test id, assertions, inputs, expected, observed, status, timestamp) to stdout or file. This is the Script-mode form of the recorded assertion manifest.
● Credentials never hardcoded; never printed; loaded per the pattern in Section 5.
● Trigger phrases: "portable", "stdlib only", "single file", "no dependencies", or any UDL retrieval task, which defaults to Script mode.

## 4. Environment (fact unless marked)

● Primary workstation: Windows. File permissions hardened with icacls; EFS (`cipher /e`) and Windows Credential Manager noted as stronger alternatives for secrets at rest.
● Credentials pattern: `~/.config/phase_offset/credentials.ini`, read with `configparser` using `interpolation=None`, with interactive prompt fallback.
● Claude Code is the build harness; the flight-plan skill and the Bluestaq Launchpad app form the planning-to-build workflow (brief in, archetype and stack analysis out).
● Access to the Unified Data Library (UDL) with Basic authentication.
● Space-Track.org and The Space Devs Launch Library 2 (v2.3.0) used for cataloguing work.
● Server-archetype apps deploy to the Bluestaq App Store. The App Store deploy contract and its SonarQube quality gate are load-bearing and non-obvious; see Section 7 and treat it as pre-flight for every app.

## 5. LEARNED — technical facts register

Append-only. Each entry earned through testing or source inspection; do not re-derive, and do not contradict without new evidence. When a session discovers something new and expensive, append it here and note the date.

### UDL API (verified against live behaviour and the udl-sdk source)

● The history list endpoint (`/udl/eoobservation/history`) requires `Accept: */*`. Sending `application/json` returns HTTP 406.
● The history count endpoint (`/history/count`) requires `Accept: text/plain`.
● Authorisation is standard HTTP Basic: `Basic base64(user:password)`.
● `obTime` range syntax is `startISO..endISO` with microsecond precision: `%Y-%m-%dT%H:%M:%S.%f` plus a trailing `Z`.
● `firstResult` is hard-capped at 10,000 server-side. Never offset-paginate the history endpoint; slice by time window instead (10-day slices proven for a 30-day lookback; 2-second pause between slices; de-duplicate across slices by UDL record `id`).
● `maxResults` above 10,000 triggers HTTP 500. The history endpoint is designed to return one complete bounded window per response.
● Solar Equatorial Phase Angle (SEPA) maps to `solarEqPhaseAngle`, which is distinct from `solarPhaseAngle` and `solarDecAngle`. Confirmed against the `EoObservationFull` Pydantic model in `udl-sdk`.
● The `udl-sdk` PyPI package is used as a reference for field names and behaviour only; production scripts call the API with raw `urllib`.
● Live-feed outputs should drive classification banners from the `classificationMarking` field on returned records, not from manual assertion.

### UDL notification and JCO HRR (verified live, 6 August 2026)

● The high-interest list is pulled from `/udl/notification` with a relative operator: `createdAt=>now-N hours` (URL-encoded `createdAt=%3Enow-6%20hours`), plus `dataMode=REAL&msgType=JCO-HRR-SATELLITES&source=JCO`.
● The endpoint returns a JSON list of records. Each record's `msgBody` is a direct array of `{commonName, country, satNo (string), rank (1-5), orbitRegime}`. Top-level fields include `classificationMarking`, `createdAt`, `createdBy`, `dataMode`, `id`, `msgType`, `origNetwork`, `origin`, `source`.
● Live baseline (6 August 2026): 2942 records total, 594 GEO, marking `U//DS-JCO-NOTIF`. The notification list cap is 30,000; time-slice above that.
● `/udl/elset` fields: `satNo`, `epoch`, `inclination`, `eccentricity`, `raan`, `argOfPerigee`, `meanAnomaly`, `meanMotion`, `bStar`, `dataMode`, `classificationMarking`, `origObjectId`, `source`. No target field. The epoch range needs the trailing-Z microsecond form; a `+00:00` offset returns 200 but matches nothing.
● Catalogue numbers are strings (Alpha-5 for 6-digit). OMM/GP-native is preferred; classic TLE is a legacy export.

### SGP4 TLE export and conjunction screening (validated with the sgp4 library)

● `from sgp4.exporter import export_tle` produces a valid, checksummed TLE, including Alpha-5 for 6-digit catalogue numbers, but only if after `sgp4init` you also set `satrec.classification`, `intldesg`, `elnum`, `revnum`, `ephtype`.
● Round-trip verified: `export_tle` then `Satrec.twoline2rv` reproduces position to under a metre. The international designator and rev number are not carried by the UDL elset and export blank or zero; the orbital fields and checksums are exact.
● Closest approach is a coarse-then-fine SGP4 range scan over a configurable window (default 7 days, `CONJ_WINDOW_HOURS`), reporting 3D TEME range in km. A 1-degree GEO offset gives about 736 km, matching the analytic chord.
● Retain the latest full mean element set per object so TLE export and conjunctions have real elements; the plot itself needs only sub-longitude and inclination.

### Native UDL elset TLEs (implemented in PSIRENS 1.5.0, 23 August 2026)

● `/udl/elset` records carry native `line1`/`line2` alongside the mean elements. Serving them verbatim is strictly better than reconstruction: `sgp4.exporter` cannot recover the international designator or the rev number, so a reconstructed line is always lossier than the one the provider pushed.
● A native line must be validated before it is served: shape (69 columns, `1 `/`2 ` prefixes), mod-10 checksum on BOTH lines, agreement between the two lines on a catalogue number, and that number matched against the record's own `satNo`. The satNo cross-check is the one that matters operationally, because an HRR-scoped plot would otherwise show one object's orbit under another object's name. Alpha-5 decoding is required for six-digit catalogue numbers (A=10 through Z=33, I and O omitted).
● Retain the newest element set PER PROVIDER, not just the newest overall. A single latest-elset slot makes any source-selection policy unimplementable, and a pull that misses one provider must not erase what that provider supplied earlier (anti-shrink applies to providers, not only to samples).
● The element set that is SERVED must also be the one PROPAGATED. Serving one provider's line while screening from another's puts a range on screen that the displayed elements do not produce.
● OWNER DECISION, Ash, 23 August 2026: source trust order is newest commercial first. Implemented as `TLE_SOURCE_PRIORITY`, default `commercial,government,unknown`: first class holding a fix wins, newest epoch within that class; `any` collapses to plain newest-fix-wins. Assumption stated at the time and not contradicted: "commercial first" means commercial preferred as a class, with recency deciding within it, rather than recency deciding overall.
● OWNER DECISION, Ash, 23 August 2026: gate the copy control. Implemented fail-closed, so ONLY a plain unclassified, caveat-free marking is offered for copy-out. A proprietary commercial line (`U//PR-...`) is displayed with the control withheld and the reason stated. Note the breadth: any caveat withholds, including `U//DS-...`, which is wider than the licensing question alone required. Open with Ash.
● The tenant feed uses `18SDS` (18th Space Defense Squadron, the redesignation of 18 SPCS) as a `source` value; the classification table must carry both spellings or a government fix silently classes as unknown.
● A client that does `fetch(...).then(r => r.json())` without checking `response.ok` turns every server fault into one indistinguishable message, because only a NON-JSON body reaches the catch. Starlette returns plain text for an unhandled exception, so an unguarded raise inside a route surfaces in the UI as a generic "service unavailable" with the cause discarded. Check `ok`, and render the server's stated `detail`; a route should never be able to return a non-JSON body.
● Guard EVERY propagation entry point, not just the loop. PSIRENS guarded `build_satrec` for conjunction neighbours from the start but not for the target, so a single malformed target element set took down the whole request. Filter unusable element sets at selection time as well, so a malformed record from one provider cannot shadow a usable fix the object already had.
● INFERENCE, not yet verified live: the ~100% native-line presence figure is inherited from the prior environment and has not been re-measured. `tools/udl_elset_probe.py` in the PSIRENS repo measures it, plus the satNo cross-check pass rate, the source-class coverage and the marking distribution. Run it from a networked workstation.

### Drift and trail sanity (physical ceilings)

● Real GEO longitude rate tops out around 14 deg/day. Treat anything above about 18 deg/day as a data artifact (a mismatched or near-duplicate-epoch historical elset), not motion.
● `drift_deg_per_day` must walk back to the most recent fix at least 30 minutes before the latest (ignoring near-duplicate epochs), use the shortest-angle difference across the +/-180 seam, and return None above the ceiling. A millisecond epoch gap once produced about -22,900,000 deg/day (ASTRA 1M); the naive last-two-samples form is the trap.
● Chart trail segments must skip the +/-180 seam and any segment implying motion above the ceiling, or a single bad fix beams a line across the whole belt.

### Analysis discipline

● Interrogate a new object's data before narrating it; never assume one satellite behaves like another.
● Phase-function and signature analysis needs minimum-sample guards and honest empty and sparse states.
● For any inference chain, backtest against historical truth before building the application around it; measured evidence beats asserted acceptance criteria.

## 6. Active projects

● **PSIRENS (GEO-belt screening dashboard).** Deployed to the Bluestaq App Store; server archetype (FastAPI plus a single-file SPA). Current build 1.5.2. Plots JCO HRR GEO objects (sub-satellite longitude vs inclination) with rank colour, drift glyphs, age fade, historic trails, and a per-object RA (RAAN) bearing needle. Features: dynamic JCO HRR pull; real / simulation / combined views; relative-window and absolute start-stop scenario pulls via `POST /api/pull`; per-neighbour closest approach and copyable TLEs in a floating inspector; deep zoom to 0.05 deg longitude and 0.01 deg inclination with precision-aware axis labels. Memory set to 1Gi. Native UDL TLEs served verbatim with source provenance and a fail-closed copy-out gate (1.5.0). Open items: ingest-time sample sanitisation (would also clean the inspector Track-span readout); live verification of the native-TLE assumptions via `tools/udl_elset_probe.py`; the breadth of the copy-out gate. The flaky `/api/pull` test-isolation issue is RESOLVED (1.4.14, `SCHEDULER_ENABLED`).
● **UDL EO retriever.** Working. Single-file, Script mode, chart-ready JSON output (`mag_sepa_satNo{satNo}.json`) with a flat points array and per-sensor series. Next: generalisation into a backend service with per-object caching.
● **SEPA and temporal signature analysis.** Working against multiple objects; frontend visual layer in the established dark mission-control style.
● **SATCAT designator gap finder.** Working. Space-Track gaps correlated against Launch Library 2, with `satf_els` analyst-object ingestion.
● **Launch-detection webapp.** Planned. NOTAM and navigational-warning ingestion, PostGIS, `tle-tailor` differential correction. Agreed first step: a two-week backtest harness over about 50 historical launches before any UI.
● **AI infrastructure build.** In flight. Governed by the AI Infrastructure Audit (9 July 2026) and its operator console; UDL knowledge above graduates into a `udl-data-engineering` skill.

## 7. Bluestaq App Store: deploy contract and quality gate

Understand this before the first commit of any App Store app. None of it is fully discoverable from the platform docs; each line below cost at least one build or upload cycle to learn on PSIRENS. Build compliant from the start rather than fixing reactively.

### Deploy contract (container archetype)

● The template is auto-detected from the Dockerfile; a Dockerfile means the container archetype.
● Read the `PORT` env var (default 8080) and bind `0.0.0.0`. Do not set `ENV PORT` in the Dockerfile; the platform injects it.
● Run as a non-root numeric user (uid 10001).
● `GET /` and `GET /healthz` (and `/readyz`) must return 200 unauthenticated; the platform probes these.
● Memory floor is 256Mi. Set resources in the Configuration tab, not the Dockerfile. A live UDL pull needs headroom; PSIRENS runs at 1Gi, and an under-set memory value was the last deploy blocker until it was raised.
● The "App Build" queued state is normal, not a failure; the queue clock is not an error.
● Upload a new build via "Upload New Version" from the GitLab project `vanguard-engineering/app-store-apps/<slug>`. The slug is lowercase; the display name is separate.

### Two requirements files

● The platform TEST stage runs only `pip install -r requirements.txt` then pytest. All test tooling (pytest, pytest-cov, coverage, and any helper the tests import, for example the complexity checker) must therefore be in `requirements.txt`, or the test stage cannot collect.
● The runtime image installs `requirements-runtime.txt` (no test tooling) so the container stays lean.

### SonarQube quality gate (five conditions, all on OVERALL code, maximum 0% violations)

● violations = 0 (bugs, vulnerabilities, code smells). A single Blocker drops the rating to E and fails the gate.
● coverage at least 80%.
● security rating A.
● reliability rating A.
● security-review rating A (at least 30% of security hotspots reviewed).
● Multi-Quality mode is in force. On failure the pipeline uploads `sonar-issues.json` and `sonar-quality-gate.json` artifacts that name the exact failing condition and file:line; read those first.

### sonar-project.properties (proven for a Python plus single-file SPA app)

● `sonar.sources=src`. Note that the SPA `index.html` IS analysed as source for issues.
● `sonar.coverage.exclusions=src/<pkg>/static/**` and `sonar.cpd.exclusions=src/<pkg>/static/**`. The SPA is excluded from coverage and duplication only; it is still analysed for issues. Coverage is driven by the Python side.

### The local-proxy gap (the expensive, recurring lesson)

The local eslint plus eslint-plugin-sonarjs proxy does NOT cover the SonarQube Web (HTML) analyser, nor several JS and Python rules. Each of the following slipped past the local proxy and cost one upload cycle before it was understood. Anticipate them:
● HTML a11y landmark labelling: an `aside` or `section` used as a landmark needs an accessible name.
● `no-negated-condition` (S1940): no negated `if`/`else` and no negated ternary.
● zero-fraction numeric literals.
● `python:S3776` cognitive complexity: a hard cap of 15 per function.
● `python:S107` parameter count: a hard cap of 13 per function, counting keyword-only arguments. This failed a real PSIRENS upload (1.5.0, `_elset_dict` at 18 parameters) after a fully green local loop, because neither ruff nor the eslint proxy carries the rule. The fix is to group arguments that travel together into a value object, not to split the function. Now gated locally by the same AST pass that checks cognitive complexity.
● `S6643`: prefer `globalThis` over `window`.
● `S6819`: prefer the native element over an ARIA landmark role (region to a `<section>` with an accessible name, banner to `<header>`, navigation to `<nav>`, and the same for main, contentinfo, complementary, and form).
● prefer `element.dataset.x` over `getAttribute("data-x")`.

### Pre-flight and the durable fix

● `simulate-pipeline.sh` mirrors the platform test stage (`pip install -r requirements.txt`, then pytest with an 80% coverage floor) and then runs reactive gates that pre-empt the rules above: a Python AST gate covering BOTH cognitive complexity (the `cognitive_complexity` PyPI package matches the SonarSource algorithm exactly and fails any function over 15) and parameter count (fails any function over 13), `grep window.`, `grep role="(region|banner|navigation|main|contentinfo|complementary|form)"`, and `grep getAttribute("data-`.
● The grep gates are reactive substitutes, one per rule already learned. The durable fix is a real `sonar-scanner` run against the tenant SonarQube server before upload, which catches all of the above in one pass. The sandbox cannot reach the SonarQube host; a networked workstation can. Standing this up ends the one-rule-per-cycle pattern.
● When the gate fails, fix to the exact line it names, then add a local grep gate so that rule cannot recur.

## 8. Standing cautions

● UDL redistribution terms, FAA NOTAM system migration, and inland launch coverage gaps are open risks on the launch-detection work (inference: unresolved as at last discussion).
● Anything in Section 5 dated by an external provider can rot; re-verify on version-change signals rather than assuming.
● A Claude Code remote build container is denied egress to `unifieddatalibrary.com` by organisation policy (403 on CONNECT, observed 23 August 2026) and carries no credentials. UDL reachability on Ash's workstation does not imply reachability in the build environment. Verify live UDL behaviour with a Script-mode probe run on the workstation; never assume the container can reach it.
● The App Store SonarQube gate will fail on rules the local eslint proxy cannot see. Treat Section 7 as pre-flight for every app, not just PSIRENS, until a real `sonar-scanner` run is wired into the loop.
