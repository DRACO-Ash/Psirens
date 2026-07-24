"""PSIRENS server: the createApp factory and the ASGI `app`.

Runtime contract (App Store): reads PORT (default 8080), binds 0.0.0.0 via the
gunicorn CMD, returns 200 unauthenticated at `/` and `/healthz`, runs non-root.
The operator Environment Variables tab must stay EMPTY for a code-defaults run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response

from .astro import drift_deg_per_day
from .config import Config, load_config
from .models import VIEW_MODES, DataMode, ManualElsetIn
from .refresh import Refresher
from .security import RateLimiter, SingleFlight, token_ok
from .sources import DemoElsetSource, ManualElsetSource, UDLElsetSource
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
_log = logging.getLogger("psirens")

_STATIC = os.path.join(os.path.dirname(__file__), "static")

# Classification ranking so the banner shows the most restrictive marking present.
_RANK = {"U": 0, "UNCLASSIFIED": 0, "CUI": 1, "C": 2, "S": 3, "TS": 4}


def _banner(markings: list[str], default: str = "UNCLASSIFIED") -> str:
    best, best_rank = default, -1
    for m in markings:
        r = _RANK.get(m.upper().split("//")[0], 0)
        if r > best_rank:
            best_rank, best = r, m
    # Show a readable word for the unclassified case rather than a bare "U".
    return "UNCLASSIFIED" if best_rank <= 0 else best


def _tracks_payload(cfg: Config, store: Store, view: str,
                    modes: set[DataMode]) -> dict:
    data = store.load()
    tracks, markings = [], []
    for oid, rec in data.get("objects", {}).items():
        try:
            mode = DataMode(rec.get("data_mode", "REAL"))
        except ValueError:
            mode = DataMode.REAL
        if mode not in modes:
            continue
        samples = rec.get("samples", [])
        if not samples:
            continue
        pairs = [
            (datetime.fromisoformat(s["epoch"]), s["sub_lon_deg"]) for s in samples
        ]
        markings.append(rec.get("classification_marking", "U"))
        tracks.append({
            "object_id": oid,
            "name": rec.get("name", oid),
            "data_mode": mode.value,
            "classification_marking": rec.get("classification_marking", "U"),
            "source": rec.get("source", ""),
            "origin": rec.get("origin", ""),
            "target": rec.get("target"),
            "samples": samples,
            "drift_deg_per_day": drift_deg_per_day(pairs),
        })
    return {
        "view": view,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification_banner": _banner(markings),
        "bounds": {
            "lon_min": cfg.lon_min, "lon_max": cfg.lon_max,
            "inc_min": cfg.inc_min, "inc_max": cfg.inc_max,
        },
        "count": len(tracks),
        "tracks": tracks,
    }


def create_app(cfg: Config | None = None,
               sources: list | None = None,
               http_client: httpx.Client | None = None) -> FastAPI:
    cfg = cfg or load_config()

    # Fail closed in production: a token with a wildcard origin refuses to start.
    if cfg.team_token and cfg.allowed_origin in ("", "*"):
        raise RuntimeError(
            "Refusing to start: TEAM_TOKEN is set but ALLOWED_ORIGIN is "
            "unset or '*'. Set ALLOWED_ORIGIN to the app's real origin."
        )

    store = Store(cfg.storage_dir())
    manual = ManualElsetSource(cfg.storage_dir())
    if sources is None:
        sources = []
        if cfg.udl_enabled:
            sources.append(UDLElsetSource(cfg, client=http_client))
        sources.append(manual)
        if cfg.demo_mode or not cfg.udl_enabled:
            sources.append(DemoElsetSource())
    flight = SingleFlight()
    refresher = Refresher(cfg, store, sources, flight)
    global_rl = RateLimiter(limit=120, window_s=60.0)
    strict_rl = RateLimiter(limit=6, window_s=60.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio
        ok, detail = store.probe_write()
        _log.info("storage boot verdict: %s (%s)", "WRITABLE" if ok else "UNWRITABLE", detail)
        refresher.run_once()  # seed the store immediately so `/` is never empty
        if cfg.udl_enabled:
            n = len(store.load().get("objects", {}))
            if n == 0:
                _log.warning(
                    "UDL enabled but first refresh added 0 objects; the demo "
                    "belt is OFF while UDL_BASE_URL is set, so the plot will be "
                    "empty. Verify against the tenant: UDL_ELSET_PATH=%s, "
                    "UDL_ACCEPT=%s, UDL_EPOCH_PARAM=%s, UDL_TARGET_FIELD=%s, "
                    "and the UDL_USER/UDL_PASSWORD credentials.",
                    cfg.udl_elset_path, cfg.udl_accept,
                    cfg.udl_epoch_param, cfg.udl_target_field,
                )
            else:
                _log.info("UDL first refresh: %d objects in store", n)
        stop = asyncio.Event()
        task = asyncio.create_task(refresher.scheduler(stop))
        try:
            yield
        finally:
            stop.set()
            task.cancel()

    app = FastAPI(title="PSIRENS", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.store = store
    app.state.manual = manual
    app.state.refresher = refresher

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "anon"

    async def _global_gate(request: Request):
        if not global_rl.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="rate limit")

    def _require_token(authorization: str | None = Header(default=None)):
        if not cfg.team_token:
            return  # single-user local mode, auth off
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not token_ok(given, cfg.team_token):
            raise HTTPException(status_code=401, detail="unauthorized")

    def _cors(resp: Response) -> Response:
        if cfg.allowed_origin and cfg.allowed_origin != "*":
            resp.headers["Access-Control-Allow-Origin"] = cfg.allowed_origin
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    # -- health (unauthenticated) ----------------------------------------
    @app.get("/healthz")
    def healthz():
        ok, detail = store.probe_write()
        if ok:
            return JSONResponse({"status": "ok", "data_dir": detail})
        return JSONResponse({"status": "unwritable", "detail": detail}, status_code=503)

    @app.get("/readyz")
    def readyz():
        return {"status": "ready", "last_refresh":
                refresher.last_run.isoformat() if refresher.last_run else None}

    # -- SPA + data (open reads) -----------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(_STATIC, "index.html"), encoding="utf-8") as fh:
            return _cors(HTMLResponse(fh.read()))

    # Icon and manifest assets (whitelisted; no path traversal).
    _ASSETS = {
        "icon-512.png": "image/png",
        "icon-192.png": "image/png",
        "favicon-32.png": "image/png",
        "apple-touch-icon.png": "image/png",
        "psirens-banner.png": "image/png",
        "manifest.webmanifest": "application/manifest+json",
    }

    @app.get("/favicon.ico")
    def favicon():
        return _cors(FileResponse(os.path.join(_STATIC, "favicon-32.png"),
                                  media_type="image/png"))

    @app.get("/static/{name}")
    def static_asset(name: str):
        media = _ASSETS.get(name)
        if media is None:
            raise HTTPException(status_code=404, detail="not found")
        return _cors(FileResponse(os.path.join(_STATIC, name), media_type=media))

    @app.get("/api/tracks", dependencies=[Depends(_global_gate)])
    def tracks(request: Request, view: str = "combined", modes: str = "",
               if_none_match: str | None = Header(default=None)):
        if view not in VIEW_MODES:
            raise HTTPException(status_code=400, detail="unknown view")
        if modes.strip():
            wanted = set()
            for m in modes.split(","):
                try:
                    wanted.add(DataMode(m.strip().upper()))
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"bad mode {m!r}")
        else:
            wanted = set(VIEW_MODES[view])
        payload = _tracks_payload(cfg, store, view, wanted)
        body = json.dumps(payload, separators=(",", ":"))
        # ETag is a CACHE KEY (not security): hash stable content only, so an
        # unchanged dataset returns 304 even though generated_at advances.
        stable = json.dumps(
            {k: payload[k] for k in ("view", "classification_banner", "bounds",
                                     "count", "tracks")},
            separators=(",", ":"), sort_keys=True,
        )
        etag = '"' + hashlib.sha1(stable.encode()).hexdigest() + '"'  # noqa: S324
        if if_none_match == etag:
            return _cors(Response(status_code=304))
        resp = Response(content=body, media_type="application/json")
        resp.headers["ETag"] = etag
        return _cors(resp)

    @app.get("/api/meta")
    def meta():
        return _cors(JSONResponse({
            "classification_default": "UNCLASSIFIED",
            "views": {k: [m.value for m in v] for k, v in VIEW_MODES.items()},
            "refresh_seconds": cfg.refresh_seconds,
            "retention_days": cfg.retention_days,
            "last_refresh": refresher.last_run.isoformat() if refresher.last_run else None,
            "manual_count": len(manual.list_active()),
        }))

    # -- state-changing (token-gated + strict rate limit) ----------------
    @app.post("/api/manual-elset", dependencies=[Depends(_require_token)])
    def add_manual(elset: ManualElsetIn, request: Request):
        if not strict_rl.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="rate limit")
        manual.add(elset)
        refresher.merge_one(manual)  # deterministic, not single-flight
        return _cors(JSONResponse({"status": "added", "object_id": elset.object_id}))

    @app.delete("/api/manual-elset/{object_id}", dependencies=[Depends(_require_token)])
    def del_manual(object_id: str):
        removed = manual.remove(object_id)
        if not removed:
            raise HTTPException(status_code=404, detail="not found")
        store.remove_object(object_id)  # drop from the plot immediately
        return _cors(JSONResponse({"status": "removed", "object_id": object_id}))

    @app.post("/api/refresh", dependencies=[Depends(_require_token)])
    def refresh(request: Request):
        if not strict_rl.allow(_client_key(request)):
            raise HTTPException(status_code=429, detail="rate limit")
        return _cors(JSONResponse(refresher.run_once()))

    return app


# ASGI entrypoint for gunicorn: `gunicorn --pythonpath src psirens.main:app`
app = create_app()
