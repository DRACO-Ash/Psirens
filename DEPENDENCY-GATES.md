# DEPENDENCY-GATES: the App Store supply-chain gates, as far as PSIRENS has proven them

**Owner:** Ash Higgins, Technical Director, Bluestaq Ltd
**Classification:** Not Classified
**Status as at 26 August 2026:** every dependency-related gate has PASSED on every
PSIRENS upload. Nothing in this file was learned from a failure.

## Read this warning first

This is **not** the counterpart to the SonarQube section in `CLAUDE.md`. That
section is a root-caused failure catalogue: each line cost a real upload cycle
and is therefore fact. This file is the opposite situation. `Dependencies`,
`Dependency Scanning`, `Secret Detection`, `SAST Scan` and `Container Scan` have
never failed for this app, the pipeline UI returned "Couldn't load the job log"
when the run was inspected, and no scanner output has ever been read.

So most of what follows is **inference from a passing signal**, which is the
weakest evidence there is: a gate that has never fired tells you almost nothing
about where its threshold sits. Every claim below is tagged.

● **FACT**: observed directly in this repository or in the pipeline UI.
● **INFERENCE**: reasoned from those observations, not confirmed.
● **TBC**: genuinely unknown. Do not guess these; get them from a real run.

The single most useful thing anyone can do with this file is replace the TBC
items with facts the first time a dependency gate actually fails, or the first
time someone reads a job log.

## 1. The pipeline, and where these gates sit

**FACT.** Nine stages, in this order, from the 1.5.0 and 1.5.2 upload screens:

| # | Stage | Concern |
|---|-------|---------|
| 1 | Secret Detection | credentials committed to the archive |
| 2 | Dependencies | the declared dependency set |
| 3 | SAST Scan | static analysis of our own code |
| 4 | Dependency Scanning | known vulnerabilities in dependencies |
| 5 | Test | `pip install -r requirements.txt` then pytest |
| 6 | Code Quality | the SonarQube quality gate |
| 7 | Dockerfile Lint | Dockerfile hygiene |
| 8 | Container Build | the image builds |
| 9 | Container Scan | vulnerabilities in the built image |

**FACT, and useful.** A Code Quality failure at stage 6 did **not** stop stages
7, 8 and 9 running: all three still reported Passed on the failed 1.5.0 run,
and the summary read "8 of 9 stages passed". The pipeline is not fail-fast, so
a Code Quality failure still tells you the container built and scanned clean.

**INFERENCE.** Stage 2 (`Dependencies`) and stage 4 (`Dependency Scanning`) are
distinct concerns, most likely resolve-and-inventory versus vulnerability
matching. The names alone imply this; neither stage's output has been seen.

**TBC.** Which scanner backs stages 4 and 9, what severity threshold fails
them, whether transitive dependencies are in scope, and whether a waiver or
allowlist mechanism exists. All four matter the moment a CVE lands, and none of
them can be answered honestly today.

## 2. Why PSIRENS keeps passing (the parts that are real)

These are structural properties of the repository, so they are FACT, and they
are almost certainly why the gates are quiet. They are worth preserving
deliberately rather than by accident.

### 2.1 Two requirements files, and the split is load-bearing

**FACT.** The platform TEST stage runs `pip install -r requirements.txt`, so all
test tooling must live there. The Docker image installs
`requirements-runtime.txt` instead.

**FACT.** The delta between the two files is exactly two packages:

```
pytest==9.0.3
pytest-cov==7.0.0
```

**FACT.** `.dockerignore` also excludes `tests/`, so neither the test tooling
nor the test code reaches the image.

**INFERENCE, and the main point of this section.** This is why a CVE in pytest,
coverage or any test-only transitive dependency cannot fail Container Scan: it
is not in the image. It could still surface at stage 4 if that stage scans the
full declared set rather than the runtime set, which is TBC. Anyone tempted to
collapse the two files into one should understand they would be widening the
image's vulnerability surface to include the entire test toolchain.

### 2.2 Everything is exactly pinned

**FACT.** All 19 runtime dependencies use `==`. Zero use ranges or unpinned
names:

```
annotated-doc  annotated-types  anyio       certifi   click
fastapi        gunicorn         h11         httpcore  httpx
idna           packaging        pydantic    pydantic_core
sgp4           starlette        typing-inspection      typing_extensions
uvicorn
```

**INFERENCE.** Exact pinning makes a scan result reproducible between uploads:
the same archive scans the same way twice, and a new finding means the CVE
database moved, not that the resolver drifted. It also means a fix is a
deliberate edit, never a silent one.

**Consequence worth stating plainly.** Pinning does not make you safe, it makes
you *stationary*. The set above ages every day it is not touched. A quiet gate
today is not evidence of a healthy dependency set; it is evidence that nothing
in that set has been disclosed **yet**.

### 2.3 One deliberate runtime dependency

**FACT.** `sgp4==2.27` is the only dependency added for the application's own
purposes, and it is recorded in `CLAUDE.md` as a deliberate choice (the
validated Vallado propagator). Everything else in the list is FastAPI, Pydantic,
Starlette, uvicorn, gunicorn, httpx and their transitive closure.

**INFERENCE.** A small, boring, framework-shaped dependency set is the cheapest
supply-chain control available, and it is the second reason these gates are
quiet. The convention in `CLAUDE.md`, no new runtime dependency without a
recorded reason, is doing real work here and should be defended.

## 3. Standing risks in the current posture

None of these has failed a gate. All are observable in the repository today and
all are the kind of thing a supply-chain gate eventually finds.

### 3.1 The base image is pinned by tag, not by digest

**FACT.** The Dockerfile builds `FROM python:3.12-slim` in two stages, and its
own header comment says: `Pin <pinned-digest> to a real digest at build time`.
The digest was never substituted.

**INFERENCE.** `python:3.12-slim` is a moving tag. Two builds of the identical
archive can therefore produce different images with different OS package sets
and different Container Scan results. This is the largest reproducibility gap
in the build, it is already flagged in the file's own comment, and it is the
one item here I would fix first.

### 3.2 The final image contains a whole Debian filesystem

**FACT.** The last stage is `FROM scratch` followed by `COPY --from=prep / /`.

**INFERENCE.** The `scratch` base is a layer-flattening device, chosen so the
image-policy scanner finds no setuid or setgid bit in layer history. It is not a
minimal image: the entire Debian userland from `python:3.12-slim` is copied in.
Container Scan therefore sees every OS package, not just the Python ones. That
is the correct thing to expect from stage 9, and it is another reason the base
image digest matters more than the Python pins.

### 3.3 OS patching is fail-open

**FACT.** The patch step ends `... || true`:

```
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* 2>/dev/null || true
```

**INFERENCE.** This is deliberate, so a transient mirror failure cannot break
the build. The cost is that it can also silently no-op: a build where
`apt-get upgrade` failed outright looks identical to one where it succeeded, and
the resulting image simply carries unpatched packages into stage 9. If Container
Scan ever fails on an OS package, check this first, because a green build is not
evidence the patch ran.

### 3.4 No hash pinning and no SBOM

**FACT.** The requirements files carry no `--hash` entries, and the repository
produces no SBOM artefact.

**INFERENCE.** Version pinning without hashes protects against drift but not
against a compromised or re-uploaded artefact on the index. Whether the platform
generates its own SBOM at stage 2 or 4 is **TBC**; if it does, we do not need to,
and that is worth finding out before building one.

## 4. Pre-flight checklist

Everything here is cheap, local, and does not need the platform. It will not
prove the gates pass, because we do not know their thresholds, but it removes the
failure modes that are visible from here.

● Confirm both requirements files are exactly pinned and that the delta is only
  test tooling:
  `comm -23 <(grep -oE "^[A-Za-z0-9_.-]+==[0-9A-Za-z.]+" requirements.txt | sort) <(grep -oE "^[A-Za-z0-9_.-]+==[0-9A-Za-z.]+" requirements-runtime.txt | sort)`
● Confirm no new runtime dependency arrived without a recorded reason in
  `CLAUDE.md`.
● Confirm `.dockerignore` still excludes `tests/`, `.venv/` and `.git/`, so the
  build context stays lean.
● Confirm no credential or `.env` reached the archive. The packaging allowlist in
  `package-appstore.sh` is an allowlist, not a denylist, which is why stage 1 has
  never fired: only named files and `src`/`tests` are ever included. Preserve that
  shape; a switch to "everything except" would put stage 1 genuinely at risk.
● If a dependency was bumped, run the golden loop, because a version change can
  break tests long before it breaks a scanner.

## 5. What to do when one of these gates finally fails

Written now, while there is no pressure, because the SonarQube experience shows
the failure arrives mid-release.

1. **Read the job log before changing anything.** The one thing we have never
   done is read the actual scanner output. It names the package, the CVE and
   the severity. Do not infer any of those from the stage name.
2. **Establish which set is implicated:** runtime, test-only, or an OS package
   from the base image. The fix is completely different in each case. Runtime
   means bumping a pin in both requirements files; test-only means bumping it in
   `requirements.txt` alone; OS means the base image digest or the patch step.
3. **Bump, do not remove.** The dependency set is small and every entry is load
   bearing. Removing a transitive pin to silence a scanner will break the
   install.
4. **Re-run the golden loop before repackaging.** A dependency bump is a code
   change in effect; `simulate-pipeline.sh` installs from `requirements.txt` and
   will catch an incompatibility immediately.
5. **Record what the log actually said in this file**, converting a TBC into a
   FACT. That is the whole point of the document.

## 6. The honest summary

**FACT.** Five gates concerned with supply chain have passed on every upload,
including the two uploads that failed elsewhere.

**INFERENCE.** They pass because the dependency set is small, exactly pinned,
framework-shaped, and split so the runtime image carries no test tooling. Those
are real controls and they should be defended deliberately.

**The thing not to conclude.** None of this means the gates are weak, or that
the app is secure. It means these gates have not yet had anything to say to us.
The three items in Section 3, an unpinned base image digest, a fail-open patch
step, and a full Debian userland in the final image, are the places where the
first real finding is most likely to appear, and none of them is visible from a
passing pipeline.

## Pointers

● `CLAUDE.md`: the App Store deploy contract and the SonarQube gate discipline.
● `CONTEXT-001.md` Section 7: the deploy contract and the local-proxy gap, in
  the LEARNED register.
● `Dockerfile`: the base image, the patch step and the flatten stage.
● `package-appstore.sh`: the packaging allowlist that keeps stage 1 quiet.
