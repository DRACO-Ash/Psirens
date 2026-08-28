# PSIRENS App Store package: file listing

Shared in response to a request from another App Store application team for a
listing of our package root, so they can diff their failing archive against a
structure that clears the platform's structural gates.

**Filenames, sizes and timestamps only. No file contents.** Nothing here is
confidential: PSIRENS is Not Classified, and this listing contains no
credentials, no endpoint configuration and no data.

Scope note: this covers **PSIRENS only**. I have no visibility of Enlightenment
and cannot speak for it.

## Provenance of this listing

● Artefact: `psirens-appstore-1.5.3.zip`
● SHA-256: `869d15c5e9de099b17957de959a50907ba13627a56a63683c516cfce4f56c6ad`
● Size: 1,584,298 bytes, 39 entries
● Packaged version: `1.5.3` (from `pyproject.toml` inside the archive)
● Exported: 27 August 2026

**Be precise about what "passing" means here, because it matters for the diff.**
The 1.5.3 archive has not itself been uploaded yet. The file **set** below is
byte-for-byte identical in shape to 1.5.0 and 1.5.2, both of which were
uploaded. On those runs, every structural gate passed:

| Stage | Result on our uploads |
|---|---|
| Secret Detection | Passed |
| Dependencies | Passed |
| SAST Scan | Passed |
| Dependency Scanning | Passed |
| Test | Passed |
| Code Quality | **Failed** (SonarQube, on code content, not packaging) |
| Dockerfile Lint | Passed |
| Container Build | Passed |
| Container Scan | Passed |

So this layout is proven against everything that inspects the **archive**. Our
failures were at Code Quality, which analyses source content and has nothing to
do with package structure. If your failure is a packaging or build failure, this
listing is directly comparable. If yours is also Code Quality, the structure is
not your problem and this file will not help.

## 1. Raw `unzip -l` output

```
Archive:  psirens-appstore-1.5.3.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
     1695  2026-08-02 06:46   Dockerfile
      236  2026-08-02 06:50   .dockerignore
      801  2026-07-28 11:52   requirements.txt
      594  2026-07-28 11:52   requirements-runtime.txt
      424  2026-08-26 13:03   pyproject.toml
      598  2026-07-29 18:27   sonar-project.properties
    10215  2026-07-27 21:15   README.md
        0  2026-07-20 14:16   src/
        0  2026-08-24 12:14   src/psirens/
     9031  2026-08-07 11:32   src/psirens/refresh.py
    17993  2026-08-26 13:03   src/psirens/main.py
     5240  2026-08-23 20:22   src/psirens/config.py
        0  2026-07-20 13:42   src/psirens/__init__.py
        0  2026-08-06 14:11   src/psirens/static/
    33062  2026-08-05 12:20   src/psirens/static/hrr-geo.json
    42745  2026-08-26 13:02   src/psirens/static/index.html
  1018215  2026-07-20 14:29   src/psirens/static/psirens-banner.png
    58077  2026-07-20 14:29   src/psirens/static/icon-192.png
    51568  2026-07-20 14:29   src/psirens/static/apple-touch-icon.png
     2439  2026-07-20 14:29   src/psirens/static/favicon-32.png
      392  2026-07-20 14:29   src/psirens/static/manifest.webmanifest
   365088  2026-07-20 14:29   src/psirens/static/icon-512.png
    10453  2026-08-25 15:29   src/psirens/conjunction.py
    13569  2026-08-25 15:28   src/psirens/tle.py
     4933  2026-08-11 06:48   src/psirens/astro.py
    18522  2026-08-24 12:12   src/psirens/sources.py
     2013  2026-07-20 13:41   src/psirens/security.py
     2474  2026-08-06 19:51   src/psirens/models.py
     9228  2026-08-23 20:21   src/psirens/store.py
    10174  2026-08-06 11:52   src/psirens/hrr.py
        0  2026-08-23 20:25   tests/
     5309  2026-08-25 15:29   tests/test_conjunction.py
     6668  2026-08-23 20:26   tests/test_store.py
     1448  2026-08-11 06:49   tests/test_drift.py
     8945  2026-08-23 20:26   tests/test_sources.py
    14072  2026-08-23 20:27   tests/test_api.py
     9115  2026-08-06 11:37   tests/test_hrr.py
    11631  2026-08-25 15:29   tests/test_tle.py
     2716  2026-07-20 14:16   tests/test_astro.py
---------                     -------
  1749683                     39 files
```

## 2. Bare sorted paths, for a direct `diff`

Generate the same form on your side with `unzip -Z1 <your.zip> | sort` and diff
against this block.

```
.dockerignore
Dockerfile
README.md
pyproject.toml
requirements-runtime.txt
requirements.txt
sonar-project.properties
src/
src/psirens/
src/psirens/__init__.py
src/psirens/astro.py
src/psirens/config.py
src/psirens/conjunction.py
src/psirens/hrr.py
src/psirens/main.py
src/psirens/models.py
src/psirens/refresh.py
src/psirens/security.py
src/psirens/sources.py
src/psirens/static/
src/psirens/static/apple-touch-icon.png
src/psirens/static/favicon-32.png
src/psirens/static/hrr-geo.json
src/psirens/static/icon-192.png
src/psirens/static/icon-512.png
src/psirens/static/index.html
src/psirens/static/manifest.webmanifest
src/psirens/static/psirens-banner.png
src/psirens/store.py
src/psirens/tle.py
tests/
tests/test_api.py
tests/test_astro.py
tests/test_conjunction.py
tests/test_drift.py
tests/test_hrr.py
tests/test_sources.py
tests/test_store.py
tests/test_tle.py
```

## 3. The four structural properties a diff will surface

A bare listing does not explain itself, so these are the things we would expect
a diff to raise. All four are our own hard-won learning from repeated upload
cycles, not quotations from platform documentation. Treat them as field notes
from one app, not as the specification.

### 3.1 The archive is FLAT at the root, with no wrapping folder

`Dockerfile`, `requirements.txt`, `pyproject.toml`, `src` and `tests` sit at the
archive root. There is no `psirens/` or `psirens-1.5.3/` directory containing
them.

This is the single most common way to get this wrong, because almost every
archive tool wraps by default: zipping a project folder from the file manager
produces `myapp/Dockerfile`, not `Dockerfile`. If your listing shows one extra
leading path component on every line, that alone is worth testing first.

### 3.2 Two requirements files, and the split is load bearing

`requirements.txt` and `requirements-runtime.txt` are both present and they are
different files.

The platform's test stage runs `pip install -r requirements.txt` and then
pytest, so every test tool must be in that file or the stage cannot collect. The
Dockerfile installs `requirements-runtime.txt` instead, so the image stays free
of test tooling. For us the delta between the two is exactly `pytest` and
`pytest-cov`.

If you have one requirements file, or if your test tooling lives only in a
`requirements-dev.txt` that the platform never installs, the test stage will
fail on a missing pytest even though everything works locally.

### 3.3 `tests/` is inside the archive

The archive ships the test suite, because the platform runs it. `.dockerignore`
then excludes `tests/` from the image build, so the tests reach the test stage
but not the container.

### 3.4 `sonar-project.properties` is present

Included at the root so the Code Quality stage analyses the intended source set.

## 4. What is deliberately absent, and why

Absence is as diagnostic as presence in a diff, so these omissions are
intentional rather than oversights:

● `CLAUDE.md`, `CONTEXT-001.md`, `READINESS.md`, `DEPENDENCY-GATES.md` and this
  file: internal engineering documents, never shipped.
● `tools/`: a standalone diagnostic script, not part of the application.
● `simulate-pipeline.sh`, `package-appstore.sh`: build tooling.
● `.venv/`, `__pycache__/`, `.git/`, `coverage.xml`, `.env`: never packaged.
● `docker-compose.yml`: local development only.
● No `.gitlab-ci.yml`. The platform generates its own pipeline. We do not ship
  one, and our understanding is that you should not either.

The mechanism matters more than the list. Our packaging script uses an
**allowlist**, naming exactly what goes in, rather than zipping everything and
excluding. That is why our Secret Detection stage has never fired: a stray
`.env` or credentials file cannot be included by accident, because it was never
named. If your packaging is "zip the directory, minus some exclusions", that is
worth changing regardless of your current failure.

## 5. How this archive is produced

For reference, the whole of our packaging step:

```sh
zip -r "psirens-appstore-${VERSION}.zip" \
  Dockerfile .dockerignore requirements.txt requirements-runtime.txt \
  pyproject.toml sonar-project.properties README.md \
  src tests \
  -x '*/__pycache__/*' '*.pyc'
```

Run from the repository root, which is what produces the flat layout in
section 3.1.

## 6. Regenerating this listing

It will drift as the app changes. To refresh:

```sh
unzip -l  psirens-appstore-<version>.zip          # section 1
unzip -Z1 psirens-appstore-<version>.zip | sort   # section 2
sha256sum psirens-appstore-<version>.zip
```

Happy to answer follow-up questions on any of the above, or to re-export
against a newer build.
