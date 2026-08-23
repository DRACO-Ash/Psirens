# PSIRENS - App Store readiness report

**Band: Not yet** (fail-closed). Weighted score of verifiable dimensions is high
and everything runnable here is green, but two blocker-class items cannot be
called passed yet: the last platform pipeline run is red (fix ready, not yet
re-run), and the runtime container image has never been built or scanned.

This is a pre-flight estimate, not the binding App Store decision. The
platform's quality gate, container scan, cATO score, and human review remain the
real gate, and `deploy-gate` is the last internal word.

Archetype: server / container (Python, FastAPI). Date: 27 July 2026.

## Blockers (these cap the band at "Not yet")

1. **Last pipeline run is red.** The most recent App Store test stage failed with
   `pytest: command not found` (exit 127), because the platform installs only
   `requirements.txt` and the test tools were in `requirements-dev.txt`. Fixed in
   this working tree (see below), but not yet pushed to the pipeline's repo or
   re-run. Scored from the actual last run, this is a blocker until a green run
   exists. Owner: `appstore-gate-compliance`, `ci-cd`.

2. **Runtime image build and scan are unverified.** There is no Docker in this
   environment, so the multi-stage build, the non-root user, the setuid/setgid
   strip, the OS patch, and the single-layer flatten are structurally correct but
   unproven. Per fail-closed scoring an unverifiable control is treated as not
   passed. The platform's build and container-scan stages are the first real
   exercise of the Dockerfile. Owner: `security-hardening`, `deploy-recipes`,
   `app-store-deployment`, `appstore-gate-compliance`.

3. **Binding gates not run here.** `engineering-reviewer`, `security-reviewer`,
   and `deploy-gate` are not available in this environment, so deep mode was not
   executed. Run them in the harness before submit; a gate FAIL is a blocker.

## Fix applied in this session (for blocker 1)

- `requirements.txt` is now the full set (runtime plus `pytest==9.0.3` and
  `pytest-cov==7.0.0`), so the platform test stage has pytest.
- `requirements-runtime.txt` is new and runtime-only; the Dockerfile installs it
  so the image stays lean and free of test tooling.
- `pytest` moved to 9.0.3 to clear CVE PYSEC-2026-1845 (present in 8.4.2), so
  neither dependency set has a known vulnerability.
- `simulate-pipeline.sh` now mirrors the platform exactly (installs only
  `requirements.txt`, runs the platform's exact pytest command). This is the
  check that would have caught the failure before upload.
- `requirements-dev.txt` removed as redundant.

To clear blocker 1: commit these changes to the branch the pipeline builds from
and re-run. The stage will then install pytest and pass.

## Per-dimension scoring

| Dimension | Weight | Result |
|---|---|---|
| Verification loop green (41 passed, exit 0) | blocker | PASS |
| Coverage at least 80% (92.8%) | heavy | PASS |
| No secret in source; `.env.example` redacted | blocker | PASS (run a git-history scan on the repo to confirm) |
| Server contract: PORT default 8080, binds 0.0.0.0, `/` and `/healthz` 200, non-root uid 10001, no ENV PORT | blocker | PASS |
| Container package flat: Dockerfile and src at root | blocker | PASS |
| Runtime image hardened AND flattened (non-root, no suid, OS patched, single layer) | blocker | UNVERIFIED (no build/scan here) |
| Coverage report at the SonarQube path (`coverage.xml`, scoped by `sonar-project.properties`) | heavy | PASS |
| Reproducible install from pinned deps; no High/Critical CVE | heavy | PASS (pinned; CVE scan clean. Hash-pinning not used) |
| Container quality-gated: testable source tree, pipeline simulation green, emits coverage | blocker (pre-submit) | PASS |
| Per-commit static analysis at zero violations | heavy | LOCAL PASS (ruff clean); SonarQube is server-side |
| `test` invocation tolerates the platform command and emits coverage | blocker | PASS (was the failure; now fixed and verified) |
| Negative assertions classified per environment; scanners fail-closed | blocker | PASS (tests are deterministic and offline-safe) |
| CI pipeline mirrors the loop, least privilege, latest run green | blocker if red | NOT YET (no repo CI; platform run last red) |
| Version stamp, generic client errors, no secret in logs | medium | PASS |
| Accessibility to WCAG AA; tokens, one accent | medium (UI) | PARTIAL (focus rings, ARIA, reduced-motion present; not formally audited) |
| Surgical structure, no dead code, documented architecture | medium | PASS |
| House voice in user-facing copy | light | PASS |

## What would raise the band

- **To "Likely after fixes":** push the test-stage fix to the pipeline's branch
  and get a green test stage.
- **To "Ready":** a full green platform pipeline (test, build, container scan,
  SonarQube quality gate) on the head commit, plus PASS from the binding
  `engineering-reviewer`, `security-reviewer`, and `deploy-gate`.

## Skills to download from Launchpad

Most owning skills are already in your set (`security-hardening`,
`deploy-recipes`, `app-store-deployment`, `appstore-gate-compliance`,
`testing-standards`). The gap not covered by a skill you already hold:

- **`ci-cd`** - there is no repository CI mirroring the local loop, and a CI that
  runs the same platform-exact command per commit is what stops the test-stage
  class of failure recurring.

Optional, only if you act on the medium findings:

- **`accessibility`** - to take the dashboard from partial to an audited WCAG AA.

## Note on persistence and UDL (context, not scored)

- Storage is ephemeral `/tmp` for v1 by choice; trails reset on restart.
- UDL wire settings are on defaults and TBC against the tenant; the app runs
  healthy but shows an empty plot if a setting or credential is wrong, and the
  pod log names which one.
- Confirm outbound egress to the UDL host from the deployed pod.

Report is reproducible against the current source. Generated by the
app-store-readiness check.
