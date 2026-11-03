from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pvi import __version__
from pvi.api.routes import router
from pvi.config import Settings
from pvi.infrastructure.repository import RepositoryLoader


def _error(status: int, code: str, message: str, request_id: str, details=None):
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": request_id,
            }
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = (settings or Settings()).resolved()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loader = RepositoryLoader(
            configured.data_dir,
            configured.contract_dir,
            validate=configured.require_data_validation,
        )
        app.state.repository = loader.load()
        yield

    app = FastAPI(
        title="PVI Capability Service",
        version=__version__,
        openapi_version="3.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if isinstance(exc.detail, tuple):
            code, message = exc.detail
        else:
            code, message = "http_error", str(exc.detail)
        return _error(exc.status_code, code, message, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", "invalid value"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return _error(
            422,
            "request_validation_failed",
            "Request did not satisfy the API contract.",
            request.state.request_id,
            {"errors": details},
        )

    app.include_router(router)
    return app


app = create_app()
