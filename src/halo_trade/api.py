"""HTTP surface. `create_app(ctx)` for tests and embedding; module-level `app` for uvicorn.

Every error is an `ErrorOut`. Validation errors never echo the offending value.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import alerts, plugin
from .auth import RateLimiter, current_user, current_user_for_close
from .config import Settings
from .context import AppContext
from .contracts import (
    CloseIn,
    ErrorCode,
    ErrorOut,
    Outcome,
    PreviewIn,
    PreviewOut,
    StateOut,
    SubmitIn,
    SubmitOut,
)
from .chain.shared import SharedReads
from .limits import ApiError
from .llm import make_client
from .monitor import Monitor
from .service import Service, User, window_of
from .startup import validate_settings
from .watcher import Watcher

logger = logging.getLogger(__name__)

OPTIONAL_ROUTERS = ("halo_trade.onboarding", "halo_trade.stats", "halo_trade.telegram")
_UNKNOWN_ON_FAILURE = frozenset({"/v1/submit", "/v1/close", "/onboard/agent/close"})
MAX_BODY_BYTES = 16 * 1024  # every legitimate request is far under this
# A 4096-character message is up to twelve bytes per character once JSON escapes it, and an update
# can also carry the message it replies to. The webhook's secret check runs after this limit.
PATH_BODY_LIMITS = {"/telegram/webhook": 256 * 1024}


class BodySizeLimit:
    """Refuses a request body over `MAX_BODY_BYTES` before the application sees it.

    Counts bytes as they arrive, so a chunked body with no Content-Length is cut off at the limit
    rather than buffered in memory. A pure ASGI wrapper: nothing past it can read an oversized body.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = PATH_BODY_LIMITS.get(scope.get("path", ""), self.limit)
        declared = next((value for name, value in scope["headers"] if name == b"content-length"), None)
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._too_large(scope, receive, send, limit)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > limit:
                await self._too_large(scope, receive, send, limit)
                return
            more = message.get("more_body", False)
        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    async def _too_large(
        self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]], limit: int
    ) -> None:
        response = _error_response(
            413,
            ErrorOut(outcome=Outcome.REJECTED, code=ErrorCode.INVALID_INTENT, limit=f"max_body_bytes={limit}"),
        )
        response.headers["Connection"] = "close"  # the rest of the body is not read
        await response(scope, receive, send)


def _error_response(status: int, out: ErrorOut) -> JSONResponse:
    return JSONResponse(out.model_dump(mode="json"), status_code=status)


def _attach(app: FastAPI, ctx: AppContext) -> None:
    service = Service(ctx)
    app.state.ctx = ctx
    app.state.service = service
    app.state.watcher = Watcher(service)
    app.state.monitor = Monitor(service)
    app.state.rate_limiter = RateLimiter()
    engine = plugin.engine()
    if engine is None:
        logger.warning("the decision engine is not part of this build: no agent runs and no order is decided")
    else:
        engine.attach(app.state, service)


def _service(request: Request) -> Service:
    return request.app.state.service


def include_optional_routers(app: FastAPI, modules: tuple[str, ...] = OPTIONAL_ROUTERS) -> list[str]:
    """Include sibling routers, skipping only those whose module does not exist yet."""
    included = []
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            logger.warning("router %s not present, skipping", name)
            continue
        app.include_router(module.router)
        included.append(name)
    return included


def _register(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status, exc.out)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if not request.url.path.startswith("/v1/"):
            return JSONResponse({"ok": False, "code": "INVALID_REQUEST", "limit": None}, status_code=422)
        user: Optional[User] = getattr(request.state, "user", None)
        ctx: Optional[AppContext] = getattr(request.app.state, "ctx", None)
        if user is not None and ctx is not None and request.url.path in ("/v1/preview", "/v1/submit", "/v1/close"):
            kind = request.url.path.rsplit("/", 1)[-1]
            service: Service = request.app.state.service
            await service._log_decision(user.id, window_of(ctx.clock()), kind, {"result": "rejected", "code": ErrorCode.INVALID_INTENT.value})
        return _error_response(422, ErrorOut(outcome=Outcome.REJECTED, code=ErrorCode.INVALID_INTENT))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s", request.url.path)
        unknown = request.url.path in _UNKNOWN_ON_FAILURE
        return _error_response(
            500,
            ErrorOut(
                outcome=Outcome.UNKNOWN if unknown else Outcome.REJECTED,
                code=ErrorCode.INTERNAL,
                check_state_after_s=30 if unknown else None,
            ),
        )

    @app.get("/v1/state", response_model=StateOut)
    async def get_state(request: Request, user: User = Depends(current_user)) -> StateOut:
        return await _service(request).state(user)

    @app.post("/v1/preview", response_model=PreviewOut)
    async def post_preview(request: Request, body: PreviewIn, user: User = Depends(current_user)) -> PreviewOut:
        return await _service(request).preview(user, body)

    @app.post("/v1/submit", response_model=SubmitOut)
    async def post_submit(request: Request, body: SubmitIn, user: User = Depends(current_user)) -> SubmitOut:
        return await _service(request).submit(user, body)

    @app.post("/v1/close", response_model=SubmitOut)
    async def post_close(request: Request, body: CloseIn, user: User = Depends(current_user_for_close)) -> SubmitOut:
        return await _service(request).close(user, body)

    @app.get("/v1/health")
    async def get_health(request: Request) -> dict[str, object]:
        ctx: Optional[AppContext] = getattr(request.app.state, "ctx", None)
        return {"ok": True, "heartbeat_age_ms": alerts.heartbeat_age_ms(ctx) if ctx is not None else None}


@asynccontextmanager
async def _run_background(app: FastAPI) -> AsyncIterator[None]:
    """Serve the watcher, its watchdog and any Telegram worker for the life of the app. With the
    engine present, also its loops and the price monitor that brings a decision forward."""
    watcher: Watcher = app.state.watcher
    stop = asyncio.Event()
    tasks = [asyncio.create_task(watcher.run(stop)), asyncio.create_task(watcher.watchdog(stop))]
    engine = plugin.engine()
    if engine is not None:
        tasks.append(asyncio.create_task(app.state.monitor.run(stop)))
        tasks.extend(asyncio.create_task(loop) for loop in engine.loops(app.state, stop))
    if app.state.ctx.settings.telegram_configured:
        from .telegram import TelegramWorker  # Lazy: unused when no bot is configured.

        tasks.append(asyncio.create_task(TelegramWorker(app.state.service).run(stop)))
    try:
        yield
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _build(lifespan: Callable[[FastAPI], "AsyncIterator[None]"]) -> FastAPI:
    app = FastAPI(title="halo-trade", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodySizeLimit)
    _register(app)
    include_optional_routers(app)
    return app


def create_app(ctx: AppContext) -> FastAPI:
    validate_settings(ctx.settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with _run_background(app):
            yield

    app = _build(lifespan)
    _attach(app, ctx)
    return app


def _context_from_env() -> AppContext:
    from .chain.factory import make_chain
    from .db import connect

    settings = Settings()
    validate_settings(settings)
    engine = plugin.engine()
    feeds = None if engine is None else engine.feeds(settings)
    return AppContext(settings=settings, db=connect(settings.db_path), chain=SharedReads(make_chain(settings)), llm=make_client(settings), feeds=feeds)


@asynccontextmanager
async def _env_lifespan(app: FastAPI) -> AsyncIterator[None]:
    _attach(app, _context_from_env())
    async with _run_background(app):
        yield
    ctx: AppContext = app.state.ctx
    aclose: Optional[Callable[[], Awaitable[None]]] = getattr(ctx.chain, "aclose", None)
    if aclose is not None:
        await aclose()
    if ctx.llm is not None:
        await ctx.llm.aclose()
    if ctx.feeds is not None:
        await ctx.feeds.aclose()
    ctx.db.close()


app = _build(_env_lifespan)
