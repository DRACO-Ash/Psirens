# PSIRENS quality ledger

Every code-quality issue raised against this application during development,
what caused it, how it was fixed, and what now stops it recurring.

Classification: Not Classified. Owner: Ash Higgins, Bluestaq Limited.
Covers builds 1.4.14 to 1.6.16, 23 August to 5 October 2026, 30 issues.

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
  "covers": {"from": "1.4.14", "to": "1.6.16",
             "dates": "2026-08-23/2026-10-05", "issues": 30},
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
