"""FastAPI control plane for durable asynchronous migration resources."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from vme.adapters.registry import builtin_registry
from vme.errors import (
    ConfigurationError,
    ResourceNotFoundError,
    StateConflictError,
    VMEError,
    redact_text,
)
from vme.server.auth import AuthenticationError, Authenticator, require_role
from vme.server.models import Actor, DesiredState, WorkspaceRole
from vme.server.schemas import (
    ConnectionProfileCreate,
    JobCreate,
    MigrationCreate,
    PlanCreate,
)
from vme.server.secrets import SecretResolver, validate_secret_references
from vme.server.security import (
    EndpointPolicy,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    validate_migration_payload,
)
from vme.server.settings import ServerSettings
from vme.server.store import SQLiteServiceStore, to_public_dict
from vme.server.worker import ServiceWorker
from vme.state.sqlite import SQLiteStateStore


def create_app(
    settings: ServerSettings | None = None,
    *,
    store: SQLiteServiceStore | None = None,
    worker: ServiceWorker | None = None,
) -> FastAPI:
    settings = settings or ServerSettings.from_env()
    settings.validate()
    owns_store = store is None
    store = store or SQLiteServiceStore(settings.state_path)
    resolver = SecretResolver(
        settings.allowed_secret_roots,
        settings.allowed_secret_env_names,
    )
    endpoint_policy = EndpointPolicy(
        allowed_adapters=settings.allowed_adapters,
        allowed_data_roots=settings.allowed_data_roots,
        allowed_endpoints=settings.allowed_endpoints,
        allow_insecure_endpoints=settings.allow_insecure_endpoints,
        allow_embedded_chroma=settings.allow_embedded_chroma,
    )
    worker = worker or ServiceWorker(
        store=store,
        state_path=str(settings.state_path),
        resolver=resolver,
        poll_seconds=settings.worker_poll_seconds,
        lease_seconds=settings.lease_seconds,
        endpoint_policy=endpoint_policy,
    )
    authenticator = Authenticator(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if settings.run_worker:
            task = asyncio.create_task(worker.run_forever(), name="vme-service-worker")
        try:
            yield
        finally:
            worker.request_shutdown()
            if task is not None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(task, timeout=5)
                if not task.done():
                    task.cancel()
            if owns_store:
                store.close()

    app = FastAPI(
        title="Vector Migration Engine",
        version="1.0.0a1",
        description="Capability-aware, durable vector database migrations",
        lifespan=lifespan,
        docs_url="/docs" if settings.expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.expose_docs else None,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=settings.max_request_bytes,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.allowed_hosts),
        www_redirect=False,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.hsts_enabled)
    app.state.store = store
    app.state.settings = settings
    app.state.worker = worker

    def current_actor(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Actor:
        try:
            return authenticator.authenticate(authorization)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    CurrentActor = Annotated[Actor, Depends(current_actor)]

    def operator(actor: CurrentActor) -> Actor:
        try:
            require_role(actor, WorkspaceRole.OPERATOR)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return actor

    def admin(actor: CurrentActor) -> Actor:
        try:
            require_role(actor, WorkspaceRole.ADMIN)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return actor

    Operator = Annotated[Actor, Depends(operator)]
    Admin = Annotated[Actor, Depends(admin)]
    IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key")]
    PageLimit = Annotated[int, Query(ge=1, le=200)]
    PageOffset = Annotated[int, Query(ge=0, le=1_000_000)]

    @app.exception_handler(ResourceNotFoundError)
    async def not_found_handler(_request: Request, error: ResourceNotFoundError) -> JSONResponse:
        return _problem(404, "Resource not found", str(error))

    @app.exception_handler(StateConflictError)
    async def conflict_handler(_request: Request, error: StateConflictError) -> JSONResponse:
        return _problem(409, "State conflict", str(error))

    @app.exception_handler(ConfigurationError)
    async def configuration_handler(_request: Request, error: ConfigurationError) -> JSONResponse:
        return _problem(422, "Invalid configuration", str(error))

    @app.exception_handler(VMEError)
    async def vme_handler(_request: Request, error: VMEError) -> JSONResponse:
        return _problem(400, "Migration error", str(error))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return _problem(422, "Invalid request", "request validation failed")

    @app.exception_handler(Exception)
    async def internal_error_handler(_request: Request, _error: Exception) -> JSONResponse:
        return _problem(500, "Internal server error", "an unexpected error occurred")

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> dict[str, str]:
        try:
            store.list_jobs(settings.workspace_id)
        except Exception as error:
            raise HTTPException(status_code=503, detail="state store is unavailable") from error
        return {"status": "ready"}

    @app.get("/v1/adapters")
    async def adapters(_actor: CurrentActor) -> Mapping[str, Any]:
        available = builtin_registry().available()
        allowed = set(settings.allowed_adapters)
        return {
            kind: [name for name in names if name in allowed] for kind, names in available.items()
        }

    @app.post("/v1/connection-profiles", status_code=201)
    async def create_profile(
        body: ConnectionProfileCreate,
        response: Response,
        actor: Admin,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        request = body.model_dump(mode="json")
        validate_secret_references(body.connection)
        resolver.validate_references(body.connection)
        endpoint_policy.validate_connection(body.adapter, body.connection)
        existing = _existing_id(store, actor, "create-profile", idempotency_key, request)
        if existing:
            return to_public_dict(store.get_profile(actor.workspace_id, existing))
        profile = store.create_profile(
            workspace_id=actor.workspace_id,
            name=body.name,
            adapter=body.adapter,
            role=body.role,
            connection=body.connection,
            actor=actor.subject,
        )
        _record_id(store, actor, "create-profile", idempotency_key, request, profile.id)
        response.headers["Location"] = f"/v1/connection-profiles/{profile.id}"
        return to_public_dict(profile)

    @app.get("/v1/connection-profiles")
    async def list_profiles(
        actor: CurrentActor,
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> list[dict[str, Any]]:
        return [
            to_public_dict(item)
            for item in store.list_profiles(actor.workspace_id, limit=limit, offset=offset)
        ]

    @app.get("/v1/connection-profiles/{profile_id}")
    async def get_profile(profile_id: str, actor: CurrentActor) -> dict[str, Any]:
        return to_public_dict(store.get_profile(actor.workspace_id, profile_id))

    @app.post("/v1/migrations", status_code=201)
    async def create_migration(
        body: MigrationCreate,
        response: Response,
        actor: Operator,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        request = body.model_dump(mode="json")
        validate_migration_payload(request)
        existing = _existing_id(store, actor, "create-migration", idempotency_key, request)
        if existing:
            return to_public_dict(store.get_migration(actor.workspace_id, existing))
        store.get_profile(actor.workspace_id, body.source_profile_id)
        store.get_profile(actor.workspace_id, body.destination_profile_id)
        migration = store.create_migration(
            workspace_id=actor.workspace_id,
            name=body.name,
            specification=request,
            actor=actor.subject,
        )
        _record_id(store, actor, "create-migration", idempotency_key, request, migration.id)
        response.headers["Location"] = f"/v1/migrations/{migration.id}"
        return to_public_dict(migration)

    @app.get("/v1/migrations/{migration_id}")
    async def get_migration(migration_id: str, actor: CurrentActor) -> dict[str, Any]:
        return to_public_dict(store.get_migration(actor.workspace_id, migration_id))

    @app.post("/v1/plans", status_code=202)
    async def create_plan(
        body: PlanCreate,
        response: Response,
        actor: Operator,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        request = body.model_dump(mode="json")
        existing = _existing_id(store, actor, "create-plan", idempotency_key, request)
        if existing:
            return _status_resource(store.get_plan(actor.workspace_id, existing), "plans")
        plan = store.create_plan(
            workspace_id=actor.workspace_id,
            migration_id=body.migration_id,
            actor=actor.subject,
        )
        _record_id(store, actor, "create-plan", idempotency_key, request, plan.id)
        response.headers["Location"] = f"/v1/plans/{plan.id}"
        response.headers["Retry-After"] = "1"
        return _status_resource(plan, "plans")

    @app.get("/v1/plans/{plan_id}")
    async def get_plan(plan_id: str, actor: CurrentActor) -> dict[str, Any]:
        return _status_resource(store.get_plan(actor.workspace_id, plan_id), "plans")

    @app.post("/v1/jobs", status_code=202)
    async def create_job(
        body: JobCreate,
        response: Response,
        actor: Operator,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        request = body.model_dump(mode="json")
        existing = _existing_id(store, actor, "create-job", idempotency_key, request)
        if existing:
            return _job_response(store, actor.workspace_id, existing)
        job = store.create_job(
            workspace_id=actor.workspace_id, plan_id=body.plan_id, actor=actor.subject
        )
        _record_id(store, actor, "create-job", idempotency_key, request, job.id)
        response.headers["Location"] = f"/v1/jobs/{job.id}"
        response.headers["Retry-After"] = "1"
        return _job_response(store, actor.workspace_id, job.id)

    @app.get("/v1/jobs")
    async def list_jobs(
        actor: CurrentActor,
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> list[dict[str, Any]]:
        return [
            _job_response(store, actor.workspace_id, item.id)
            for item in store.list_jobs(actor.workspace_id, limit=limit, offset=offset)
        ]

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, actor: CurrentActor) -> dict[str, Any]:
        return _job_response(store, actor.workspace_id, job_id)

    @app.post("/v1/jobs/{job_id}/stop", status_code=202)
    async def stop_job(job_id: str, actor: Operator) -> dict[str, Any]:
        store.request_job_state(
            workspace_id=actor.workspace_id,
            job_id=job_id,
            desired=DesiredState.STOPPED,
            actor=actor.subject,
        )
        return _job_response(store, actor.workspace_id, job_id)

    @app.post("/v1/jobs/{job_id}/resume", status_code=202)
    async def resume_job(job_id: str, actor: Operator) -> dict[str, Any]:
        store.request_job_state(
            workspace_id=actor.workspace_id,
            job_id=job_id,
            desired=DesiredState.RUNNING,
            actor=actor.subject,
        )
        return _job_response(store, actor.workspace_id, job_id)

    @app.post("/v1/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(job_id: str, actor: Operator) -> dict[str, Any]:
        store.request_job_state(
            workspace_id=actor.workspace_id,
            job_id=job_id,
            desired=DesiredState.CANCELLED,
            actor=actor.subject,
        )
        return _job_response(store, actor.workspace_id, job_id)

    @app.get("/v1/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        actor: CurrentActor,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        store.get_job(actor.workspace_id, job_id)
        events = store.events(
            workspace_id=actor.workspace_id,
            resource_type="job",
            resource_id=job_id,
            after=after,
            limit=limit,
        )
        return {"items": events, "next_after": events[-1]["sequence"] if events else after}

    @app.get("/v1/jobs/{job_id}/report")
    async def job_report(job_id: str, actor: CurrentActor) -> dict[str, Any]:
        job = store.get_job(actor.workspace_id, job_id)
        if job.report is None:
            raise StateConflictError(f"job {job_id} does not have a completed report")
        return job.report

    return app


def _problem(status_code: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": title,
            "status": status_code,
            "detail": redact_text(detail),
        },
    )


def _existing_id(
    store: SQLiteServiceStore,
    actor: Actor,
    route: str,
    key: str,
    request: Mapping[str, Any],
) -> str | None:
    if len(key) < 8 or len(key) > 200:
        raise HTTPException(status_code=400, detail="Idempotency-Key must be 8-200 characters")
    return store.check_idempotency(
        workspace_id=actor.workspace_id, route=route, key=key, request=request
    )


def _record_id(
    store: SQLiteServiceStore,
    actor: Actor,
    route: str,
    key: str,
    request: Mapping[str, Any],
    resource_id: str,
) -> None:
    stored = store.record_idempotency(
        workspace_id=actor.workspace_id,
        route=route,
        key=key,
        request=request,
        resource_id=resource_id,
    )
    if stored != resource_id:
        raise StateConflictError("concurrent idempotent request created a different resource")


def _status_resource(value: Any, plural: str) -> dict[str, Any]:
    result = to_public_dict(value)
    result["status_url"] = f"/v1/{plural}/{value.id}"
    return result


def _job_response(store: SQLiteServiceStore, workspace_id: str, job_id: str) -> dict[str, Any]:
    job = store.get_job(workspace_id, job_id)
    result = _status_resource(job, "jobs")
    engine = SQLiteStateStore(store.path)
    try:
        snapshot = engine.get_job(job_id)
    except ResourceNotFoundError:
        snapshot = None
    except StateConflictError:
        snapshot = None
    finally:
        engine.close()
    if snapshot is not None:
        result["progress"] = asdict(snapshot)
        result["progress"]["status"] = snapshot.status.value
    return result
