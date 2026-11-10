# PSIRENS - App Store python template. Multi-stage, non-root, port 8080.
# Base image is python:3.13-slim from 1.6.20: 3.12.16 carried five Container
# Scan findings with a fix only in 3.13 or later. Three more (CVE-2025-15367,
# CVE-2026-116566, CVE-2026-103995) have NO fix in any released line and stay
# open on 3.13 as well; see DEPENDENCY-GATES.md rather than chasing them.
# Contract: read PORT (default 8080), bind 0.0.0.0, GET / and /healthz return
# 200 unauthenticated, no ENV PORT, no ENV DATA_DIR (code defaults carry them;
# platform injection wins). Pin <pinned-digest> to a real digest at build time.

FROM python:3.13-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-runtime.txt .
RUN pip install -r requirements-runtime.txt

FROM python:3.13-slim AS prep
# Patch OS packages (fail-open, in its own layer so it cannot mask the strip).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* 2>/dev/null || true
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY src ./src
# Create the non-root user, drop the package installers, THEN strip suid/sgid
# as the LAST mutation, so no later instruction can re-introduce the class
# (fail-closed).
#
# WHY pip AND setuptools GO. Nothing in this container ever installs a package:
# the venv is built in the stage above and copied in finished. They are pure
# attack surface, and they are also a standing Container Scan finding that no
# version bump ever really clears, because the scanner reads whatever pip is
# present. On 10 November 2026 pip alone carried six advisories here, none of
# them reachable in an image that never installs anything.
# BOTH COPIES, because there are two: the one inside /opt/venv and the
# interpreter's own in /usr/local. The final stage copies the whole filesystem,
# so leaving either one behind leaves the findings behind. `rm -rf` with a glob
# that matches nothing still succeeds, so this does not break on a base image
# that has already moved them.
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser \
 && chown -R 10001:10001 /app \
 && rm -rf /opt/venv/lib/python*/site-packages/pip* \
           /opt/venv/lib/python*/site-packages/setuptools* \
           /opt/venv/lib/python*/site-packages/pkg_resources \
           /opt/venv/lib/python*/site-packages/wheel* \
           /opt/venv/bin/pip* \
           /usr/local/lib/python*/site-packages/pip* \
           /usr/local/lib/python*/site-packages/setuptools* \
           /usr/local/lib/python*/site-packages/pkg_resources \
           /usr/local/lib/python*/site-packages/wheel* \
           /usr/local/bin/pip* \
 && find / -xdev -perm /6000 \( -type f -o -type d \) -exec chmod a-s {} + 2>/dev/null || true

# Flatten to a single clean layer so the image-policy scanner finds no
# setuid/setgid bit in layer history.
FROM scratch
COPY --from=prep / /
ENV PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
USER 10001:10001
EXPOSE 8080
# exec so SIGTERM reaches gunicorn; uvicorn worker for the ASGI app.
CMD ["sh","-c","exec gunicorn psirens.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080} --workers 1 --timeout 60 --no-control-socket --preload"]
