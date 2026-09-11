# Improvement plan (Teach Bob learning loop)

Produced by the `learn-from-feedback` skill from the retrospective corpus.
**Proposal only. Nothing in this file has been applied.** Approve the items you
want and they become one batched change.

## Corpus and its limits, stated first

**One report.** `RETROSPECTIVE.md`, Ash Higgins, 2026-09-09, covering PSIRENS
1.5.0 to 1.6.3 and the derived LEO starter kit.

The skill says not to run on a single report, because one report is a data point
and not yet a theme, and to use it only if it names something sharp and
specific. It qualifies on that exception and nothing more: it names a shipped
defect on a deployed build, an audited count of unproved guards, and the same
failure mode occurring twice in twelve days. Everything below is therefore a
finding about **this project**, not about engineering in general. A theme is
only a theme when it recurs across stacks and archetypes, and with one Python
and FastAPI container service there is no cross-stack evidence here at all.

**The baseline in scope is small, and it is not the Foundations bundle.** This
repository has no `.claude` skills tree. What the report names as its baseline
is `CLAUDE.md`, `CONTEXT-001.md` and `simulate-pipeline.sh`. Items marked
UPSTREAM below are ones I believe belong in the Foundations bundle instead; I
cannot see that repository, so they are written to be carried there by hand.

## The measurement that shapes this plan

The skill's guardrail is to prefer an executable guard to a written rule, and to
watch the residuals ratio: when prose additions outpace executable ones, stop
folding and start enforcing. Counted in the tree today:

| Measure | Count |
|---|---|
| Prose open items carried in `CLAUDE.md`, unscheduled | 9 |
| Executable guards in `simulate-pipeline.sh` that can fail a build | 9 |
| Of those, guards with a committed case proving they can go red | **1** |
| Lines of prose standard (`CLAUDE.md` and `CONTEXT-001.md`) | 523 |
| Lines of executable loop (`simulate-pipeline.sh`) | 124 |

The ratio is at the point the skill warns about. Four to one prose over
executable by line count, nine unscheduled residuals, and eight of nine guards
unproved. **So this plan deliberately proposes almost no new prose.** Seven of
the nine items are checks that fail; the two that cannot be made executable are
marked as unenforced conventions, as the skill requires, so nobody mistakes an
unrun sentence for a guard.

There is a self-referential trap worth naming. The report's leading finding is
that our lessons live as prose that cannot fail a build. Responding to it by
adding more prose would reproduce the defect while appearing to address it. The
resize lesson is **already** written in `CLAUDE.md`, in detail, and it prevented
nothing.

## Read first: where the tooling or the standard cost us

The skill says to read this section before the six lenses and separately,
because it is the only part written about the standard by someone who cannot see
it from the inside.

### T1. A skipped check exits zero
**Target:** `simulate-pipeline.sh`, the page-lint step.
**Change:** make an unrunnable check a hard failure, overridable only by an
explicit `ALLOW_SKIPPED_SPA_LINT=1`. Print which checks ran and which did not in
the final summary line.
**Rationale:** the warning was made loud during the window, which helps a human
reading the scroll and does nothing for a machine or a hurried human. A build
that could not run a check should not be able to report a pass.
**Evidence:** "A skipped step still exits zero... a machine that cannot run a
check should not be able to report a pass" (Ash Higgins, 2026-09-09).
**Size:** small, under an hour. **Executable.**

### T2. The local lint proxy is measurably incomplete, and should say so at runtime
**Target:** `simulate-pipeline.sh` summary output.
**Change:** have the green line name what the loop does **not** cover: no real
SonarQube, the page excluded from coverage, no layout or runtime check unless
item G3 is adopted.
**Rationale:** the rule that failed one upload is not carried by the plugin at
all, even with all 279 of its rules enabled. That was measured, not assumed. The
danger is not the gap, it is that a green line reads as full cover.
**Evidence:** "A proxy trusted as equivalent to the real thing is worse than no
proxy, because it converts an unknown into a false assurance" (Ash Higgins,
2026-09-09).
**Size:** small. **Executable**, in that the output changes; the honesty is
still prose.

### T3. Get the authoritative scanner into the loop
**Target:** `simulate-pipeline.sh`, plus network access.
**Change:** run a real `sonar-scanner` against the tenant host before upload.
**Rationale:** three upload cycles, two rejections and one measured-incomplete
proxy all trace to this one absence. Every local gate exists as a substitute
for it.
**Evidence:** "Buy the real signal rather than approximating it" (carried
forward item 3, Ash Higgins, 2026-09-09), and an open item that predates this
window.
**Size:** medium, and **blocked**: needs a reachable host and credentials, which
this environment does not have. **Needs the owner**, not an engineer.

### T4. Evidence tags are skimmed past
**Target:** any document written to leave the project.
**Change:** before an external share, extract every claim tagged INFERENCE or
TBC into a checklist the author confirms. This is scriptable: the tags are
literal strings.
**Rationale:** the supply-chain document tagged every claim, which was the right
instinct, and the nine-versus-ten stage error still survived review and an
external share.
**Evidence:** "A document tagged by evidence strength still reads as fact"
(Ash Higgins, 2026-09-09).
**Size:** small. **Executable.**

## Read second: the guards section

Each item here names a check this project already has and cannot prove.

### G1. Commit a red case for the complexity and parameter gates
**Target:** new `tests/gate-cases/` plus a step in `simulate-pipeline.sh`.
**Change:** commit two deliberately bad functions, one over cognitive complexity
15 and one at 14 parameters, in a directory the AST gate scans in a second pass.
The build fails if the gate does **not** flag both. This is exactly the canary
pattern that already works for the page linter, applied to the gate that has
never proved itself.
**Rationale:** both gates were demonstrated once, against throwaway probes that
were never committed. Nothing in the tree proves they still work. They did catch
two real violations in the derived kit, which is evidence, but it is a session
transcript rather than a test.
**Evidence:** "Cognitive complexity cap: not proved... Parameter-count cap: not
proved" (Ash Higgins, 2026-09-09).
**Size:** small, one to two hours. **Executable.** Highest value per hour in
this plan.

### G2. Commit a red case for the four page text checks
**Target:** a committed fixture fragment plus a second pass in the loop.
**Change:** one small HTML fragment containing exactly one violation of each of
the four patterns. The loop greps the fixture and fails if any pattern is not
found, then greps the real page as it does today.
**Rationale:** no committed case makes any of the four fire, and one of them has
already produced a false positive by matching its own explanatory comment, which
shows the patterns are fragile enough to need testing.
**Evidence:** "The four source-text checks on the served page: not proved"
(Ash Higgins, 2026-09-09).
**Size:** small. **Executable.**

### G3. Commit the layout probe
**Target:** a new committed browser check, wired into the loop.
**Change:** drive the served page headlessly, resize the modal larger and then
smaller, and assert that the canvas backing store matches its box within a
pixel and that the body never exceeds the modal. Both directions, because the
bug only appeared on the shrink.
**Rationale:** this is the gap that most deserves closing. The check that found
the 1.6.3 bug was never committed, so the protection now standing is the same
prose that let 1.6.2 through to Active.
**Evidence:** "Layout and resize behaviour: no guard at all" (Ash Higgins,
2026-09-09).
**Feasibility, checked:** node 22 and a preinstalled Chromium are present in
this environment, so the probe can run; Playwright is not in the test virtual
environment and must not go into `requirements.txt`, because the platform test
stage installs that file and does not run a browser. It belongs beside the page
lint as an optional step under item T1's explicit-skip rule.
**Size:** medium, half a day including the skip handling. **Executable.**

### G4. Stop the coverage figure reassuring about the wrong surface
**Target:** `simulate-pipeline.sh` output, and the loop's structure.
**Change:** print the coverage figure alongside an explicit line naming what is
excluded from it. Do **not** chase coverage of the page itself; see D1.
**Rationale:** 93.72 per cent across 150 tests, with the served page, the
surface that produced both of this window's shipped defects, excluded from the
metric entirely.
**Evidence:** "The number is high and is reassuring about the wrong thing"
(Ash Higgins, 2026-09-09).
**Size:** small. **Executable.**

## From the six lenses

### L1. Put the LEO starter kit under version control
**Target:** a new repository.
**Change:** commit the kit, currently a zip and a checksum with 150 tests and a
green pipeline.
**Rationale:** it has no history, no issue trail and no route to receive a fix,
and it has already been handed to a third party.
**Evidence:** "Put the handover kit under version control" (improvements, Ash
Higgins, 2026-09-09).
**Size:** small. **Needs the owner**: where it should live is a decision, not an
engineering choice.

### L2. Treat a rejected upload as a full report
**Target:** `CLAUDE.md`, packaging section.
**Change:** one line: read every stage of a rejection before starting the fix,
because the gates are not fail-fast and one upload reports them all.
**Rationale:** the fix cycle ran one rule at a time although the information to
fix both existed after the first rejection.
**Evidence:** "One rule per upload cycle, twice" (Ash Higgins, 2026-09-09).
**Size:** trivial. **UNENFORCED CONVENTION.** Marked as prose deliberately: no
check can make somebody read a report. It is one of only two prose items here,
and it is the kind that genuinely cannot be executable.

### L3. The compound-command shell hazard
**Target:** environment note in `CLAUDE.md`.
**Change:** record that a process-kill inside a compound command terminates the
session shell, and cost two files mid-write.
**Rationale:** silent and destructive, and nothing warns about it.
**Evidence:** "A shell habit destroyed work twice" (Ash Higgins, 2026-09-09).
**Size:** trivial. **UNENFORCED CONVENTION**, and **UPSTREAM**: this is a
property of the tool, not of PSIRENS, so it belongs in the Foundations guidance
where every project inherits it. Recording it here helps one repository.

## Declined, with the measurement that declines them

The skill requires these to be named rather than silently dropped, because a
plan that ingests everything makes the baseline worse.

### D1. Chasing page coverage up to the 80 per cent floor
**Declined.** The temptation follows directly from G4, and it is a trap. Both
defects this window were runtime and layout behaviour: a missing response status
check, and an intrinsic-size ratchet. Neither would be caught by unit coverage
of the file, and reaching the floor would mean adding a JavaScript test
toolchain to satisfy a metric. **G3 closes that class outright; coverage of the
file closes nothing.** This is the same shape as the skill's own cited trap of a
broad analyser profile reporting 562 findings where 67 mattered.

### D2. A new `CLAUDE.md` rule for each lesson in the report
**Declined**, by the residuals ratio measured above and by direct evidence. The
resize lesson is already in `CLAUDE.md`, written in full, with the cause and the
correct assertion spelled out, and 1.6.2 still reached Active. Nine unscheduled
residuals are already carried. Adding nine more sentences would raise the prose
count and close nothing.

### D3. Running the full loop as a pre-commit hook
**Declined.** The loop builds a virtual environment and installs from scratch on
every run, which is right for a final gate and wrong for an inner loop. A gate
that is slow at commit time gets bypassed, and a bypassed gate is worse than an
absent one because it still reads as protection. The correct placement is before
packaging, which is where it already sits.

### D4. Rewriting the co-planar threshold arcs
**Declined for this plan**, though the finding is real: on logarithmic axes they
are screen-space ellipses rather than a locus. It is a design decision for the
owner and not a lesson about how the project is built, so it stays an open item
rather than entering the learning loop.

## Suggested order

Take G1, G2, T1 and G4 as one batch. Together they are under a day, they convert
four unproved guards into proved ones, and they make a skipped check impossible
to mistake for a passed one. That moves the audited count from one guard in nine
to five or six in nine.

Then G3, which is the half day that closes the class that actually shipped a
defect.

T3 and L1 need you rather than an engineer. L2 and L3 are one line each and can
ride along with any batch.

## Credits

● **Ash Higgins**, 2026-09-09, `RETROSPECTIVE.md`, PSIRENS 1.5.0 to 1.6.3 and
  the derived LEO starter kit. Every item above traces to that report; the
  residuals ratio and the browser feasibility check are the only figures
  measured fresh for this plan.

## Loop record

**Applied, 2026-09-09, one batched change, approved by Ash Higgins:**
G1, G2, T1, G4, and G3 on 11 September. Each was demonstrated to fail first:
loosening the AST caps makes the red case fail; an empty glob fails rather than
passing by examining nothing; a typo in an SPA pattern fails on the red case;
and with node absent the loop stops instead of warning and going green. The
audited count moves from one guard in nine with a committed red case to six.

**G3 applied 11 September 2026.** `tools/layout_probe.js` and
`tools/layout_check.sh` serve the app offline, drive Chromium, and assert the
backing store tracks the box, the body never overflows the modal, and shrinking
shrinks. Proven by reverting the two CSS rules to their byte-identical 1.6.2
form: 437px body in a 420px modal, build red. Worth recording that the FIRST
proof attempt used a reconstruction of the 1.6.2 CSS and the probe passed
against it. A reconstructed bug is not the bug.

The audited count is now eight guards in nine with a committed red case.

**Still open:** T2 (declare the loop's blind spots in its summary line) was
partly delivered by G4 and the closing lines and is not claimed as done. T3 (the
real scanner) and L1 (the kit under version control) need the owner. L2 and L3
are unapplied prose.

**Declined:** D1, D2, D3, D4, with the measurements above, so a later synthesis
does not re-propose them.
