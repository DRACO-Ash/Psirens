# Code-quality sniffs: a compendium

Every rule here has bitten a real Bluestaq App Store build, or was caught by a
local gate before it could. Nothing is included because a style guide somewhere
recommends it.

Written to be handed to another project. It comes with a checker
(`tools/sniff_check.py`) that runs anywhere Python 3.9 runs, with no pip
install, no node and no network, so "check our code against this" is a command
rather than a reading exercise.

Owner: Ash Higgins, Technical Director, Bluestaq Ltd. Classification: Not
Classified. Source: PSIRENS 1.5.0 to 1.6.5, August to September 2026.

Evidence tags. **UPLOAD** means it failed a real platform upload and cost a
cycle. **GATE** means a local check caught it before upload. **MEASURED**
means a number in this document was produced by running something, and the
command is given. Anything unverified is marked TBC and named as such.

## 1. Use it

```sh
python3 tools/sniff_check.py src tests        # check a tree
python3 tools/sniff_check.py --self-test      # prove the checker works
python3 tools/sniff_check.py --list           # list the rules
```

Exit 0 clean, 1 findings, 2 bad usage. Wire it into CI as a gate.

**Run `--self-test` first, and in CI.** It asserts that every rule rejects a
case it must reject AND accepts a case it must not flag, including that no text
rule fires on a comment describing it. MEASURED: 23 of 23 assertions pass. A
checker nobody has watched fail is not a checker, and this estate has shipped
two checks that could not fail (section 5).

## 2. The single most useful fact in this document

**MEASURED, 21 September 2026.** Five of ten common sniffs are NOT reported by
`eslint-plugin-sonarjs` under its recommended configuration, even with the
plugin fully enabled. Reproduce by linting one violating snippet per rule:

| Sniff | eslint-plugin-sonarjs |
|---|---|
| Nested ternary | FLAGGED (`sonarjs/no-nested-conditional`) |
| Invariant return | FLAGGED (`sonarjs/no-invariant-returns`) |
| Cognitive complexity | FLAGGED (`sonarjs/cognitive-complexity`) |
| Negated condition | FLAGGED (`no-negated-condition`, an eslint core rule) |
| `window.` over `globalThis` | **NOT FLAGGED** |
| `getAttribute("data-x")` | **NOT FLAGGED** |
| `return Promise.reject()` in a `then` | **NOT FLAGGED** |
| Zero-fraction literal | **NOT FLAGGED** |
| Duplicated string literal | **NOT FLAGGED** |
| Identical functions | **NOT FLAGGED** |
| Text contrast below WCAG AA | **NOT FLAGGED** (it is a CSS/HTML rule, not JS) |

So a green eslint run is not a prediction of a green SonarQube gate. Treat the
linter as covering a different, overlapping set, and cover the rest with text
rules or the real scanner. Assuming otherwise cost this project an upload
cycle: the `Promise.reject` rule that rejected build 1.5.2 is in the NOT
FLAGGED column.

## 3. The rules

Each carries what it is, how it bit, and the fix that works.

### 3.1 Parameter count above 13 (`python:S107`) UPLOAD

Build 1.5.0 was rejected on a function taking 18 parameters. It had grown one
argument at a time, each addition individually reasonable.

**Fix:** group related arguments into a value object, never widen the
signature. A `NamedTuple` of the provenance fields took that function from 18
to 12 and read better.

**Trap:** counting by eye. Keyword-only arguments, `*args` and `**kwargs` all
count. `sniff_check` counts them the way the rule does.

### 3.2 Cognitive complexity above 15 (`python:S3776`) GATE

Caught twice in one afternoon on new code, at 17 and at 24. A function scores 1
for each construct that breaks the linear flow, plus 1 more for every level it
is nested inside, so nesting is punished far harder than length. A 200-line
function of flat statements scores 0.

**Fix:** extract the inner loop or the repeated guard. The 24 was four copies
of one permission check inlined into four routes; one helper returning the
refusal response took it to single figures.

**On the checker's number.** `sniff_check` uses the reference
`cognitive_complexity` package when it is installed and says so in its output.
Without it, it falls back to a stdlib implementation. MEASURED: the fallback
agrees exactly on every elementary construct and on 384 of 386 functions in
this repository, but OVER-counts deeply nested `if/elif` chains, so a finding
from the fallback near the cap deserves a second look. The tool names which
engine produced each number rather than leaving you to guess.

### 3.3 `return Promise.reject(error)` inside a `then` callback UPLOAD

Build 1.5.2 was rejected on this. Inside a `then`, throwing and returning a
rejected promise reject identically, and Sonar prefers the throw.

**Fix:** `throw error`.

**Trap, MEASURED:** the eslint plugin does not carry this rule (section 2), so
a green lint proves nothing here. Only the real scanner or a text rule catches
it.

### 3.4 Unchecked `response.ok` before `.json()` UPLOAD-adjacent, twice

Not a Sonar rule. A reliability defect that reached a deployed build twice, so
it earns its place.

```js
fetch(url).then(r => r.json())        // an HTML error body rejects here
```

A non-JSON error body makes `.json()` reject, the catch fires, and the only
thing the interface can say is "service unavailable". The real cause, which
the server put in the response, never reaches the screen.

**Fix:**

```js
fetch(url).then(r => {
  if (r.ok) { return r.json(); }
  throw new Error(messageFor(r.status));   // a function, not a nested ternary
})
```

**The expensive lesson.** We fixed this on one route in 1.5.2 and shipped the
identical defect on a second route for two more builds, because the fix was
applied to the route that failed rather than to the pattern. **When a defect is
found, grep the file for the pattern, not the route.**

### 3.5 Nested ternaries (`sonarjs/no-nested-conditional`) GATE

Caught on the very commit that was fixing 3.4: a status-to-message map written
as `a?x:b?y:z`.

**Fix:** a small function with early returns. Worth noting the gate paid for
itself on the change that was tightening it.

### 3.6 `window.` instead of `globalThis` (`S6643`)

**Fix:** `globalThis`, or better the specific object: `navigator`, `document`,
`location`. NOT FLAGGED by the plugin.

### 3.7 ARIA landmark role where a native element exists (`S6819`)

`role="region"` on a `div` when `<section>` exists; likewise `banner` for
`<header>`, `navigation` for `<nav>`, `contentinfo` for `<footer>`,
`main` for `<main>`, `form` for `<form>`.

**Fix:** use the element. It is shorter and it carries the semantics for free.
NOT FLAGGED by the plugin.

### 3.8 `getAttribute("data-x")` instead of `dataset`

**Fix:** `element.dataset.x`. NOT FLAGGED by the plugin.

### 3.9 Negated condition with an else branch (`no-negated-condition`)

`if (!ok) { ... } else { ... }` forces the reader to invert twice.

**Fix:** swap the branches. This one IS an eslint core rule, so enable it
explicitly; it is not in the sonarjs recommended set.

### 3.10 Zero-fraction numeric literals

Write `2`, not `2.0`.

**Trap, found by running this checker on our own tree:** the naive pattern
`\d+\.0` matches `127.0.0.1`. The rule needs a negative lookahead for a
following dot as well as a following digit. A rule that cries wolf on an IP
address gets switched off, and then it catches nothing.

### 3.12 Text contrast, and the translucent-background trap UPLOAD-adjacent

**The finding:** "Text does not meet the minimal contrast requirement with its
background", raised against one line of a stylesheet.

**The trap.** The rule that produced it looked like this:

```css
.stale { background: rgba(198,124,0,.13); color: #F4DCAE }
```

On a near-black page that renders at **13.35:1**, which is excellent. A static
analyser flagged it anyway, and it was right to. It cannot know what sits
behind a translucent tint, so it reads the declaration as the opaque colour
`#C67C00`, and light amber on mid amber is **2.50:1**.

MEASURED, both numbers, with the WCAG 2.1 relative-luminance formula. The
checker reproduces the analyser's reading and reports the same line.

**Fix:** declare the composited colour instead of the tint.

```css
.stale { background: #17100A; color: #F4DCAE }     /* 14.08:1, and honest */
```

The rendering is identical to the eye. The difference is that the declaration
now says what the user actually sees, so a human reading the source, an
analyser, and the screen all agree.

**Do not argue with the analyser on this one.** "It composites fine at runtime"
is true and useless: the rule exists because translucent text backgrounds are
genuinely fragile, and the next person to put that component on a lighter
surface gets 2.5:1 for real.

**The wider lesson, which cost a second instance.** The same pattern had
already been repeated in a component added the following day
(`.crow.on{background:rgba(198,124,0,.16)}` with the colour on a child
selector). Grep the whole stylesheet for `background:rgba(` paired with text,
not just the line the tool named. See 3.4 for the same lesson learned the
expensive way.

**Checker support and its limit.** `sniff_check.py` implements the WCAG ratio
and flags any block below 4.5:1, treating an `rgba()` background as its opaque
base. It compares only colours declared in the SAME block, so a colour set on
a child selector is invisible to it. That is exactly how the second instance
escaped, and it is why the grep above still matters.

### 3.11 Python correctness sniffs the checker also covers

Not Sonar-gate failures for us, but exactly decidable and worth having:

● **Mutable default argument** (`def f(x=[])`): created once, shared by every
  call. Use `None` and build inside.
● **Bare `except:`**: swallows `KeyboardInterrupt` and `SystemExit`. Name the
  exceptions.
● **`== None`**: use `is None`. `==` invokes `__eq__`, which a class can
  define, so the two are not equivalent.

## 4. Rules no cheap checker can decide

Listed so nobody mistakes a clean `sniff_check` run for a clean gate. These
need the real scanner:

● duplicated blocks across files (needs cross-file analysis);
● security hotspots and taint tracking;
● unused private members and dead stores (needs type resolution);
● test coverage, which is a gate condition, not a rule;
● **TBC:** the severity threshold at which the platform's dependency and
  container scans actually fail a build. Never triggered for us, so never
  observed. Convert this to fact the first time one fires.

## 5. The meta-rules, which cost more than any sniff

Four habits, each learned from a real failure. They are the reason the rules
above are enforced rather than merely written down.

### 5.1 A verification that cannot fail is worse than none

It converts an unknown into a false assurance. Two examples from this project:

● an eslint step reported success while linting **nothing at all**, because
  eslint resolves its base path from the working directory and silently ignores
  a file outside it;
● a browser check asserted that a canvas element's CSS box changed after a
  resize, which is true by construction, and passed while a real layout defect
  was live. That build reached Active status and the owner found the bug.

**The fix that works:** every gate runs twice, once against a case it MUST
reject and once against the real source. `sniff_check --self-test` does exactly
this. If the deliberately bad case stops being flagged, the build fails.

### 5.2 A check that cannot run must not report a pass

A skipped step exiting zero is the same failure in a different costume. Make an
unrunnable check fatal, with a named environment variable to accept the gap
deliberately and loudly.

### 5.3 A reconstructed bug is not the bug

Proving a new guard catches an old defect means reverting the defect **from
version control, byte for byte**. We first reconstructed a CSS regression from
memory, the new probe passed against the reconstruction, and for a moment we
had a guard that looked proven and was not. The real revert failed it
immediately.

### 5.4 Beware the escape hatch that cannot fire

A conditional that was written to handle a case that never occurs is not
defensive, it is dead logic that has never been exercised and will not work
when it finally runs. One in this project guarded against an empty watchlist
that a bundled snapshot makes impossible. Delete it, or make the case real and
test it.

### 5.6 A swallowed failure must still be counted

Resilient code catches an error so one bad call does not kill a cycle. That is
correct. What is not correct is catching it and reporting nothing, because the
health signal then cannot tell a rejected upstream from a quiet one.

MEASURED, PSIRENS 26 September 2026: a live health endpoint reported
`last_ingest_added: 0` beside `last_ingest_errors: []` while the picture was
403 hours old. The error list was built from exceptions that reached the
caller, and the HTTP layer below it caught every failure by design. A 403 and
a genuinely empty window produced byte-identical output.

The rule: wherever a `try/except` exists so the loop survives, add a counter
beside it and put the counter in the health payload with the REASON. Then
prove it, by making the failure happen and asserting the report is not empty.
An assertion that the list is non-empty, written against the live symptom, is
the whole test.

### 5.5 Watch what the coverage figure excludes

MEASURED: 93.72 per cent across 157 tests, with the single-page application
excluded from the coverage metric entirely. That file produced both of the
defects that reached a deployed build. A high number that omits the risky
surface is reassurance about the wrong thing. Print the exclusions next to the
figure.

## 6. Adopting this on an existing project

1. Copy `tools/sniff_check.py` in. One file, no dependencies.
2. `python3 tools/sniff_check.py --self-test` and read the manifest.
3. `python3 tools/sniff_check.py <your source>` and triage. Expect false
   positives on generated files; exclude directories rather than weakening
   rules.
4. Wire it into CI as a gate, with the self-test running first.
5. When a platform gate rejects a build, fix the finding AND add the local
   check that would have caught it, then prove the new check fails on the thing
   it catches. One rule per cycle is a treadmill; one gate per rule learned
   ends it.

**MEASURED on the source project, 25 September 2026:** clean. 0 findings
across 24 files in `src` and `tests`, with `WEB-CONTRAST` added after a real
Code Quality finding (3.12). Getting there from the first run took
two fixes: a false positive in the zero-fraction pattern, and one real
violation in a tool that SonarQube never analyses and would never have caught.
