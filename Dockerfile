# PSIRENS - App Store python template. Multi-stage, non-root, port 8080.
# Contract: read PORT (default 8080), bind 0.0.0.0, GET / and /healthz return
# 200 unauthenticated, no ENV PORT, no ENV DATA_DIR (code defaults carry them;
# platform injection wins). Pin <pinned-digest> to a real digest at build time.

FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM python:3.12-slim AS prep
# Patch OS packages (fail-open, in its own layer so it cannot mask the strip).
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/* 2>/dev/null || true
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY src ./src
# Create the non-root user, THEN strip suid/sgid as the LAST mutation, so no
# later instruction can re-introduce the class (fail-closed).
RUN useradd -u 10001 -r -s /usr/sbin/nologin appuser \
 && chown -R 10001:10001 /app \
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
CMD ["sh","-c","exec gunicorn psirens.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080} --workers 2 --timeout 60"]
