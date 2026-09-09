# Teach Bob retrospective

- Developer: Ash Higgins
- Project: PSIRENS (and the derived LEOAPP starter kit)
- Date: 2026-09-09
- Scope: PSIRENS 1.5.0 to 1.6.3, plus the standalone LEO handover kit built from it
- Stack: Python 3.11/3.12, FastAPI, sgp4 2.27, single-file canvas SPA, no build step
- Archetype: container service on the Bluestaq App Store, plus one portable starter kit
- Duration: 12 commits over 12 days (2026-08-23 to 2026-09-03), then the kit
- Baseline: the repository's own `CLAUDE.md` operating contract, `CONTEXT-001.md`
  learned register, and `simulate-pipeline.sh`. No external skill bundle version.

Filed under the name recorded as owner in `CLAUDE.md` and matching the account
running the session. Correct the name and re-file if that is wrong.

## Summary

A recovery, three features and a fix, shipped through a platform pipeline that
rejects on a single rule. The window opened by restoring a build that a
migration had silently replaced with an older scaffold, then delivered native
element-set handling, a co-planar view, a naming fix and a layout fix, ending
with all ten pipeline stages green. The most important lesson is uncomfortable
and is the reason this report exists: **of the eight local guards this project
relies on, exactly one has a committed case proving it can go red.** That is not
a theoretical gap. Version 1.6.2 reached Active on the App Store carrying a live
layout defect, and the check written to catch it asserted something that could
never be false. The owner found the bug, not the build.

## What went well

● **Adding a gate per rule learned ended a three-cycle treadmill.** Two uploads
  in this window were rejected at Code Quality on one rule each: 1.5.0 on a
  parameter-count smell and 1.5.2 on a promise-rejection style rule. After a
  local check was added for each, 1.5.3 cleared Code Quality outright, the first
  build in the project's history to do so (evidence: commits `a506715`,
  `b0b2234`, and the pass recorded in `e07f738`).
● **Root-causing beat patching on the live 500.** The "service unavailable"
  error in the inspector was traced to two real defects, an unchecked
  `response.ok` in the SPA and an unguarded propagator build on the target
  object, both of which predated the feature work and were present in 1.4.14.
  The fix guards every path and returns JSON with a stated reason rather than a
  catch-all (evidence: `292aa77`, and `conjunction.py:228` where the guard now
  sits).
● **A divergent feature branch merged additively.** The co-planar merge touched
  25 files and deleted two lines in total, both of them version-string bumps
  (evidence: `git diff --shortstat 3ed6184 ee3942f` gives +2873/-88 across the
  whole window; the merge itself is `7ef6c4a`).
● **Measurement rather than inspection on the layout bug.** The modal resize
  defect was diagnosed by capturing live numbers from the running page (modal
  725 with body 682, then modal 800 with body 910, then modal 420 with body
  still 910) rather than by reading the stylesheet and reasoning. The ratchet
  was obvious in the numbers and invisible in the CSS.
● **An owner assertion was corrected with evidence rather than accepted.** Told
  the data source was reachable, the work proceeded offline instead after the
  proxy returned a refusal on connect. Accepting it would have produced feature
  work built on an untestable assumption.

## What did not

● **1.6.2 shipped a live defect to Active, and the verification could not have
  caught it.** The check asserted that a canvas element's CSS box changed after
  a resize. That is true by construction, because the box is the thing being
  resized. It passed while the real bug, an intrinsic-size ratchet, was live
  underneath. The owner reported it from screenshots.
● **A lint step reported success while linting nothing at all.** The linter
  resolves its base path from the working directory and silently ignores a file
  outside it, so the step exited zero having examined no code. This is the same
  failure mode as the point above, five days apart, in the same repository.
● **The window opened with half a day of recovery, not feature work.** The
  repository held a v1.0.x scaffold rather than the current build, and that
  scaffold's own verification loop failed on a dependency resolution conflict.
  Both had to be fixed before anything could start (evidence: `3ed6184`,
  "Restore PSIRENS 1.4.14 (migration landed a v1.0.x snapshot)").
● **One rule per upload cycle, twice.** Because the gates are not fail-fast, one
  upload reports every gate at once, yet the fix cycle still ran one rule at a
  time. The information to fix both at once existed after the first rejection
  and was not used.
● **A grep-based gate matched its own explanatory comment.** The check for a
  forbidden JavaScript construct fired on the comment that explained the check,
  producing a false failure until the comment was reworded.
● **A naming fix shipped a shadowing bug in its first draft.** The initial
  normaliser defaulted an unnamed catalogue entry to its own identifier, which
  would have masked a genuine name from the authoritative source. A test caught
  it before commit.
● **A shell habit destroyed work twice.** Running a process-kill inside a
  compound command terminates the shell itself, and on two occasions it killed a
  heredoc mid-write, losing the file being created.

## Improvements

● **Commit a red case for every guard, or stop calling them guards.** The
  parameter-count and complexity gates were each proved to fire, once, with a
  throwaway probe that was never committed. Nothing in the tree proves they
  still work. Add a fixture per gate that the gate must reject.
● **Commit the browser check.** The resize lesson currently lives as prose in
  `CLAUDE.md`. Prose does not fail a build. The check that caught the 1.6.3 bug
  exists nowhere in the repository, so the next person to touch that stylesheet
  has exactly the protection that let 1.6.2 through.
● **Run the real scanner against the tenant host before uploading.** This has
  been an open item since before this window and is still open. Every local
  check is a proxy for it, and one proxy was measured to be incomplete.
● **Put the handover kit under version control.** It exists only as a zip and a
  checksum. It has 150 tests and a green pipeline, and no history, no issue
  trail and no way to receive a fix.
● **Treat a rejected upload as a full report, not a single finding.** Read every
  stage before starting the fix.

## Optimisations

● **Three upload cycles could have been one.** The rules that fired were all
  knowable from a single run of the real scanner. The cost of not having it was
  paid three times.
● **The live-500 diagnosis started at the expensive end.** A store of 594
  objects and 28 MB was built to test a timeout hypothesis, which it correctly
  disproved (1.0 s cold, 0.8 s warm). Reading the SPA's fetch handler first
  would have found the missing status check in about a minute, and the error
  text was already the SPA's catch-all string rather than any timeout message.
● **Full pipeline runs were used where a targeted test run would have done.**
  The full loop builds a virtual environment and installs from scratch every
  time; it is the right final gate and the wrong inner loop.
● **The kit's demo data was rebuilt once after the semantics were wrong.** The
  synthetic pair was quoted at each track's own head rather than at a shared
  reference epoch, which made the objects merely look coplanar instead of being
  coplanar. The tests caught it, but stating the invariant before writing the
  generator would have avoided the rework.

## Waste

● **The 594-object store.** Genuinely conclusive, and unnecessary. The earlier
  signal was in the error string on the screenshot: it was the interface's
  catch-all, which meant a non-JSON response, which points at the response
  handling and not at the clock.
● **A supply-chain document written from failures only, and wrong as a result.**
  It recorded the pipeline as nine stages. It is ten. A run that fails a gate
  never reaches deployment, so a document derived exclusively from failed runs
  cannot see the stage that only appears on success. Corrected later
  (`d110a8f`, then `e07f738`), after being shared externally.
● **Two heredocs written twice** because of the shell habit above.

## Missed detail

● **A defect reached a deployed, Active build**, and was found by the owner
  rather than by any check.
● **The threshold arcs on the co-planar chart are decorative, not a locus.**
  With logarithmic axes they are drawn as screen-space ellipses whose intercepts
  happen to sit at the threshold values. Raised twice, not fixed, and still
  reads to an operator as a meaningful boundary.
● **The project README predates several shipped features** including the
  bearing needles, the simulation view, the range pull, conjunctions and deep
  zoom. Recorded as an open item and carried across the whole window untouched.
● **The native element-set work has never been checked against live data.** A
  standalone probe was written for exactly this and has only ever been run in
  self-test mode. Four assumptions remain unverified in production.
● **The copy-out gate refuses on any caveat, not only the proprietary one.**
  Fail-closed is the right default, but nobody has confirmed whether the
  broader class should be copyable, so an operator may be denied a copy control
  they are entitled to.

## Guards seen to fail

● **Linter canary (a deliberate violation the plugin must flag): proved.** The
  build fails if the canary is not reported, and the canary is generated inside
  the tracked `simulate-pipeline.sh`, so the case travels with the repository.
  This is the only guard in the project that proves itself on every run.
● **Cognitive complexity cap: not proved.** Demonstrated once against a
  throwaway function that was never committed. No case in the tree makes it go
  red. It did catch two real violations in the derived kit, which is evidence it
  works, but that evidence is a session transcript and not a test.
● **Parameter-count cap: not proved.** Same position, demonstrated once with an
  uncommitted probe.
● **The four source-text checks on the served page: not proved.** No committed
  case makes any of the four fire.
● **Coverage floor at 80 per cent: not proved**, and weaker than it looks.
  Coverage is 93.72 per cent across 150 tests, and the served page, which is the
  surface that produced both of this window's shipped defects, is excluded from
  the coverage metric entirely. The number is high and is reassuring about the
  wrong thing.
● **Layout and resize behaviour: no guard at all.** The check that found the
  1.6.3 bug was never committed. This is the gap that most deserves closing.
● **In the derived kit, the refusal behaviour is proved.** Committed tests
  assert that both unimplemented decision points raise rather than return, that
  the neighbourhood payload refuses rather than returning an empty list, and
  that the epoch-mismatch error is real. That last one is a committed
  counter-example, so the passing assertion beside it cannot be vacuous. The
  kit's own gates, inherited from this project, are in the same unproved state
  as the originals.

## Where the tooling or the standard cost us

● **The local lint proxy is a false friend, and this was measured, not
  assumed.** The rule that failed the 1.5.2 upload is not carried by the plugin
  at all, even with all 279 of its rules enabled. Running it would not have
  caught the failure. A proxy trusted as equivalent to the real thing is worse
  than no proxy, because it converts an unknown into a false assurance. It is
  still in the loop, now with the canary and a text search beside it, and the
  document says plainly what it cannot see.
● **A skipped step still exits zero.** When the runtime for the page lint is
  absent, the step prints a warning and the build goes green. The warning was
  made loud during this window, which is an improvement, but a machine that
  cannot run a check should not be able to report a pass.
● **The coverage floor measures the wrong surface**, as above. A floor that
  excludes the riskiest file is a floor for the other files.
● **A document tagged by evidence strength still reads as fact.** The
  supply-chain notes tag every claim, which is the right instinct, but the
  nine-versus-ten error survived a review and an external share because the tags
  are easy to skim past. Structure the weak claims as questions rather than as
  tagged statements.
● **The shell environment: a process-kill inside a compound command terminates
  the session's own shell.** Cost two lost files. The workaround is to issue it
  as its own command, which is now habit, but the failure is silent and
  destructive and nothing warns about it.

## Top three to carry forward

1. **Commit a failing case for every guard, or delete the guard.** Seven of
   eight are currently unproved, and the one that is proved is the only one that
   has never been in question. A guard nobody has watched fail is a comment.
2. **A verification that cannot fail is worse than none.** It happened twice in
   twelve days, in the same repository, and one of the two let a defect reach a
   deployed build. Before trusting a new check, break the thing it protects and
   watch it go red.
3. **Buy the real signal rather than approximating it.** Three upload cycles,
   two rejections and one measured-incomplete proxy all trace to not having the
   authoritative scanner in the loop. Every local gate exists because that one
   thing is missing.
