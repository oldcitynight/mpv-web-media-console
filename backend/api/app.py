from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from backend.auth import bearer_token
from backend.config.network import NetworkSettings
from backend.security.logging import install_secret_redaction_filters
from backend.security.trusted_lan import TrustedLanSourceMiddleware

from .frontend_assets import install_frontend_routes
from .errors import ErrorCode, StructuredApiError, register_error_handlers
from .models import (
    Chapter,
    PerformanceProbeStartRequest,
    PerformanceProbeCancelRequest,
    CommandResponse,
    HealthResponse,
    LoadRequest,
    MediaBrowserResponse,
    MediaMetadataResponse,
    PlayerStateResponse,
    Profile,
    OutputInventoryResponse,
    ProfileApplyRequest,
    ProfileApplyResponse,
    QueueAddRequest,
    QueueItem,
    QueueMoveRequest,
    QueueRevisionRequest,
    QueueResponse,
    SeekRequest,
    Track,
    TrackSelectRequest,
    ValueRequest,
)

try:
    from fastapi import FastAPI, Header, Query, Request, WebSocket
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - exercised only in dependency-free tooling
    FastAPI = None  # type: ignore[assignment,misc]
    Header = None  # type: ignore[assignment,misc]
    Query = None  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[misc,assignment]
    WebSocket = Any  # type: ignore[misc,assignment]
    CORSMiddleware = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]

from backend.ws.origin import BrowserOriginPolicy, OriginPolicyError

MAX_API_REQUEST_BODY_BYTES = 1024 * 1024


class RequestBodyLimitMiddleware:
    """Bound API bodies before any router, parser, or service middleware runs."""

    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def _reject(self, send: Any, status_code: int, code: str, message: str) -> None:
        body = (
            f'{{"code":"{code}","message":"{message}"}}'
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "") if isinstance(scope, dict) else ""
        if (
            scope.get("type") != "http"
            or not isinstance(path, str)
            or not (path == "/api/v1" or path.startswith("/api/v1/"))
        ):
            await self.app(scope, receive, send)
            return

        try:
            headers = scope.get("headers", ())
            content_lengths = [
                value
                for name, value in headers
                if isinstance(name, bytes) and name.lower() == b"content-length"
            ]
        except (TypeError, ValueError):
            await self._reject(
                send, 400, "invalid_content_length", "invalid Content-Length header"
            )
            return

        if len(content_lengths) > 1 or (
            content_lengths
            and (
                not isinstance(content_lengths[0], bytes)
                or not content_lengths[0].isdigit()
            )
        ):
            await self._reject(
                send, 400, "invalid_content_length", "invalid Content-Length header"
            )
            return
        if content_lengths and int(content_lengths[0]) > self.max_body_bytes:
            await self._reject(
                send,
                413,
                "request_too_large",
                "request body exceeds the 1 MiB limit",
            )
            return

        # Buffer at most the configured limit before invoking downstream ASGI
        # middleware. This both bounds chunked requests and ensures no route can
        # consume a prefix of an oversized body before the 413 is decided.
        buffered_body = bytearray()
        terminal_message: dict[str, Any] | None = None
        body_complete = False
        total_bytes = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type != "http.request":
                terminal_message = message
                break
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                await self._reject(
                    send, 400, "invalid_request_body", "invalid HTTP request body"
                )
                return
            total_bytes += len(body)
            if total_bytes > self.max_body_bytes:
                await self._reject(
                    send,
                    413,
                    "request_too_large",
                    "request body exceeds the 1 MiB limit",
                )
                return
            buffered_body.extend(body)
            if not message.get("more_body", False):
                body_complete = True
                break

        replay_messages = [
            {
                "type": "http.request",
                "body": bytes(buffered_body),
                "more_body": not body_complete,
            }
        ]
        if terminal_message is not None:
            replay_messages.append(terminal_message)
        message_index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal message_index
            if message_index < len(replay_messages):
                message = replay_messages[message_index]
                message_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)


def create_app(
    service: Any | None = None, *, authentication_enabled: bool | None = None
):
    """Create a fail-closed API bound to one service instance.

    Tests and embedders may inject a service. The module-level production app
    is composed explicitly by ``create_production_app`` below.
    """

    if FastAPI is None:
        raise RuntimeError("FastAPI is required to create the control API")
    install_secret_redaction_filters()
    network_settings = NetworkSettings.from_environment()
    try:
        origin_policy = BrowserOriginPolicy.from_environment()
    except OriginPolicyError as error:
        raise ValueError(str(error)) from error
    if authentication_enabled is None:
        authentication_enabled = network_settings.auth_enabled
    elif not isinstance(authentication_enabled, bool):
        raise TypeError("authentication_enabled must be a boolean")
    elif network_settings.auth_enabled and not authentication_enabled:
        raise ValueError("configured API authentication cannot be disabled at application creation")

    @asynccontextmanager
    async def lifespan(_app):
        if service is not None:
            start = getattr(service, "start", None)
            if not callable(start):
                raise RuntimeError("configured player service has no start lifecycle")
            start()
        try:
            yield
        finally:
            if service is not None:
                close = getattr(service, "close", None)
                if not callable(close):
                    raise RuntimeError("configured player service has no close lifecycle")
                close()

    app = FastAPI(title="mpv-webd control API", version="1.0.0", lifespan=lifespan)
    register_error_handlers(app)

    public_http_paths = {"/api/v1/health/live", "/api/v1/health/ready"}

    @app.middleware("http")
    async def authenticate_api_requests(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/v1/"):
            return await call_next(request)

        raw_headers = request.scope.get("headers", ())
        origin_values = [
            value
            for name, value in raw_headers
            if isinstance(name, bytes) and name.lower() == b"origin"
        ] if isinstance(raw_headers, (list, tuple)) else []
        if origin_values and (
            len(origin_values) != 1
            or not isinstance(origin_values[0], bytes)
            or not origin_policy.allows(origin_values[0].decode("latin-1"))
        ):
            return JSONResponse(
                status_code=403,
                content={"code": "origin_not_allowed", "message": "browser origin is not allowed"},
            )

        if not authentication_enabled or path in public_http_paths:
            return await call_next(request)

        authorization_values = [
            value
            for name, value in raw_headers
            if isinstance(name, bytes) and name.lower() == b"authorization"
        ] if isinstance(raw_headers, (list, tuple)) else []
        token = None
        if len(authorization_values) == 1 and isinstance(authorization_values[0], bytes):
            token = bearer_token(authorization_values[0].decode("latin-1"))

        repository = getattr(service, "runtime_repository", None)
        verify = getattr(repository, "authenticate_token", None)
        if token is None or not callable(verify) or not verify(token):
            return JSONResponse(
                status_code=401,
                content={"code": "unauthorized", "message": "authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    if CORSMiddleware is None:  # pragma: no cover - guarded with FastAPI above
        raise RuntimeError("FastAPI CORS middleware is required to create the control API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(origin_policy.origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        max_age=600,
    )
    # Added after CORS so this request gate is outermost: oversized bodies are
    # rejected before preflight, authentication, routing, or request parsing.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=MAX_API_REQUEST_BODY_BYTES,
    )
    if network_settings.trusted_lan_network is not None:
        # Added last so every HTTP, UI, static-asset, and WebSocket request is
        # filtered against the ASGI peer address before reaching other middleware.
        app.add_middleware(
            TrustedLanSourceMiddleware,
            network=network_settings.trusted_lan_network,
        )

    def call(name: str, *args: Any, **kwargs: Any) -> Any:
        if service is None:
            raise StructuredApiError(
                ErrorCode.PLAYER_UNAVAILABLE,
                "The playback service is not configured.",
            )
        method = getattr(service, name, None)
        if not callable(method):
            raise StructuredApiError(
                ErrorCode.PLAYER_UNAVAILABLE,
                "The playback service is not ready.",
            )
        return method(*args, **kwargs)

    @app.get("/api/v1/health/live", response_model=HealthResponse)
    def health_live() -> HealthResponse:
        return call("health_live")

    @app.get("/api/v1/health/ready", response_model=HealthResponse)
    def health_ready() -> HealthResponse:
        return call("health_ready")

    @app.get("/api/v1/player/state", response_model=PlayerStateResponse)
    def player_state() -> PlayerStateResponse:
        return call("player_state")

    @app.post("/api/v1/diagnostics/performance/start", status_code=202, include_in_schema=False)
    def performance_probe_start(request: PerformanceProbeStartRequest,
                                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        return call("performance_probe_start", request, idempotency_key)

    @app.post("/api/v1/diagnostics/performance/cancel", include_in_schema=False)
    def performance_probe_cancel(request: PerformanceProbeCancelRequest):
        return call("performance_probe_cancel", request)

    @app.get("/api/v1/diagnostics/performance/status", include_in_schema=False)
    def performance_probe_status():
        return call("performance_probe_status")

    @app.get("/api/v1/diagnostics/performance/result", include_in_schema=False)
    def performance_probe_result():
        return call("performance_probe_result")

    @app.get(
        "/api/v1/media/browse",
        response_model=MediaBrowserResponse,
        summary="Browse configured server-side media roots",
        description=(
            "Lists only regular files and directories whose canonical paths remain inside "
            "configured local media roots. An omitted directory lists configured roots; "
            "search filters the current directory only. Network URLs and arbitrary "
            "browser-local paths are not browse targets."
        ),
        responses={403: {"description": "No configured roots or directory outside the local allowlist"}},
    )
    def media_browse(
        directory: str | None = Query(default=None, max_length=4096),
        search: str = Query(default="", max_length=128),
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        version: str | None = Query(default=None, max_length=64),
        sort: str = Query(default="name", pattern="^(name|name_desc|modified)$"),
        details: bool = Query(default=True),
    ) -> MediaBrowserResponse:
        return call(
            "media_browse",
            directory=directory,
            search=search,
            limit=limit, offset=offset, version=version, sort=sort, details=details,
        )

    @app.get(
        "/api/v1/media/metadata",
        response_model=MediaMetadataResponse,
        summary="Describe one configured media item with bounded metadata",
        description=(
            "Returns evidence-backed short codec and dynamic-range labels plus the selected "
            "bounded preview source. Unknown metadata remains explicitly Unknown."
        ),
        responses={
            403: {"description": "Media item is outside the local allowlist"},
            404: {"description": "Media metadata is unavailable"},
        },
    )
    def media_metadata(path: str = Query(..., min_length=1, max_length=4096)) -> MediaMetadataResponse:
        return call("media_metadata", path)

    @app.get(
        "/api/v1/media/preview",
        summary="Serve a bounded media preview for one configured media item",
        responses={
            200: {
                "description": "Embedded art, sibling art, representative video frame, or a safe placeholder",
                "content": {
                    "image/jpeg": {},
                    "image/png": {},
                    "image/webp": {},
                    "image/svg+xml": {},
                },
            },
            403: {"description": "Media item or preview is outside the local allowlist"},
            404: {"description": "The media item is unavailable"},
        },
    )
    def media_preview(path: str = Query(..., min_length=1, max_length=4096)) -> Any:
        return call("media_preview", path)

    @app.post("/api/v1/player/load", response_model=CommandResponse, status_code=202)
    def player_load(
        request: LoadRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CommandResponse:
        return call("player_load", request, idempotency_key)

    @app.post("/api/v1/player/play", response_model=CommandResponse)
    def player_play(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CommandResponse:
        return call("player_play", idempotency_key)

    @app.post("/api/v1/player/pause", response_model=CommandResponse)
    def player_pause(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CommandResponse:
        return call("player_pause", idempotency_key)

    @app.post("/api/v1/player/seek", response_model=CommandResponse)
    def player_seek(
        request: SeekRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CommandResponse:
        return call("player_seek", request, idempotency_key)

    @app.post("/api/v1/player/stop", response_model=CommandResponse)
    def player_stop(
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CommandResponse:
        return call("player_stop", idempotency_key)

    @app.post("/api/v1/player/volume", response_model=CommandResponse)
    def player_volume(
        request: ValueRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CommandResponse:
        return call("player_volume", request, idempotency_key)

    @app.post("/api/v1/player/speed", response_model=CommandResponse)
    def player_speed(
        request: ValueRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CommandResponse:
        return call("player_speed", request, idempotency_key)

    @app.get("/api/v1/player/tracks", response_model=list[Track])
    def player_tracks() -> list[Track]:
        return call("player_tracks")

    @app.post("/api/v1/player/tracks/{track_id}/select", response_model=CommandResponse)
    def player_track_select(track_id: int, request: TrackSelectRequest) -> CommandResponse:
        return call("player_select_track", track_id, request)

    @app.get("/api/v1/player/chapters", response_model=list[Chapter])
    def player_chapters() -> list[Chapter]:
        return call("player_chapters")

    @app.get("/api/v1/queue", response_model=QueueResponse)
    def queue_get() -> QueueResponse:
        return call("queue_get")

    @app.post("/api/v1/queue", response_model=QueueItem, status_code=201)
    def queue_add(request: QueueAddRequest) -> QueueItem:
        return call("queue_add", request)

    @app.delete("/api/v1/queue/items/{item_id}", response_model=QueueResponse)
    def queue_remove(
        item_id: str,
        expected_revision: int | None = Query(default=None, ge=0),
    ) -> QueueResponse:
        return call("queue_remove", item_id, expected_revision)

    @app.patch("/api/v1/queue/items/{item_id}", response_model=QueueResponse)
    def queue_move(item_id: str, request: QueueMoveRequest) -> QueueResponse:
        return call("queue_move", item_id, request)

    @app.post("/api/v1/queue/items/{item_id}/activate", response_model=QueueResponse)
    def queue_activate(item_id: str, request: QueueRevisionRequest) -> QueueResponse:
        return call("queue_activate", item_id, request)

    @app.delete("/api/v1/queue", response_model=QueueResponse)
    def queue_clear(
        expected_revision: int | None = Query(default=None, ge=0),
    ) -> QueueResponse:
        return call("queue_clear", expected_revision)

    @app.get("/api/v1/outputs", response_model=OutputInventoryResponse)
    def output_inventory() -> OutputInventoryResponse:
        return call("output_inventory")

    @app.get("/api/v1/profiles", response_model=list[Profile])
    def profiles() -> list[Profile]:
        return call("profiles")

    @app.post(
        "/api/v1/profiles/apply",
        response_model=ProfileApplyResponse,
        summary="Apply and resolve a profile selection",
        description=(
            'Accept profile IDs and catalog-validated typed overrides. Hardware output IDs resolve only through fresh reported devices and exact trusted routes. Stale outputs are rejected before player actions. Controlled restarts preserve unselected active output routes and require readback for all preserved routes. Backend and render-context profiles retain their compatibility IDs.'
        ),
    )
    def profile_apply(request: ProfileApplyRequest) -> ProfileApplyResponse:
        return call("profile_apply", request)

    @app.websocket("/api/v1/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        raw_headers = websocket.scope.get("headers", ())
        origin_values = [
            value
            for name, value in raw_headers
            if isinstance(name, bytes) and name.lower() == b"origin"
        ] if isinstance(raw_headers, (list, tuple)) else []
        if origin_values and (
            len(origin_values) != 1
            or not isinstance(origin_values[0], bytes)
            or not origin_policy.allows(origin_values[0].decode("latin-1"))
        ):
            await websocket.close(code=1008, reason="origin not allowed")
            return
        handler = getattr(service, "websocket", None) if service is not None else None
        if not callable(handler):
            await websocket.close(code=1013, reason="playback service unavailable")
            return
        await handler(websocket)

    install_frontend_routes(app)
    return app


def create_production_app():
    """Compose the service-owned production dependency graph."""

    if FastAPI is None:
        raise RuntimeError("FastAPI is required to create the control API")
    from .service import PlayerService

    settings = NetworkSettings.from_environment()
    return create_app(
        PlayerService.from_environment(), authentication_enabled=settings.auth_enabled
    )


app = create_production_app() if FastAPI is not None else None
