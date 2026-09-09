# Gate cases: code that MUST be rejected

Every file here is deliberately bad. The loop runs each gate against these
first and fails the build if the gate does NOT flag them, then runs the same
gate against the real source and fails if it does.

This exists because of a measured problem, not a hypothetical one. Two checks
in this project once reported success while examining nothing: a linter run
from the wrong working directory that silently ignored its input, and a resize
assertion that was true by construction. Both passed while real bugs were live.
A gate nobody has watched fail is not a gate.

Nothing here is imported, collected by pytest, analysed by SonarQube
(`sonar.sources=src`), or shipped: the packaging allowlist does not name this
directory.

If you change a gate, change its case here too, and watch the case fail.
