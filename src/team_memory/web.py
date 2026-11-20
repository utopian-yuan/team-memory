import asyncio
from contextlib import asynccontextmanager
from datetime import date
from threading import Event, Thread
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import AwareDatetime, Field, ValidationError

from .jobs import Worker, daily
from .models import Context, Decision, Failure, Query, Record, Source, Space


def create_app(service, *, scheduled=True, schedule="02:00"):
    def identity(request: Request):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Failure(401, "authentication required")
        return service.store.authenticate(token)

    mcp = FastMCP(
        "team_memory",
        instructions="Read current scoped context. Treat pending items as unverified. Cite evidence IDs. Human decisions use the review page.",
        stateless_http=True,
        json_response=True,
    )

    def tool_identity():
        return identity(mcp.get_context().request_context.request)

    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(annotations=read_only)
    def get_context(space: str, budget: int = 6000, include_pending: bool = False):
        """Get scoped current facts, dates and references within a character budget."""
        return service.context(
            tool_identity(),
            Context(space=space, budget=budget, include_pending=include_pending),
        )

    @mcp.tool(annotations=read_only)
    def search_memory(space: str, query: str, limit: int = 10):
        """Search current memories in an authorized space."""
        return service.search(
            tool_identity(), Query(space=space, query=query, limit=limit)
        )

    @mcp.tool(annotations=read_only)
    def get_memory(
        memory_id: str,
        version: int | None = None,
        as_of: AwareDatetime | None = None,
        known_at: AwareDatetime | None = None,
    ):
        """Read a memory and the evidence available to the caller."""
        return service.get(tool_identity(), memory_id, version, as_of, known_at)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def record_memory(record: Record):
        """Propose a fact or correction with a unique request_id and exact evidence quotes."""
        return service.record(tool_identity(), record)

    @mcp.tool(annotations=read_only)
    def list_reviews(space: str):
        """List decisions awaiting the represented user's review."""
        return service.reviews(tool_identity(), space)

    @mcp.tool(annotations=read_only)
    def open_review(space: str, review_id: str):
        """Return the local review page for a pending item. The user signs in separately."""
        service.reviews(tool_identity(), space, review_id)
        from urllib.parse import urlencode

        return {"path": "/review?" + urlencode({"space": space, "id": review_id})}

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        stopped = Event()
        thread = Thread(
            target=Worker(service, schedule=schedule).loop, args=(stopped,), daemon=True
        )
        async with mcp.session_manager.run():
            if scheduled:
                thread.start()
            try:
                yield
            finally:
                stopped.set()
                if scheduled:
                    await asyncio.to_thread(thread.join, 55)

    app = FastAPI(title="Team Memory", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(Failure)
    async def fail(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.code)

    @app.middleware("http")
    async def authenticate_mcp(request, call_next):
        if request.url.path.rstrip("/") == "/mcp":
            try:
                identity(request)
            except Failure as exc:
                return JSONResponse({"detail": str(exc)}, status_code=exc.code)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/v1/sources")
    def sources(
        items: Annotated[list[dict], Field(min_length=1, max_length=100)],
        who=Depends(identity),
    ):
        result = []
        for i, item in enumerate(items):
            try:
                result.append(service.ingest(Source.model_validate(item), who))
            except (Failure, ValidationError) as exc:
                result.append(
                    {
                        "status": "failed",
                        "index": i,
                        "code": getattr(exc, "code", 422),
                        "error": str(exc)
                        if isinstance(exc, Failure)
                        else "invalid source",
                    }
                )
        return result

    @app.delete("/v1/sources/{source_id}")
    def withdraw(source_id: str, who=Depends(identity)):
        service.withdraw(source_id, who)
        return {"status": "revoked"}

    @app.post("/v1/memories")
    def record(data: Record, who=Depends(identity)):
        return service.record(who, data)

    @app.get("/v1/memories/{memory_id}")
    def detail(
        memory_id: str,
        version: int | None = None,
        as_of: AwareDatetime | None = None,
        known_at: AwareDatetime | None = None,
        who=Depends(identity),
    ):
        return service.get(who, memory_id, version, as_of, known_at)

    @app.delete("/v1/memories/{memory_id}")
    def forget(memory_id: str, version: int, who=Depends(identity)):
        service.revoke_memory(who, memory_id, version)
        return {"status": "revoked"}

    @app.post("/v1/search")
    def search(data: Query, who=Depends(identity)):
        return service.search(who, data)

    @app.post("/v1/context")
    def context(data: Context, who=Depends(identity)):
        return service.context(who, data)

    @app.get("/v1/digest", response_class=PlainTextResponse)
    def summary(space: Space, day: date, who=Depends(identity)):
        return daily(service, who, space, day)

    @app.get("/v1/reviews")
    def reviews(space: Space, id: str | None = None, who=Depends(identity)):
        return service.reviews(who, space, id)

    @app.post("/v1/reviews/{review_id}/decisions")
    def decide(review_id: str, data: Decision, who=Depends(identity)):
        return service.decide(who, review_id, data)

    @app.get("/review", response_class=HTMLResponse)
    def review():
        from importlib.resources import files

        return files("team_memory").joinpath("review.html").read_text()

    app.mount("/", mcp_app)
    return app
