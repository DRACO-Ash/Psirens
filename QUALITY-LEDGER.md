# PSIRENS quality ledger

Every code-quality issue raised against this application during development,
what caused it, how it was fixed, and what now stops it recurring.

Classification: Not Classified. Owner: Ash Higgins, Bluestaq Limited.
Covers builds 1.4.14 to 1.7.2, 23 August to 17 November 2026, 43 issues.

---

## 1. What this is, and what it is not

This is the **register**: a dated account of what actually went wrong on a real
application that ships through a hard quality gate, with the evidence.

`SNIFFS.md` is the companion **rulebook**: the patterns to avoid, with a
runnable checker. If you want to stop hitting these, read that. If you want to
know what hitting them costs and in what order it happens, read this.

**How to use it in another project.** Read section 2, then section 7. Section 8
is a JSON manifest of the same register for programmatic ingestion. The entry
IDs are stable; cite them.

**One caveat on scope.** The SonarQube conditions, rule identifiers and pipeline
shape here are those of the Bluestaq App Store as observed between August and
October 2026. The rules are standard SonarQube and portable. The platform
specifics may not be.

---

## 2. The four findings that matter most

Read these before anything else. They are the ones that cost real cycles, and
the ones most likely to be true of your project too.

● **A green local run does not predict a green gate.** MEASURED: five of ten
  common sniffs are not reported by `eslint-plugin-sonarjs` even with all 279 of
  its rules enabled, including the one that failed a real upload. Only a real
  scanner, or a rule-specific grep, catches those. [A2]

● **A verification that cannot fail is worse than none.** An audit of this
  project's nine guards found exactly **one** had a committed case proving it
  could go red. The other **eight were unproved**. Several were later shown to
  be incapable of failing at all. [D1]

● **Resilience without counting buys silence.** Code that catches an error so a
  loop survives must count what it caught and surface it, or a dead feed is
  indistinguishable from a quiet one. This cost sixteen days of a silently
  frozen operational picture. [C9]

● **When a defect is found, grep for the pattern, not the line.** One defect
  class here was fixed three separate times, on three routes, across eight
  builds, because each fix was applied to the route that had failed rather than
  to the shape of the mistake. [C1, C2, C3]

---

## 3. Rejected by the platform quality gate

Four findings reached the Code Quality stage and failed an upload or a scan.
Each cost at least one build cycle.

### A1. Parameter count above 13 (`python:S107`)
● **Raised:** against 1.5.0. Upload rejected; that build never deployed.
● **Symptom:** `_elset_dict` taking 18 parameters.
● **Cause:** the function had grown one argument at a time across several
  features, and no local check counted parameters.
● **Fix (1.5.1):** group the arguments that travel together into a value object
  (`ElsetProvenance`). Not splitting the function, which would have moved the
  problem.
● **Guard:** `tools/ast_gates.py`, an AST pass that fails any function over 13,
  run twice per build (against a 14-parameter red case first).
● **Cost:** one upload cycle.

### A2. `return Promise.reject(error)` inside a `then` callback
● **Raised:** against 1.5.2. Upload rejected.
● **Symptom:** a returned rejected promise where the rule wants `throw error`.
  Inside a `then` callback both reject identically, so the behaviour was correct.
● **Cause, and the finding that generalises:** MEASURED after the fact,
  `eslint-plugin-sonarjs` does **not** carry this rule, even with all 279 rules
  enabled. The local lint was green. The gate was not.
● **Fix (1.5.3):** `throw error`.
● **Guard:** a text rule in the build loop, checked against a red case before the
  real page.
● **Cost:** one upload cycle, and the realisation that the local linter is a
  proxy rather than a substitute.

### A3. Text contrast below WCAG AA, and the translucent-background trap
● **Raised:** at `index.html:38`, the staleness banner.
● **Symptom:** "Text does not meet the minimal contrast requirement with its
  background."
● **Cause:** NOT a rendering defect. On the near-black page it measures
  **13.35:1**, which is excellent. The analyser cannot know what sits behind a
  **translucent** background, so it reads `background:rgba(198,124,0,.13)` as the
  opaque `#C67C00`, and light amber on mid amber is **2.50:1**. Both figures
  computed with the WCAG 2.1 formula.
● **Fix (1.6.8):** declare the composited colour. `background:#17100A` renders
  identically and measures **14.08:1** as declared, so the source, the analyser
  and the screen all agree.
● **Do not argue "it composites fine at runtime."** The rule exists because a
  translucent text background genuinely breaks the moment the component moves to
  a lighter surface.
● **Second instance, found by grepping the pattern rather than the line:** a
  feature added the day before repeated it on a child selector and would have
  been flagged on the next scan.
● **Guard:** `WEB-CONTRAST` in `tools/sniff_check.py`, a real WCAG ratio
  computation that treats an `rgba()` background as its opaque base. PROVEN by
  running it over the pre-fix file, where it reports the same line and the same
  2.50:1 the platform did.
● **Documented limitation:** it compares only colours declared in the same
  block, which is exactly how the second instance escaped it.

### A4. Lambda capturing a loop variable
● **Raised:** at `astro.py:174`. "Add a parameter to the parent lambda function
  and use variable `col` as its default value; the value of `col` might change at
  the next loop iteration."
● **Symptom:** `max(range(col, n), key=lambda r: abs(aug[r][col]))` inside a
  `for col in range(n)` loop.
● **Not a defect in that instance:** the lambda is consumed by `max` within the
  same iteration, so it never outlives the value it captured. **The rule is about
  the shape, and is right to be.** Store that lambda in a list, return it, or
  defer it, and every copy reads the last value.
● **Fix (1.6.12):** extract `_pivot_row(aug, col, n)` so `col` arrives as a
  parameter. Chosen over the `lambda r, c=col:` default-argument form the
  analyser suggests, because the extracted function reads better.
● **Guard:** `PY-LOOP-LAMBDA` in `sniff_check.py`, an AST rule that reads a `for`
  statement's target names and flags any lambda in its body whose own arguments
  do not shadow them. PROVEN against the pre-fix file from git history, where it
  reports the same line the platform did.
● **Cost:** one scan cycle.

### A5 to A8. Four findings on one upload (1.7.0, 17 November 2026)
Raised together by Code Quality on the Shift Brief build, every one in code
written that week, and every one a rule the local gates did not carry.
● **A5, `python:S1192`:** "application/json" three times in `main.py`, the third
  copy added with `/api/events`. Fixed with one `_JSON` constant.
● **A6, `python:S5886`:** `ew_split` annotated `-> _Split | None` but returned
  `dataclasses.replace(best, ...)`, which typeshed types as the generic
  `DataclassInstance`. Not a runtime defect; the analyser reads it as a type
  mismatch. Fixed by constructing `_Split(...)` explicitly.
● **A7 and A8, `Web:S6843` and `Web:S6819`, one line:** the timeline canvas
  carried `role="img"`. A canvas counts as an interactive element, so any role
  on it is S6843, and `role="img"` anywhere is S6819 (use `<img>` or `<svg>`).
  Fixed with fallback text between the canvas tags, which is the accessible
  description a canvas is meant to have.
● **Guards:** four new `sniff_check.py` rules, `PY-DUP-LITERAL`,
  `PY-REPLACE-RETURN`, `WEB-CANVAS-ROLE` and `WEB-IMG-ROLE`, each with a red and
  a green self-test case. **CALIBRATED, not guessed:** run over the 1.7.0 tree
  from git they report the same four findings at the same file and line as the
  platform; run over the 1.6.12 tree, which passed Code Quality, they report
  nothing. `PY-DUP-LITERAL` skips identifier-like strings and test files,
  because the 1.6.12 tree repeats both freely and passed, and `tests/` is
  declared `sonar.tests`.
● **Cost:** one upload cycle.

### A9. Equality against a float literal (`python:S1244`, 1.7.1)
● **Raised:** `events.py:437`, "Do not perform equality checks with floating
  point values", as a BUG, on 17 November 2026. The gate now reads "New issues:
  1 (max allowed: 0)".
● **Symptom:** `if gap == 0.0 or ... or gap * closing <= 0.0` in `projection`.
● **Not a defect in that instance, and redundant besides:** a gap of exactly
  zero makes the product zero, which the third test already skips. Removed,
  with a regression test that a co-located object is still never offered.
● **Guard:** `PY-FLOAT-EQ`, `==`/`!=` against a float literal in source files.
  CALIBRATED: 1.7.1 from git gives exactly this line; 1.6.12 gives nothing.
  Test files excluded, because the 1.6.12 suite compares floats exactly in five
  files and passed.
● **The process lesson, and it is mine:** this line was written in 1.7.0 and was
  almost certainly already reported on that upload. The 1.7.0 paste ended with
  the heading "Bugs" and nothing under it. A section heading with no content is
  a truncated report, not an empty one; it cost a second upload cycle not to ask
  for the rest. See D11.
● **Cost:** one upload cycle.

### A10 to A17. Eight collection idioms from four rules (`python:S7517`, `S8517`, `S7508`, `S7504`, 1.7.5)
● **Raised:** 17 November 2026, "New issues: 8 (max allowed: 0). 8 code smells
  exceed threshold", all in code written that day or the day before:
  `agreement.py:234, 273, 308, 336` "Use items to iterate over key-value pairs"
  (S7517); `agreement.py:223` "Use min() instead of sorting to find this value"
  (S8517); `agreement.py:222` and `tle.py:415` "Remove this redundant call"
  (S7508); `store.py:355` "Remove this unnecessary list() call on an already
  iterable object" (S7504).
● **Why the gates missed them, plainly:** none of the four rules had ever fired
  on this repository, and every local Python rule was written after the
  platform raised it. The gates were complete for the rules we had met, not for
  the profile.
● **Fixes, two of them not mechanical:**
  ■ S7517: provider streams were keyed by a `(provider, mode)` tuple, so every
  loop unpacked a dict key. Re-keyed mode first, then provider; nothing is
  unpacked now.
  ■ S7508 and S8517: the double sort was a deliberate stable two-pass sort
  (policy class, then newest). Replaced by ONE sort, or `min`, on a compound key
  `(class rank, negated epoch timestamp)`, not by deleting a pass.
  ■ S7504: the `list()` existed because the loop deletes from the dict it walks.
  Removing it alone would raise at runtime; the loop now decides first and
  deletes after.
● **Guards, two layers:**
  ■ `sniff_check.py` gained `PY-DICT-UNPACK`, `PY-SORTED-INDEX`,
  `PY-REDUNDANT-CALL` and `PY-LIST-ITER`. CALIBRATED on git: the uploaded 1.7.5
  tree (`b159761`) gives exactly the platform's eight lines and nothing else;
  1.7.3 (`de7d91f`) and 1.6.12 (`9258395`), both passed, give nothing.
  ■ A NEW wider gate in the loop: ruff's equivalents for this family (`C4`,
  `PERF101`, `PERF102`, `PLC0206`, `FURB192`, `RUF015`, `SIM118`), pinned, with
  a red case (`gate-cases/collections_case.py`) that must produce six named
  codes. Same calibration: silent on 1.7.3 and 1.6.12, catches the S8517 line
  on 1.7.5. `C408` excluded, because it fires on test helpers that passed
  every upload since 1.6.12. Its first run FAILED on its own red case
  (PERF101 needs a locally visible list), which is the red case working.
● **What it does NOT close, stated:** ruff caught only one of the eight
  directly (it declines S7504 where the loop mutates, which Sonar does not),
  so neither layer is a SonarQube profile. The durable fix is unchanged: run
  the tenant's own scanner before upload.
● **Cost:** one upload cycle.

### A18. Three `assert` statements in shipped code (bandit `B101`, 1.7.13)
● **Raised:** 25 November 2026. 1.7.13 passed all 12 stages, but the App Store
  list showed its CATO status as "Pass (97)" where Ash's other apps read 100.
  The findings, from Ash: `store.py:502` (`_snapshot`), `:614` (`ensure`) and
  `:756` (`_purge_locked`), each `assert self._data is not None`, written to
  narrow the type for mypy. bandit flags them because `python -O` strips an
  assert. An earlier paste described two taint-analysis vulnerabilities;
  the three findings Ash then listed are what was acted on.
● **Live risk, measured rather than assumed:** none. `_boot` sets the store
  on every path and the image does not run `-O`. Under `-O` the 1.7.13
  methods, given an unloaded store, returned without error (`_snapshot`,
  `ensure`) or failed with an `AttributeError` that names nothing
  (`_purge_locked`).
● **Fix (1.7.14):** `if self._data is None: raise RuntimeError(NOT_LOADED)`
  at each site. mypy narrows the type the same way. The message is one
  constant, because the suggested literal repeated three times trips
  `python:S1192`, the rule that failed 1.7.0. `PY-DUP-LITERAL` caught that
  before upload.
● **Guards:** the loop runs ruff's bandit port, the whole `S` family, over
  `src`, with a red case (`gate-cases/bandit_case.py`: S101, S301, S307,
  S602). `sniff_check.py` gains `PY-ASSERT` (source only). Both are
  CALIBRATED on git: exactly the three lines on 1.7.13 (`b1976b6`), and
  nothing on 1.7.12 (`d3c8f28`) or 1.6.12 (`9258395`). A test asserts each
  guard raises the named error, and three red cases put each assert back.
● **Cost:** no upload rejected, three points of CATO score.

---

## 4. Caught by local gates before upload

Six findings the project's own loop caught. These are the gates paying for
themselves.

### B1. Cognitive complexity 18 in `load_config` (`python:S3776`)
Introduced while adding six configuration settings in one change. Split into
`_env_int` / `_env_float` helpers. Cap is 15.

### B2. Cognitive complexity 18 in the complexity checker itself
`sniff_check.cognitive_complexity` tripped its own rule. Split into a visitor
class. **A checker is code and is subject to its own rules.**

### B3. Cognitive complexity 18 in `tools/udl_elset_probe.py`
Found only by running our own published rules over our own tree. That file sits
outside `sonar.sources=src`, so **the platform would never have rejected it**.
Worth stating plainly: the platform scans what you tell it to scan.

### B4. Nested ternary (`sonarjs/no-nested-conditional`)
A status-to-message mapping written as a nested ternary, caught on the first
attempt at the very fix for C2. Extracted into a named function.

### B5. Pseudo-random generator (`sonarjs/pseudo-random`)
`Math.random()` used to shuffle quiz questions. Not security-sensitive, but the
scanner cannot tell a quiz from a token. Replaced with
`crypto.getRandomValues`. **The honest fix was cheaper than the argument.**

### B6. Nested ternary inside a nested template literal
Two rules at once in four lines of UI code. Extracted into a named function.

---

## 5. Found by review, screenshot or live operation

Eleven defects no static analyser would have found. These are the expensive
ones.

### C1 to C3. Unchecked `response.ok` before `.json()` — the same defect, three times
● **C1 (fixed 1.5.2), `/api/conjunctions`:** a malformed element set raised an
  unhandled 500 with a non-JSON body. `.json()` rejected, and the catch could
  only say "Conjunction service unavailable", hiding the stated cause.
● **C2 (fixed 1.6.4), `/api/coplanar`:** identical shape, survived two builds,
  **because the C1 fix was applied to the route that had failed rather than to
  the pattern.**
● **C3 (fixed 1.6.13), `/api/hrr`:** identical shape again, and the worst of the
  three. A non-JSON error body blanked the high-interest map, and **every real
  object silently vanished from the plot with no stated cause.**
● **Lesson, recorded three times before it stuck:** when a defect is found, grep
  the file for the pattern, not the route.

### C4. An identifier displayed as a name (1.6.1)
Two views served `obj["name"]` straight from the store while two others joined a
lookup client-side, so rows read "63157 63157". Fixed by resolving names
**server-side**, so every consumer agrees rather than every view repeating the
join.

### C5. A modal that could only grow (1.6.3)
A flex item defaulted to `min-height:auto`, which takes a canvas element's
width and height **attributes** as an intrinsic floor. The redraw set those
attributes from the measured box, so the body ratcheted larger and never
smaller, overflowed, and clipped the plot. MEASURED: an 800px modal gave a 910px
body; shrinking to 420px left the body at 910px.

### C6. Text made invisible by an age fade (1.6.5)
Labels and bearing needles multiplied their alpha by an age-fade factor that
bottoms out at 0.18, so a name drew at 0.166 alpha. **Age fade is meaningful for
a marker and ruinous for text.** Both now carry a brightness floor.

### C7. An escape hatch that could never fire (1.6.5)
A fallback for "the high-interest list is empty" was dead logic: the list seeds
from a bundled 594-object snapshot and is never empty. Removed rather than left
as reassuring but unreachable code.

### C8. A request shape the project's own notes forbade (1.6.6)
The scheduled refresh asked for 90 days of every object in one request. The
project's learned register **already recorded** that the tenant hard-caps results
server-side and that the remedy is to slice by time window. The response was
truncated, the newest fixes were the ones dropped, and because the window start
advanced with the clock the lag was **constant rather than growing**, which is
why it read as "out of date" rather than "frozen".

### C9. A swallowed failure that reported no error (1.6.9)
The health endpoint reported `last_ingest_added: 0` beside
`last_ingest_errors: []` while the picture was **403 hours old**. The HTTP layer
caught every failure by design so one bad slice never killed a cycle, and
nothing counted the catching. A 403 and a genuinely empty window produced
byte-identical output. Now every source counts requests, failures with reason,
raw rows, objects offered and objects kept, and a request failure is reported as
an error. **Still never fatal, no longer silent.**

### C10. A rate that measured oscillation, not motion (1.6.11)
A drift rate taken as a two-point difference over a 30-minute baseline divides
by 1/48 of a day, so it multiplies any disagreement by 48. A slightly eccentric
geostationary orbit swings east and west once per sidereal day without going
anywhere: at e = 0.0022 that is 0.25 degrees of amplitude and 1.58 degrees per
day at its fastest. Replaced with a least-squares fit over a trailing window.
MEASURED on a synthetic object with zero true drift: the old code returns
**+1.567 deg/day**, a plain line **-0.053**, and the fit that models the
oscillation **1.2e-13**.
● **Generalisable:** the panel also showed a total excursion of 5.9 degrees,
  which that rate would cover in 1.54 days. **Two numbers on one screen that
  contradict each other are a defect report.**

### C11. Three findings in one review pass (1.6.16)
● Three captured figures were registered as assets, shipped, and **referenced by
  nothing**.
● Canvas labels drawn to the right of a marker **ran off the right edge** of the
  plot, which on a chart that wraps is exactly where objects collect.
● A page fetched its own API unconditionally, logging a console error when
  opened from a file share.
**Reviewing your own output before handing it over finds things tests do not.**

### C12. A test that rotted on the calendar (found 10 November 2026)
A passing test fixture injected a record at a **hardcoded epoch**. The epoch sat
inside the application's 90-day retention window when the test was written. On
the day the window moved past it, the record began being pruned on merge and the
assertion failed permanently. Nothing in the repository changed; the calendar
did. The build that was sitting ready for upload had gone red without anyone
touching it, and would have failed the platform test stage.

Fixed with an epoch relative to `now`, plus two tests that pin the retention
boundary itself (one day inside survives, one day outside is pruned), which the
suite had never had.

**No automated gate, and the reason is part of the entry.** The textual pattern
(an absolute date in a test) occurs about twenty times in this suite and is
harmless in nineteen of them, because those epochs never reach the prune. A rule
at that signal-to-noise gets silenced within a week. The real condition is
semantic: a fixture epoch that must sit *inside* a window measured from `now`.
The standing rule is a habit, not a checker: **write it relative to `now`.**

**A green suite is evidence about the day it ran, not about the code.** Re-run
the full loop immediately before any upload, however long the tree has been
untouched; "it was green when I built it" is not the same claim.

### C14. A rail that ran under the footer (pre-existing, fixed in 1.7.0)
The page grid's single row was `auto`, so it grew to fit the left rail. The rail
ran on underneath the footer instead of scrolling, and pushed the plot legend
off the bottom of the screen. Visible in every capture since the rail grew;
noticed only when the Shift Brief made the rail longer. `minmax(0,1fr)`.

### C15. A drift rate fitted across a manoeuvre (found prototyping, fixed in 1.7.0)
The 72-hour drift fit straddled a burn and averaged "on station" with
"drifting": 0.22 deg/day for an object drifting at 0.59. Found while building
the evidence for a proposal, not by any check. Fixed by fitting only after the
newest confirmed E-W event; guarded by a red case that asserts the trap is still
in the data, and by the layout probe reading the inspector's drift row.

### C13. Labels drawn blind on top of each other (1.6.18)
Point names on a chart were drawn at a fixed offset to the right of every
marker. In a sparse test view that looks correct. In a real 40-object
neighbourhood five names landed on the same pixels and **none of them could be
read**, and a name near the right edge ran off the canvas entirely.

This is the **third page** in this project to hit the same defect class, after
the main plot and the guide's sandbox, each fixed on the page that failed rather
than across the pattern. The same lesson the `response.ok` family taught three
times over.

Fixed by trying four placements per name and dropping any that cannot be placed
clear of the markers, the other names, the overlaying panel and the canvas edge,
then **reporting the number dropped under the chart**. A label between two
markers belongs to whichever one the eye picks, which is worse than no label.

**The guard is the count, not the pixels.** Canvas text cannot be asserted on
directly, but without avoidance every name always draws, so the dropped count is
zero at every size. The probe asserts a small view hides at least one name and
strictly more than a large one. **When you cannot measure the output, measure
the decision the code made.**

---

## 6. Defects in the checks themselves

The sharpest category. Seven times, the thing that was supposed to catch a
problem was itself the problem.

### D1. One of nine guards could actually fail
A retrospective audit found that of nine checks this project trusted, **exactly
one** had a committed case proving it could go red. Every gate now runs twice:
once against a deliberately bad case it must reject, then against the real
source it must accept. **A clean result from a broken checker proves nothing.**

### D2. A linter that silently ignored its target
`eslint` run from the wrong working directory reports "File ignored because
outside of base path" and **exits 0**, which reads as a pass. The lint step now
carries a permanent canary: a deliberate violation of a rule the plugin genuinely
carries. If the canary is not flagged, the build fails.

### D3. A skipped check reporting a pass
With no `node` installed, the lint step printed a warning and continued. It now
**fails**. An explicit environment variable accepts the gap deliberately and says
so loudly in the output. **A machine that cannot run a check must not be able to
report one.**

### D4. A check that was true by construction
The first guard written for C5 asserted only that the canvas CSS box changed,
which it always did. A resize check must assert the **backing store** matches the
box **and** that the body shrinks as well as grows.

### D5. A reconstructed bug is not the bug
The first attempt to prove the C5 guard reconstructed the old CSS from memory,
and **the guard passed against it**. Reverting the file byte-for-byte from git
history made it fail immediately. **Revert from history, or the proof proves
nothing.**

### D7. The document describing the gate was wrong, twice
● **What happened:** the supply-chain notes recorded the pipeline as **nine**
  stages. It was ten. Corrected in two commits, but only **after the document
  had been shared with another team**. On 5 October, reading a passing run for
  the first time, it turned out to be **eleven**: ten gates then Deploy,
  including a DAST Scan the notes had never named.
● **Cause, and it generalises further than anything else here:** the document was
  **derived exclusively from failed runs**. A run that fails a gate never reaches
  deployment, so a record built only from failures **cannot see the stages that
  appear only on success**.
● **Second cause:** the notes tagged every claim by evidence strength, which is
  the right instinct, but the error survived a review and an external share
  anyway, **because tags are easy to skim past**. Structure a weak claim as a
  question, not as a tagged statement.
● **Fix:** read a green run end to end and correct from it. Then say in the
  document which runs it was built from.
● **Cost:** an incorrect document shared outside the team.

### D8. The linter preset was weaker than the gate claimed (1.6.19)
The SPA lint step ran eslint-plugin-sonarjs with `configs.recommended` and
reported a clean page for months. **That preset switches 65 of the plugin's 295
rules off**, and several of them are in SonarQube's own default JavaScript
profile. The gate was therefore quieter than the scanner it was standing in for,
and nothing in its output said so.

Running the full rule set over the two shipped pages found five real violations
in two default-profile rules: four unused function parameters (S1172) and one
Array constructor (S1528). Three of them had shipped in a build that already
cleared Code Quality, which is the evidence that the tenant profile does not
fail on them, and is also the reason they survived unseen.

Fixed in the source, and the missing rules added to the gate with **their own
red case**: the existing canary only proves the preset is running, so deleting
the added block would have left the step reporting a pass over a weaker gate.

**A preset is curated for the plugin's users, not for your scanner's profile.
List what it leaves out before trusting a green run.**

### D9. A red case that the test's own fixture masked (1.7.1)
The first logging test claimed in its docstring that restoring
`logging.basicConfig` would fail it. Running that revert showed it passing: the
fixture called `configure_logging()` itself, which repaired the very thing under
test. Fixed by testing the import-time configuration directly and keeping the
fixtures out of it; the revert now fails three tests. **A guard's description
of itself is a claim, and only its red case is evidence.**

### D10. A probe that hung instead of failing (1.7.0)
The layout probe walked the Shift Brief with `j` until it reached a given entry,
in an unbounded loop. The red case that broke `j` made it run for ten minutes
instead of failing. Bounded to the number of entries. A guard that cannot fail
cleanly is found only by making it fail.

### D11. A truncated report read as a complete one (1.7.0 to 1.7.1)
The 1.7.0 Code Quality paste listed four code smells and ended with the heading
"Bugs". All four smells were fixed and gated; the bug under that heading was not
seen and shipped again in 1.7.1. **When a pasted report ends on a heading, ask
for the section, or read `sonar-issues.json`, which the job uploads on every
failure.** A fix for a partial report is a partial fix.

### D12. A rejection test that passed for the wrong reason (1.7.4)
`test_manual_elset_rejects_bad_inclination` asserted only that a
`ValidationError` was raised. Once marking, data mode and B* became required,
it raised on the missing fields and stopped testing the inclination at all,
while still passing. Found by reading the fixtures after the suite went green,
not by the suite. **Pin a rejection test to the field it is about**
(`pytest.raises(ValidationError, match="inclination_deg")`), and re-read every
negative test whenever a model gains a required field.

### D13. A probe that skipped what it could not find, and hotspots that pointed at nothing (1.7.4)
The layout probe's affiliation check did `if (!ids.includes(id)) continue;`, so
renaming the demo catalogue numbers would have left it passing while checking
nothing. It now fails on a missing id. In the same pass the Operator Guide's
inspector hotspots turned out to have been wrong since they were drawn: the
capture was scrolled past the identity and state rows the hotspots pointed at.
The capture is now taken unscrolled and the capture script MEASURES every row
and prints the hotspot table. **A check that skips on missing input is a check
that can be renamed out of existence; positions copied from a measurement beat
positions estimated from a picture.**

### D14. An audit proof that "reproduced" a scenario it never built (audit, found in 1.7.5)
`prove.py` F12 ("a 60-day-old commercial fix outranks a fresh government
one") fetched the fresh 18 SDS fix over the window `[REF, REF]`. That window
is empty, so the source made NO request, 18 SDS was never stored, and the
check reported REPRODUCES against a store that held NorthStar alone. It said
so against 1.7.3 and 1.7.4. The FINDING stood on reading the code (no age
limit, commercial first), but its offline proof was vacuous. Found when 1.7.5,
which ages old sets out, still "reproduced" it. Now ingested like every other
scenario, the check refuses to judge unless both providers are held, and
re-run against 1.7.4 from git it genuinely reproduces. **A reproduction must
assert its own preconditions; "the bad thing happened" proves nothing if the
good thing was never there.**

### D15. Two new tests that could not fail (1.7.5)
`redcases_1_7_5.py` put eleven pre-1.7.5 behaviours back one at a time. Two
tests stayed green. The QUIET test held one object, so the belt's reference
time moved with it and a 20-hour silence measured as zero. The conjunction
test gave every provider the same newest epoch, so the raw store and the
resolved view chose the same provider by dictionary order. Both fixtures now
carry what makes the difference observable (a second, current object; the
outlier newest, as it was live), and all eleven go red. **Write the red case
before believing the green; a fixture with a tie in it tests the tie-break.**

### D6. A false positive in our own checker
The zero-fraction pattern `\d+\.0` matched `127.0.0.1`. Found by running the
checker over our own tree before publishing it. Fixed with a trailing negative
lookahead.

---

## 7. False positives, recorded rather than silenced

Two findings were judged wrong and the rule was kept anyway. **How a team
handles a false positive is a stronger signal than how it handles a true one.**

### E1. A text rule meeting a different API
`WEB-GETATTR-DATA` fires on `locator.getAttribute("data-cc")` in Playwright
driver code. A Playwright `Locator` is a handle, not a DOM element, and has no
`dataset`; `getAttribute` is the only API. The rule is a text match and cannot
see the receiver's type.
**Resolution:** scope the web rules to shipped page sources, not browser driver
scripts, and write the exception down. Weakening the pattern would lose the real
rule in page code; an inline suppression teaches people to reach for
suppressions.

### E2. A text rule meeting ordinary English
`\bwindow\.` exists to catch `window.innerWidth`. It fired five times on a
documentation page, every one of them a sentence ending "...over a trailing
window." It recurred once more on a later edit.
**Resolution:** reword the sentences. **Prose has synonyms; code does not.** The
general rule: before adding a documentation page to a scanned tree, run the
scanner over it and expect the text rules to argue.

---

## 8. What to put in place on day one

Ordered by return on effort, from this project's evidence.

1. **Reproduce the platform's test stage exactly, locally.** Same install
   command, same test command, same coverage floor. A loop that differs from the
   gate is a loop that lies.
2. **Give every gate a red case, committed.** A gate that stops rejecting its own
   case fails the build. Before this change, one of nine guards here was proved;
   the other eight were taken on trust. [D1]
3. **Make a skipped check fatal**, with a loud, explicit opt-out. [D3]
4. **Add a canary to any tool that can silently no-op**, linters above all. [D2]
5. **Add one rule per finding, forever.** Each rule in `SNIFFS.md` exists because
   something got through. The list only grows.
6. **Run your own rules over your own tree, including the directories the
   platform does not scan.** [B3]
7. **Print what the coverage figure excludes.** 94% with the single-page
   application excluded is not 94% of the risk.
8. **Prove a guard by reverting from git history, byte for byte.** [D5]
9. **Run a real scanner against the real host before upload** if you can reach
   it. Everything above is a proxy for that, and proxies miss half the rules. [A2]

---

## 9. Machine-readable register

```json
{
  "project": "PSIRENS",
  "covers": {"from": "1.4.14", "to": "1.7.2",
             "dates": "2026-08-23/2026-11-17", "issues": 43},
  "categories": {
    "A": "rejected by the platform quality gate",
    "B": "caught by local gates before upload",
    "C": "found by review, screenshot or live operation",
    "D": "defects in the checks themselves",
    "E": "false positives, recorded not silenced"
  },
  "issues": [
    {"id": "A1", "rule": "python:S107", "title": "parameter count above 13",
     "fixed_in": "1.5.1", "cost": "upload cycle",
     "guard": "tools/ast_gates.py parameter pass with a red case"},
    {"id": "A2", "rule": "sonarjs throw-vs-rejected-promise",
     "title": "return Promise.reject inside a then callback", "fixed_in": "1.5.3",
     "cost": "upload cycle", "guard": "text rule in the build loop",
     "note": "eslint-plugin-sonarjs does not carry this rule"},
    {"id": "A3", "rule": "web:S*/contrast", "title": "text contrast below WCAG AA",
     "fixed_in": "1.6.8", "cost": "scan cycle",
     "guard": "WEB-CONTRAST in tools/sniff_check.py",
     "note": "analyser reads an rgba background as its opaque base"},
    {"id": "A4", "rule": "python loop-variable capture",
     "title": "lambda capturing a loop variable", "fixed_in": "1.6.12",
     "cost": "scan cycle", "guard": "PY-LOOP-LAMBDA AST rule"},
    {"id": "A5", "rule": "python:S1192", "title": "string literal duplicated three times",
     "fixed_in": "1.7.1", "cost": "upload cycle", "guard": "PY-DUP-LITERAL, calibrated on 1.6.12 and 1.7.0"},
    {"id": "A6", "rule": "python:S5886", "title": "dataclasses.replace returned from an annotated function",
     "fixed_in": "1.7.1", "cost": "upload cycle", "guard": "PY-REPLACE-RETURN"},
    {"id": "A7", "rule": "Web:S6843", "title": "a role on a canvas element",
     "fixed_in": "1.7.1", "cost": "upload cycle", "guard": "WEB-CANVAS-ROLE"},
    {"id": "A8", "rule": "Web:S6819", "title": "role=img instead of img or svg",
     "fixed_in": "1.7.1", "cost": "upload cycle", "guard": "WEB-IMG-ROLE"},
    {"id": "A9", "rule": "python:S1244", "title": "equality against a float literal",
     "fixed_in": "1.7.2", "cost": "upload cycle", "guard": "PY-FLOAT-EQ, calibrated on 1.6.12 and 1.7.1"},
    {"id": "A10-A13", "rule": "python:S7517", "title": "unpacking while iterating a tuple-keyed dict (x4)",
     "fixed_in": "1.7.6", "cost": "upload cycle", "guard": "PY-DICT-UNPACK, calibrated on 1.6.12, 1.7.3 and 1.7.5"},
    {"id": "A14", "rule": "python:S8517", "title": "sorted(...)[0] instead of min()",
     "fixed_in": "1.7.6", "cost": "upload cycle", "guard": "PY-SORTED-INDEX; ruff FURB192"},
    {"id": "A15-A16", "rule": "python:S7508", "title": "sorted, then sorted again (x2)",
     "fixed_in": "1.7.6", "cost": "upload cycle", "guard": "PY-REDUNDANT-CALL; ruff C4"},
    {"id": "A17", "rule": "python:S7504", "title": "list() around a dict being mutated in the loop",
     "fixed_in": "1.7.6", "cost": "upload cycle", "guard": "PY-LIST-ITER (ruff PERF101 declines this form)"},
    {"id": "A18", "rule": "bandit:B101", "title": "assert in shipped code (x3, store.py), CATO 97",
     "fixed_in": "1.7.14", "cost": "CATO score", "guard": "ruff S over src; PY-ASSERT"},

    {"id": "B1", "rule": "python:S3776", "title": "cognitive complexity 18 in load_config",
     "fixed_in": "1.6.6", "cost": "none", "guard": "tools/ast_gates.py"},
    {"id": "B2", "rule": "python:S3776", "title": "the complexity checker tripped its own rule",
     "fixed_in": "n/a", "cost": "none", "guard": "tools/ast_gates.py"},
    {"id": "B3", "rule": "python:S3776", "title": "complexity 18 outside sonar.sources",
     "fixed_in": "n/a", "cost": "none", "guard": "own rules run over the whole tree"},
    {"id": "B4", "rule": "sonarjs/no-nested-conditional", "title": "nested ternary",
     "fixed_in": "1.6.4", "cost": "none", "guard": "eslint-plugin-sonarjs"},
    {"id": "B5", "rule": "sonarjs/pseudo-random", "title": "Math.random in a shuffle",
     "fixed_in": "1.6.15", "cost": "none", "guard": "eslint-plugin-sonarjs"},
    {"id": "B6", "rule": "sonarjs/no-nested-template-literals",
     "title": "nested ternary inside a nested template literal",
     "fixed_in": "1.6.15", "cost": "none", "guard": "eslint-plugin-sonarjs"},

    {"id": "C1", "title": "unchecked response.ok on /api/conjunctions",
     "fixed_in": "1.5.2", "cost": "hidden error cause", "pattern": "response-ok"},
    {"id": "C2", "title": "unchecked response.ok on /api/coplanar",
     "fixed_in": "1.6.4", "cost": "two builds", "pattern": "response-ok",
     "note": "the C1 fix was applied to the route, not the pattern"},
    {"id": "C3", "title": "unchecked response.ok on /api/hrr",
     "fixed_in": "1.6.13", "cost": "silent empty plot", "pattern": "response-ok"},
    {"id": "C4", "title": "identifier displayed as a name", "fixed_in": "1.6.1",
     "cost": "visible defect", "guard": "resolve server-side so consumers agree"},
    {"id": "C5", "title": "modal could only grow (min-height:auto on a canvas)",
     "fixed_in": "1.6.3", "cost": "shipped in 1.6.2", "guard": "browser layout probe"},
    {"id": "C6", "title": "age fade made text invisible", "fixed_in": "1.6.5",
     "cost": "visible defect", "guard": "brightness floor for text"},
    {"id": "C7", "title": "escape hatch that could never fire", "fixed_in": "1.6.5",
     "cost": "dead logic", "guard": "review"},
    {"id": "C8", "title": "unsliced request truncated server-side",
     "fixed_in": "1.6.6", "cost": "14 days of stale data",
     "guard": "slice, pause, and treat a full response as truncated"},
    {"id": "C9", "title": "swallowed HTTP failure reported no error",
     "fixed_in": "1.6.9", "cost": "403 hours of silence",
     "guard": "per-source counters surfaced on the health endpoint"},
    {"id": "C10", "title": "two-point rate measured libration as drift",
     "fixed_in": "1.6.11", "cost": "wrong operational number",
     "guard": "least-squares fit with a committed red case"},
    {"id": "C11", "title": "orphaned assets, clipped labels, console error on file://",
     "fixed_in": "1.6.16", "cost": "none", "guard": "probe asset and render checks"},

    {"id": "D1", "title": "one of nine guards could actually fail",
     "fixed_in": "1.6.3 retrospective", "guard": "every gate runs against a red case first"},
    {"id": "D2", "title": "linter silently ignored its target and exited 0",
     "guard": "permanent canary in the lint step"},
    {"id": "D3", "title": "a skipped check reported a pass",
     "guard": "skipped is fatal unless explicitly allowed"},
    {"id": "D4", "title": "a check that was true by construction",
     "guard": "assert the backing store and that it shrinks"},
    {"id": "D5", "title": "a reconstructed bug is not the bug",
     "guard": "revert byte-for-byte from git history"},
    {"id": "D6", "title": "false positive in our own zero-fraction pattern",
     "guard": "run the checker over our own tree before publishing"},
    {"id": "D7", "title": "the document describing the gate was wrong twice (nine, ten, eleven stages)",
     "cost": "an incorrect document shared outside the team",
     "guard": "read a green run end to end; record which runs the document was built from",
     "note": "a record built only from failed runs cannot see stages that appear only on success"},

    {"id": "C12", "title": "test fixture epoch rotted past the retention window",
     "found_in": "1.6.16", "fixed_in": "1.6.17", "cost": "a build that would have failed the platform test stage",
     "guard": "write a fixture epoch relative to now; pin the retention boundary with tests",
     "note": "no automated gate: the textual pattern is harmless in 19 of 20 occurrences, so a rule at that noise level gets silenced"},

    {"id": "C13", "title": "chart labels drawn blind, overprinting each other",
     "fixed_in": "1.6.18", "cost": "an unreadable chart in exactly the case it exists for",
     "guard": "probe asserts a small view drops more labels than a large one",
     "note": "third instance of the same defect class on a third page; the guard measures the decision, not the pixels"},

    {"id": "C14", "title": "grid row grew to fit the rail, which ran under the footer",
     "fixed_in": "1.7.0", "cost": "legend off screen in every build", "guard": "visual review of captures"},
    {"id": "C15", "title": "drift rate fitted across a manoeuvre",
     "fixed_in": "1.7.0", "cost": "a rate a third of the real one on screen",
     "guard": "red-case test plus the probe reading the drift row"},
    {"id": "D9", "title": "a red case masked by the test's own fixture",
     "fixed_in": "1.7.1", "cost": "a guard that claimed to bite and did not",
     "guard": "every red case is run, never asserted"},
    {"id": "D15", "title": "two new tests passed against the behaviour they guard (single-object belt; tied epochs)",
     "fixed_in": "1.7.5", "cost": "two guards that could not fail",
     "guard": "redcases_1_7_5.py reinstates each old behaviour; 11 of 11 go red"},
    {"id": "D14", "title": "audit proof reproduced F12 against a store missing the provider it compared",
     "fixed_in": "1.7.5", "cost": "a finding reported as reproduced on evidence that did not exist",
     "guard": "scenario asserts its own preconditions; re-run against 1.7.4 from git"},
    {"id": "D13", "title": "probe skipped a missing id; guide hotspots pointed at rows the capture did not show",
     "fixed_in": "1.7.4", "cost": "a check that could be renamed away; a guide figure wrong since it was drawn",
     "guard": "fail on missing input; hotspots measured by the capture script"},
    {"id": "D12", "title": "rejection test passed on a different missing field",
     "fixed_in": "1.7.4", "cost": "two tests silently stopped testing their field",
     "guard": "pytest.raises(..., match=<field>) on every negative test"},
    {"id": "D11", "title": "a truncated quality report read as a complete one",
     "fixed_in": "1.7.2", "cost": "a second upload cycle",
     "guard": "a report ending on a heading is incomplete: ask, or read sonar-issues.json"},
    {"id": "D10", "title": "probe walked an unbounded loop and hung on failure",
     "fixed_in": "1.7.0", "cost": "ten minutes and no verdict", "guard": "bounded walk"},
    {"id": "D8", "title": "linter preset silently weaker than the scanner profile",
     "fixed_in": "1.6.19", "cost": "five real violations sat unseen behind a green gate",
     "guard": "add the default-profile rules the preset omits, with their own red case",
     "note": "eslint-plugin-sonarjs recommended switches 65 of 295 rules off; measured, not assumed"},

    {"id": "E1", "title": "text rule fires on a Playwright Locator",
     "resolution": "scope web rules to shipped pages, record the exception"},
    {"id": "E2", "title": "window. rule fires on prose",
     "resolution": "reword the prose, keep the rule"}
  ]
}
```

---

## 10. Where the evidence lives

● `SNIFFS.md` — the rulebook, with `tools/sniff_check.py` as its runnable,
  stdlib-only checker and a `--self-test` that proves each rule rejects a bad
  case and accepts a good one.
● `CLAUDE.md`, "Open items" — the full causal record for each entry above,
  including the measured figures quoted here.
● `CONTEXT-001.md` — the learned register of verified upstream and platform
  facts, including the slicing rule C8 violated.
● `simulate-pipeline.sh` — the build loop, where every guard in this ledger runs.
● `gate-cases/` — the committed red cases from D1.
● `tools/layout_probe.js` — the browser guard for C5, C2, C11 and the rest of
  the interface behaviour.
● Git history — every fix above is a single commit whose message states the
  cause, the fix and the proof.
