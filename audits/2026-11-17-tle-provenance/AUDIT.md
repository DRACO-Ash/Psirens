# PSIRENS element-set provenance audit

Bluestaq Limited · PSIRENS · Not Classified
17 November 2026 · prompted by COSMOS 2589 (64467) · audited source: `main` at `a97ee89` (repo 1.7.3)
Owner's rule this audit measures against, verbatim: "The UDL is the primary source of information, we need to make calculations based on that information. We are not in the business of making this up." and "It should not INVENT anything from ANY TLE!"

## 1. Bottom line

PSIRENS does not meet that rule today, and it has not met it in any deployed build.

It takes element sets from UDL correctly, and it calculates a single object's position from a single element set correctly. Everything after that is where it fails. It pools every provider, every data mode and every epoch for a catalogue number into one anonymous track. It then publishes statistics, events, positions, markings and TLE lines that no single UDL record supports. In some places it fills in values the record never carried.

COSMOS 2589 is the worked example:
● Six sources put the object at 127.9°E, holding station.
● One provider's element-set product put it at 64°E.
● PSIRENS plotted the outlier as the object, fitted a −12.96°/day drift through both positions, confirmed a manoeuvre that did not happen, and projected an approach to a neighbour that is not there.

Every mechanism behind that is reproduced below through the shipped code (`prove.py`, 11 of 11 offline findings reproduced). The one not reproduced offline is a fact of the live system.

**Rollback.** 1.6.12, the last build with a recorded green deploy, contains every defect in this audit except the event layer (Shift Brief, `/api/events`). Rolling back removes the false manoeuvre alerts. It does **not** remove:
● the blended track;
● the blended drift rate;
● the provider-hopping plot head;
● commercial-first TLE selection;
● the invented "U" marking;
● the zero-filled elements;
● the reconstructed TLEs.

Checked line by line against git (`9258395`).

## 2. Where element sets come from, and when

| Path | Trigger | Window | Filters | Code |
|---|---|---|---|---|
| UDL `/udl/elset`, scheduled | Every `REFRESH_SECONDS` (3600) from start-up | Last `REFRESH_LOOKBACK_HOURS` (24), in `UDL_SLICE_HOURS` (24) slices, halved when a slice returns 9000 rows or more | High-interest list only. **No data-mode filter** (`modes=None`) | `refresh.py:197-201`, `sources.py:271-355` |
| UDL `/udl/elset`, operator pull | `POST /api/pull` | Operator's relative or absolute window | REAL: REAL only, high-interest list. SIM: SIMULATED only, no list | `refresh.py:217-256` |
| Manual element set | `POST /api/manual-elset` | Now, held for `ttl_hours` | None. Merged under whatever catalogue number the operator gives | `sources.py:426-508`, `main.py:439` |
| Demo belt | `DEMO_MODE` true, or no `UDL_BASE_URL` | Synthetic, 30 days | High-interest filter applies when UDL is enabled | `main.py:582`, `sources.py:514-678` |

Per UDL row, PSIRENS reads:
● `satNo`, `epoch`, `inclination`, `eccentricity`, `raan`, `argOfPerigee`, `meanAnomaly`, `meanMotion`, `bStar`;
● `dataMode`, `classificationMarking`, `source`, `origObjectId`;
● `line1` and `line2`, plus `revNo`, `meanMotionDot`, `meanMotionDDot` and `ephemType`.

Field names are as recorded in `sources.py:253-256`.

Each row then becomes two things:
● **A sample** in the object's track: epoch, sub-satellite longitude, inclination and RAAN, with no provider, no marking and no data mode (`sources.py:138-147`).
● **A retained element set**, filed twice: under `elset` (newest overall, any provider) and under `elset_candidates[source]` (newest per provider, kept for ever) (`sources.py:97-112`, `store.py:86-112`).

## 3. Every output, classified

● **Direct:** verbatim from a UDL record.
● **Calculated:** a deterministic SGP4 function of one UDL record.
● **Derived:** a statistic across several records.
● **Invented:** nothing in UDL supports it.

| Output on screen or API | Class today | Built from | Verdict |
|---|---|---|---|
| Sample longitude | Calculated | SGP4 at the record's own epoch, rotated by GMST (`astro.py:53-112`) | Sound. Ignores `ephemType`, see F11 |
| Sample inclination, RAAN | Direct | The record's mean elements | Sound |
| Plot head (position, age) | Calculated, but from whichever provider published last | Newest sample of any provider (`index.html:400`) | **Fails**, F5 |
| RA needle, co-planar head | Direct, from newest record of any provider | `rec["elset"]` (`main.py:195`, `coplanar.py`) | **Fails**, same cause as F5 |
| Trail | Derived | Every provider's samples interleaved; segments above 18°/day silently dropped (`index.html:458`) | **Fails**: blends sources and hides the contradiction, F1 |
| Track span | Derived | Range of the blended samples | **Fails**, F1 (read 63.8° for a stationary object) |
| Drift rate and fit row | Derived | Least squares through the blended samples; only `|rate| > 18` rejected (`astro.py:342`) | **Fails**, F3 |
| Shift Brief events (E-W, N-S, early warning) | Derived | Fits and medians through the blended samples | **Fails**, F4 |
| Event projection ("reaches X in ~0.1 d") | **Invented** | Extrapolation of a blended rate (`events.py:425-446`) | **Remove** |
| Silences ("gone quiet") | Derived | Gaps across all providers | Defensible, but it reports a feed outage as N objects (calibration note) |
| TLE lines, native | Direct | Provider's own `line1`/`line2`, validated | Sound in itself. Which provider is chosen fails, F6 and F12 |
| TLE lines, reconstructed | **Invented** | `sgp4.exporter` with element number, rev number, ephemeris type, ṅ, n̈ and designator filled in (`conjunction.py:57-92`) | **Remove**, F8 |
| Classification marking, track and TLE | Direct, but **invented "U"** when absent | `row.get("classificationMarking", "U")`, `el.get("classification") or "U"` | **Fails, fail-open**, F7 |
| Data mode | Direct, but **invented "REAL"** when absent or unknown, and last pull labels the whole object | `_norm_mode`, `_copy_meta` | **Fails**, F10 |
| Closest approach (conjunctions) | Calculated forecast, 7 days of SGP4 | Selected element set per object, but propagated from JSON fields with ṅ, n̈ and ephemeris type forced to zero | **Fails** in part, F11. Neighbours are chosen by the blended head |
| Elements absent from a row | **Invented** as 0.0 | `row.get("meanAnomaly", 0.0)` and four others (`sources.py:371-379`) | **Fails**, F9 |
| Demo objects | **Invented** | Synthetic, labelled REAL, four under **real catalogue numbers on the high-interest list** | **Fails** whenever `DEMO_MODE` is on, F13 |
| Manual element sets | Operator-supplied | Merged into the UDL object of the same catalogue number; marking defaults to "U" | **Fails**, F14 |

## 4. Findings

Evidence key:
● **PROVEN:** reproduced through the shipped modules by `prove.py`.
● **OBSERVED:** read from the live system.
● **CODE:** read from source, with file and line.

### Critical: wrong positions and rates presented as fact

**F1. Every provider for a catalogue number is pooled into one track.** PROVEN (73 samples under 64467 spanning 64° to 128°), OBSERVED (14 two-hour windows in the live export hold both positions, up to 62.7° apart). `sources.py:385-394`, `store.py:255-262`. Samples are deduplicated by exact epoch string only. When two providers share an epoch (KBR, NorthStar and EXO all carry 16T19:24:22.550Z in your comparison), the first one seen is kept and `_enrich_sample` fills its gaps from the other. One stored sample can then hold two providers' values.

**F2. Samples carry no provider, mode or marking.** PROVEN (keys: epoch, inclination, RAAN, longitude). History therefore cannot be split by source after the fact, which is what any fix to F1 needs.

**F3. A drift rate is published from the blend; its residual is never checked.** PROVEN (−11.89°/day at 28.5° rms; the 127.9°E line alone gives −0.03°/day). OBSERVED (−12.96°/day at 19.224° rms on your screenshot). `astro.py:342` rejects only a rate above 18°/day. The docstring at `astro.py:130-134` says a large residual means "the rate should be read as a summary rather than a measurement", but nothing acts on it.

**F4. The blend is reported as a CONFIRMED manoeuvre.** OBSERVED: the live export carries 64467 STARTED, CONFIRMED, z 9.7, −0.01 → −16.32°/day, "burn 14 Nov 09:37". This is NorthStar's element sets entering the fit window. It was not reproduced offline: whether the detector fires depends on how the two providers' samples interleave. That dependence on luck, with no rule to stop it, is the finding.

**F5. The plot head is whichever provider published last.** PROVEN (head at 64.33°E with the live timing). `index.html:400` takes the newest sample. `elset` (`sources.py:105-107`) is newest overall too, so the RA needle and the co-planar head follow the same provider.

**F6. TLE selection prefers commercial over everything, and SAFRAN is not even classed.** PROVEN (NorthStar served; SAFRAN classed "unknown", which ranks last). `tle.py:80-85`, `tle.py:297-311`. The source class lists are hand-written substrings. There is no check that the chosen element set agrees with any other provider.

**F12. A provider's newest fix is kept for ever and outranks fresher data.** PROVEN (a 60-day-old NorthStar fix served over a fresh 18 SDS one). `store.py:97-112` never ages candidates out.

### Critical: invented values and markings

**F7. A record with no classification marking is labelled "U" and offered for copy.** PROVEN. `sources.py:388`, `sources.py:411`, `tle.py:320`, `main.py:184` and `:189`. The refusal `copy_denied_reason` gives for "no classification marking on the record" can never be reached. For a defence product this fails open, and every other release gate here fails closed.

**F8. With no native line, a TLE is fabricated.** PROVEN (`1 64467U          26321.37500000  .00000000  00000-0  00000+0 0    19`). `conjunction.py:57-92`. The line uses element number 1, rev number 0, ephemeris type 0, ṅ and n̈ of zero, and a blank designator. It is labelled RECONSTRUCTED, but it is still a TLE no provider issued.

**F9. Missing elements are filled with 0.0 and plotted.** PROVEN: a row missing mean anomaly, argument of perigee, RAAN and eccentricity was plotted at 168.53°E. `sources.py:371-379`. Only inclination and mean motion are required.

**F10. Data modes are merged per catalogue number, and the last pull labels the lot.** PROVEN (REAL and EXERCISE samples under one object labelled REAL). `refresh.py:200` ingests every mode on the hourly refresh. `store.py:77-83` lets the newest row set the object's mode. `sources.py:38-42`, `refresh.py:26-30` and `main.py:171-174` turn an absent or unknown mode into REAL.

**F13. The demo belt reuses four real high-interest catalogue numbers, labelled REAL.** CODE, `sources.py:535-540`: 41836, 28924, 43683 and 41748 with synthetic positions. The demo is built whenever `DEMO_MODE` is true, even with UDL enabled (`main.py:582`). No contamination is visible in your export (BEIDOU-3 G1 43683 sits at 159.9–160.0°E live, not the demo's 140°E). Whether `DEMO_MODE` is set in the deployed configuration is **TBC, Ash** (Configuration tab).

**F14. Manual element sets merge into the UDL object of the same catalogue number.** CODE: `ManualElsetIn` defaults the marking to "U" (`models.py:72`). A manual set becomes the newest `elset` if its epoch is newer, so the plot head, needle and TLE follow an operator's numbers under a UDL object's name. It does default to SIMULATED mode, which limits the damage in the Real World view.

### Serious: calculations that are not faithful to the record

**F11. The conjunction screen ignores the provider's ephemeris type and drag terms.** PROVEN (ephemeris type 4 propagated as 0, ṅ forced to zero). `conjunction.py:63-76`. CONTEXT-001 says "the element set that is SERVED must also be the one PROPAGATED". Today the displayed native line and the propagated numbers come from the same record, but not the same values. An SGP4-XP element set (type 4) propagated as plain SGP4 gives the wrong orbit. `sub_longitude_deg` has the same gap (`astro.py:74-89`). Whether any provider on this tenant sends type 4 is **TBC**: the existing probe can count it.

**F15. Neighbours, projections and co-planar partners are chosen by the blended head.** CODE, `conjunction.py:167-169`, `events.py:425-446`. This is why COSMOS 2589's brief entry said it "reaches COMSATBW-1's longitude in ~0.1 d": COMSATBW-1 sits near 64°E, where NorthStar's element sets put COSMOS 2589.

### Event-layer defects (1.7.0 to 1.7.3 only)

Recorded in CLAUDE.md the same day:
● Event ids change with every refresh, because the split grid hangs from the newest fix in the store.
● 11 of 15 STARTED events are station-keeping reversals mislabelled "now drifting".
● 37 of 46 silences are one feed stopping.

## 5. What has to change to meet the rule

These are proposed principles, not built. Each removes a class of invention rather than patching one case.

1. **One record, one provider, one mode.** Store the provider, data mode, marking and ephemeris type on every sample. Key de-duplication on (provider, epoch), not epoch alone. Never enrich one provider's sample from another's.
2. **Tracks are per provider.** Drift fits, events, track span and trails are computed within one provider's series only, never across providers. Where providers disagree, show the disagreement; never average it.
3. **Agreement is checked, not assumed.** For each object, compare the newest position from every provider. A provider more than a set tolerance from the median of the others is quarantined for that object and named in the inspector. With two providers and no majority, show "sources disagree" and publish no rate, no event and no projection.
4. **Nothing is filled in.** A row missing any element needed for a position is rejected and counted, not zero-filled. A record with no marking is unmarked and withheld, not "U". An absent or unknown data mode is rejected, not REAL. No reconstructed TLE: where UDL carried no native line, show the record's elements and say "no TLE issued by the provider".
5. **Respect the provider's theory.** Propagate with the record's own ephemeris type and drag terms. Refuse a type we cannot propagate rather than approximating it.
6. **Quality travels with every derived number.** A fit whose residual exceeds a stated ceiling publishes no rate. Every rate, event and screen result names its provider, element-set epoch and age.
7. **No extrapolated claims.** Remove the event projection. Label the conjunction screen as a forecast from named element sets with stated epochs.
8. **Synthetic data can never reach a live store.** The demo is never built when UDL is enabled. Demo objects use identifiers that cannot be catalogue numbers, in a non-REAL mode.
9. **Selection is a policy over agreeing, current sources.** Age candidates out after a stated limit. Choose only among sources that pass point 3. Classify sources from a maintained table, with an unknown source surfaced rather than ranked last in silence.

Under the owner rule this is a bold change, so it comes to Ash as a proposal with figures before any build. It is also larger than one release: points 1, 4 and 8 are the floor, and the rest build on them.

## 6. Open questions (TBC, owner named)

● Is `DEMO_MODE` set in the deployed Configuration tab? **Ash.**
● Which build is deployed now, and which are you rolling back to? `/api/meta` reports `version`. **Ash.**
● Why are NorthStar's element sets for 64467 at 64°E when NorthStar's own state agrees with the other five? **Ash with NorthStar.**
● Do this tenant's elset rows carry fields that flag uncorrelated or derived element sets, and which providers send ephemeris type 4? **Answerable** by extending `tools/udl_elset_probe.py` and running it on a networked workstation; this environment has no UDL egress.
● Is SAFRAN commercial or government for selection purposes? **Ash.**

## 7. Reproducing this

```
. .venv/bin/activate
PYTHONPATH=src python audits/2026-11-17-tle-provenance/prove.py
```

Offline, no credentials. The inputs are the two COSMOS 2589 element sets you supplied; each provider's history is generated by advancing only its own mean anomaly at its own mean motion. Every number comes from the shipped modules. Exit status 0 means every finding still reproduces, so the same script is the red case for the fix: it must fail once each finding is fixed.
