"""Composition root, HTTP adapters and authentication outside the LLM."""

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from opspilot.api.contracts import (
    Citation,
    DocumentInput,
    DocumentOutput,
    QueryInput,
    QueryOutput,
    RetrievedChunk,
)
from opspilot.api.middleware import RequestMiddleware
from opspilot.application import RagService
from opspilot.config import Settings
from opspilot.domain import AppError, Chunk, Metadata
from opspilot.observability import request_id
from opspilot.persistence.postgres import PostgresRepository
from opspilot.providers.fake import FakeProvider
from opspilot.providers.openai import OpenAIProvider


def citation(chunk: Chunk) -> Citation:
    return Citation(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        ordinal=chunk.ordinal,
        title=chunk.title,
        source=chunk.source,
        start_offset=chunk.start,
        end_offset=chunk.end,
        quote=chunk.text,
    )


def create_app(settings: Settings | None = None, service: RagService | None = None) -> FastAPI:
    if settings is None:
        try:
            settings = Settings.from_env()
        except Exception:
            raise RuntimeError("Invalid environment configuration.") from None
    config = settings
    for name in ("httpx", "httpcore", "openai", "sqlalchemy", "uvicorn.error"):
        external_logger = logging.getLogger(name)
        external_logger.handlers = [logging.NullHandler()]
        external_logger.propagate = False
        external_logger.disabled = True

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            app.state.service = service
            yield
            return
        repository = PostgresRepository(config)
        provider = OpenAIProvider(config) if config.provider == "openai" else FakeProvider()
        try:
            await repository.ready()
            app.state.service = RagService(repository, provider, provider)
            yield
        finally:
            try:
                if isinstance(provider, OpenAIProvider):
                    await provider.close()
            finally:
                await repository.close()

    app = FastAPI(title="opspilot-ai", version="0.1.0", lifespan=lifespan, debug=False)
    # Test injection does not require an ASGI lifespan manager dependency.
    if service is not None:
        app.state.service = service
    app.add_middleware(RequestMiddleware, timeout=config.request_timeout_seconds)
    token_hashes = [
        (hashlib.sha256(token.encode()).digest(), tenant)
        for token, tenant in config.tenant_tokens.items()
    ]

    def tenant(
        authorization: Annotated[str | None, Header(max_length=150)] = None,
    ) -> UUID:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="unauthorized")
        candidate = hashlib.sha256(authorization[7:].encode()).digest()
        for expected, tenant_id in token_hashes:
            if hmac.compare_digest(candidate, expected):
                return tenant_id
        raise HTTPException(status_code=401, detail="unauthorized")

    def rag(request: Request) -> RagService:
        return cast(RagService, request.app.state.service)

    @app.exception_handler(AppError)
    async def application_error(request: Request, error: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={"error": error.code, "request_id": request_id.get()},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default includes rejected input. Never echo bodies or tokens.
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_input", "request_id": request_id.get()},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": "unauthorized", "request_id": request_id.get()},
        )

    @app.post("/v1/documents", response_model=DocumentOutput, status_code=201)
    async def documents(
        payload: DocumentInput,
        tenant_id: Annotated[UUID, Depends(tenant)],
        use_case: Annotated[RagService, Depends(rag)],
    ) -> DocumentOutput:
        document, chunk_ids = await use_case.ingest(
            tenant_id,
            payload.content,
            Metadata(payload.metadata.title, payload.metadata.source, tuple(payload.metadata.tags)),
        )
        return DocumentOutput(
            document_id=document.id, chunk_ids=chunk_ids, request_id=request_id.get()
        )

    @app.post("/v1/query", response_model=QueryOutput)
    async def query(
        payload: QueryInput,
        tenant_id: Annotated[UUID, Depends(tenant)],
        use_case: Annotated[RagService, Depends(rag)],
    ) -> QueryOutput:
        result = await use_case.query(tenant_id, payload.question, payload.top_k)
        return QueryOutput(
            answer=result.answer,
            citations=[citation(chunk) for chunk in result.citations],
            retrieved_chunks=[
                RetrievedChunk(**citation(hit.chunk).model_dump(), score=hit.score)
                for hit in result.retrieved
            ],
            request_id=request_id.get(),
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(use_case: Annotated[RagService, Depends(rag)]) -> dict[str, str]:
        await use_case.repository.ready()
        return {"status": "ready"}

    return app
