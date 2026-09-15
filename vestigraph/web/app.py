"""FastAPI application factory and the ``serve`` entry point.

Binds 127.0.0.1 only, one process, one worker, no reload, no API docs page,
no CORS. Static files are served from the package (``web/static``).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..service.application import Application
from ..service.errors import ServiceError
from . import schemas
from .auth import AuthManager, cookie_name
from .private_file import write_owner_only as _write_owner_only
from .routes import router

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTROL_BODY_LIMIT = 64 * 1024
DEFAULT_PORT = 8787
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
# Same-origin only: no inline scripts, no remote assets, never framed.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
log = logging.getLogger("vestigraph.web")


def _loopback(host: str) -> str:
    value = "127.0.0.1" if host.strip().lower() == "localhost" else host.strip()
    try:
        if not ipaddress.ip_address(value).is_loopback:
            raise ValueError
    except ValueError:
        raise ServiceError("HOST_NOT_LOOPBACK", "The web service only binds loopback addresses.",
                           next_action="Use 127.0.0.1; LAN/public listening is not supported in this version.") from None
    return value


def create_app(application: Application, *, host="127.0.0.1", port=DEFAULT_PORT,
               auth: AuthManager | None = None) -> FastAPI:
    host = _loopback(host)
    app = FastAPI(title="Vestigraph", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.application = application
    app.state.auth = auth or AuthManager()
    app.state.host, app.state.port = host, int(port)
    app.state.cookie_name = cookie_name(port)
    hostpart = f"[{host}]" if ":" in host else host          # IPv6 literal needs brackets in URLs
    app.state.origin = f"http://{hostpart}:{int(port)}"
    # The page works from both spellings of loopback; a write must come from the same one.
    app.state.origins = {app.state.origin, f"http://localhost:{int(port)}"}
    app.state.allowed_hosts = {f"{hostpart}:{int(port)}", f"localhost:{int(port)}"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        if request.headers.get("host") not in app.state.allowed_hosts:
            error = ServiceError("HOST_NOT_ALLOWED", "Unexpected Host header.", status=421,
                                 next_action=f"Open {app.state.origin} directly.")
            return JSONResponse(status_code=error.status, content=schemas.fail(error, request.state.request_id))
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            length = request.headers.get("content-length")
            if "chunked" in request.headers.get("transfer-encoding", "").lower():
                error = ServiceError("LENGTH_REQUIRED", "Control requests need a Content-Length.", status=411,
                                     next_action="Send the body with Content-Length; chunked uploads are not accepted.")
                return JSONResponse(status_code=411, content=schemas.fail(error, request.state.request_id))
            if length is not None and length.isdigit() and int(length) > CONTROL_BODY_LIMIT:
                error = ServiceError("PAYLOAD_TOO_LARGE", "Control requests are limited to 64 KiB.", status=413,
                                     next_action="Send a smaller request; file uploads are not supported.")
                return JSONResponse(status_code=413, content=schemas.fail(error, request.state.request_id))
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content=schemas.fail(exc, request.state.request_id))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        error = ServiceError("BAD_REQUEST", "Request validation failed.", status=400,
                             next_action="Correct the highlighted fields.",
                             details=[{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()][:20])
        return JSONResponse(status_code=400, content=schemas.fail(error, request.state.request_id))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        error = ServiceError(code, str(exc.detail), status=exc.status_code, next_action="Check the URL.")
        return JSONResponse(status_code=exc.status_code, content=schemas.fail(error, request.state.request_id))

    import sqlite3
    from ..store import RepositoryError

    @app.exception_handler(sqlite3.DatabaseError)
    @app.exception_handler(RepositoryError)
    async def history_error(request: Request, exc):
        log.exception("history unavailable (request %s)", request.state.request_id)
        error = ServiceError("HISTORY_UNREADABLE", "History data could not be read.", status=503,
                             next_action="Preserve the history folder and run the integrity check before recovery.")
        return JSONResponse(status_code=503, content=schemas.fail(error, request.state.request_id))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("unhandled error (request %s)", request.state.request_id)
        error = ServiceError("INTERNAL_ERROR", "The service hit an unexpected error.", status=500,
                             next_action="Retry; if it repeats, report the request id from the response.")
        return JSONResponse(status_code=500, content=schemas.fail(error, request.state.request_id))

    app.include_router(router)
    from .skill_routes import router as skill_router
    app.include_router(skill_router)
    from .agent_routes import router as agent_router
    app.include_router(agent_router)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}       # anonymous liveness only; no names, paths or versions

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    @app.get("/static/{name:path}")
    async def static(name: str):
        target = (STATIC_DIR / name).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            raise StarletteHTTPException(status_code=404, detail="Not found")
        # Explicit types: ES modules need a JavaScript MIME type and Windows
        # registries sometimes map .js to text/plain.
        media_type = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        return FileResponse(target, media_type=media_type, headers={"Cache-Control": "no-store"})

    return app


def prepare_state(state_dir=None, *, zero_config=True, services=None, skill_services=None):
    """Open (or, for zero-config use, create) the state dir and the catch-all project."""
    from ..service.catalog import Catalog
    from ..service.paths import default_history_root, default_state_dir
    from ..service.state import ServiceState
    root = Path(state_dir) if state_dir else default_state_dir()
    if not (root / "vestigraph-service.json").is_file():
        if not zero_config:
            raise ServiceError("STATE_NOT_INITIALIZED", "Service state is not initialized.",
                               next_action="Run: python -m vestigraph service init --state DIR")
        Catalog.init(ServiceState.init(root))
    application = Application.open(root, services=services, skill_services=skill_services)
    if zero_config and not application.catalog.list_projects():
        application.catalog.ensure_default_project(default_history_root())
    return application


def serve(state_dir=None, *, host="127.0.0.1", port=DEFAULT_PORT, open_browser=False, log_level="warning",
          control_file=False, control_stdio=False, exit_when_idle=None, zero_config=True, services=None, skill_services=None) -> int:
    """Run the service until interrupted (or until every KLayout is gone, with --exit-when-idle).

    Sign-in links: printed once on stderr; a launcher (the KLayout plugin) can ask
    for fresh ones over stdin/stdout (``control_stdio``) or, when it did not start
    this process, through the owner-only control file (``control_file``).
    """
    import secrets
    host = _loopback(host)
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind((host, int(port)))
        probe.close()
    except OSError as exc:
        raise ServiceError("PORT_IN_USE", f"Cannot listen on {host}:{port}: {exc}",
                           next_action="Stop the other program on that port or pass --port with a free one. "
                                       "The service never falls back to other interfaces.") from exc
    application = prepare_state(state_dir, zero_config=zero_config, services=services, skill_services=skill_services).start()
    auth = AuthManager()
    app = create_app(application, host=host, port=port, auth=auth)
    control_secret = secrets.token_urlsafe(32)
    app.state.control_secret = control_secret
    control_path = application.state.root / "control.json"
    if control_file:
        restricted = _write_owner_only(control_path, json.dumps({"port": int(port), "host": host, "pid": os.getpid(),
                                                                 "secret": control_secret, "started_at": time.time()}))
        if not restricted:
            app.state.control_secret = None
            application.stop()
            raise ServiceError("CONTROL_FILE_NOT_PRIVATE", "Cannot create a private control credential file.",
                               status=503, next_action="Use a private local filesystem with ACL support or disable --control-file.")
    link = f"http://{host}:{port}/#bootstrap={auth.issue_bootstrap()}"
    print(f"Vestigraph service: state={application.state.root}", file=sys.stderr)
    print(f"Sign in within 5 minutes (single use): {link}", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(link)
    import uvicorn
    config = uvicorn.Config(app, host=host, port=int(port), workers=1, reload=False,
                            log_level=log_level, access_log=False, server_header=False, date_header=False)
    server = uvicorn.Server(config)
    threading.Thread(target=_sweep_loop, args=(server, application), daemon=True,
                     name="vestigraph-cache-sweep").start()
    if control_stdio:
        threading.Thread(target=_stdio_control, args=(server, auth, host, port), daemon=True,
                         name="vestigraph-control-stdio").start()
    if exit_when_idle:
        threading.Thread(target=_idle_watch, args=(server, application, float(exit_when_idle)), daemon=True,
                         name="vestigraph-idle-watch").start()
    try:
        server.run()
    finally:
        application.stop()
        if control_file:
            try:
                control_path.unlink()
            except OSError:
                pass
    return 0


def _stdio_control(server, auth, host, port):
    """Line protocol for the launcher that owns this process: issue-link / shutdown."""
    for raw in sys.stdin:
        line = raw.strip()
        if line == "issue-link":
            print(json.dumps({"link": f"http://{host}:{port}/#bootstrap={auth.issue_bootstrap()}"}), flush=True)
        elif line == "shutdown":
            server.should_exit = True
            return
        elif line == "ping":
            print(json.dumps({"ok": True}), flush=True)
    # stdin closed: the launcher is gone; keep running (another KLayout may still be up).


def _sweep_loop(server, application):
    """Expire cached downloads/previews/diffs/history exports and keep the cache under budget."""
    from ..service.application import SWEEP_INTERVAL_S
    while not server.should_exit:
        try:
            application.sweep_assets()
        except Exception as exc:  # noqa: BLE001 - housekeeping must never take the service down
            log.warning("cache sweep failed: %s", exc)
        for _ in range(int(SWEEP_INTERVAL_S / 2)):
            if server.should_exit:
                return
            time.sleep(2.0)


def _idle_watch(server, application, grace_s):
    """Discovery failures are not evidence that every editor has exited."""
    from vestigraph_backends.types import Availability
    directory = application.supervisor.editors
    if directory is None:
        return
    last_online = time.monotonic()
    while not server.should_exit:
        time.sleep(2.0)
        try:
            online = any(s.availability == Availability.ONLINE for s in directory.refresh())
            if directory.errors:
                last_online = time.monotonic()
                continue
        except Exception:
            last_online = time.monotonic()
            continue
        if online:
            last_online = time.monotonic()
        elif time.monotonic() - last_online >= grace_s:
            print("Vestigraph service: no editor session online; exiting.", file=sys.stderr)
            server.should_exit = True
            return
