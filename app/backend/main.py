from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import github, queries
from backend.authz import Forbidden, Reason
from backend.data import SqlDenied, SqlError

DEFAULT_STATIC = Path(__file__).resolve().parent / "static"


def _include_routers(app: FastAPI) -> None:
    # One include_router line per API module.
    # This runs before the SPA catch-all is registered, so API routes always win.
    from backend.api import candidates as candidates_api
    from backend.api import config as config_api
    from backend.api import drafts as drafts_api
    from backend.api import me as me_api
    from backend.api import pulls as pulls_api
    from backend.api import results as results_api
    from backend.api import runs as runs_api
    from backend.api import skills as skills_api
    from backend.api import try_panel as try_api

    app.include_router(config_api.router, prefix="/api")
    app.include_router(me_api.router, prefix="/api")
    app.include_router(skills_api.router, prefix="/api")
    app.include_router(candidates_api.router, prefix="/api")
    app.include_router(drafts_api.router, prefix="/api")
    app.include_router(pulls_api.router, prefix="/api")
    app.include_router(results_api.router, prefix="/api")
    app.include_router(runs_api.router, prefix="/api")
    app.include_router(try_api.router, prefix="/api")


def create_app(static_dir: Path = DEFAULT_STATIC) -> FastAPI:
    app = FastAPI(title="Skill Hub", version="0.1.0")

    @app.exception_handler(Forbidden)
    async def _forbidden(_request, exc: Forbidden):
        return JSONResponse(status_code=403, content={"detail": {"reason": exc.reason.value}})

    def _problem(status: int, reason: Reason) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": {"reason": reason.value}})

    @app.exception_handler(SqlDenied)
    async def _sql_denied(_request, exc: SqlDenied):
        return _problem(403, Reason.RESULTS_UNAVAILABLE if exc.not_found else Reason.NO_ACCESS)

    @app.exception_handler(SqlError)
    async def _sql_error(_request, _exc: SqlError):
        return _problem(502, Reason.SQL_FAILED)  # never echo server text

    @app.exception_handler(github.GitHubNotConfigured)
    async def _github_not_configured(_request, _exc: github.GitHubNotConfigured):
        return _problem(503, Reason.GITHUB_NOT_CONFIGURED)

    @app.exception_handler(github.GitHubError)
    async def _github_error(_request, exc: github.GitHubError):
        # The error type only: GitHub's message and the request are never sent to the browser.
        return JSONResponse(
            status_code=502,
            content={"detail": {"reason": Reason.GITHUB_FAILED.value, "error": type(exc).__name__}},
        )

    @app.exception_handler(queries.UnknownDepartment)
    async def _unknown_department(_request, _exc: queries.UnknownDepartment):
        return _problem(404, Reason.DEPARTMENT_NOT_FOUND)

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    _include_routers(app)

    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    root = static_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        index = root / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="frontend not built")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)

    return app


app = create_app()
