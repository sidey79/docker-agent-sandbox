"""The dispatcher. The only component in the stack with Docker access.

n8n speaks plain HTTP to this service and never sees a Docker socket, which is
the whole point of the design — docs/PLAN.md §1.
"""

import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import get_settings
from .runner import JobRunner, validate_repo
from .store import DONE_STATES, JobStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("dispatcher")


class JobRequest(BaseModel):
    task: str = Field(min_length=1, max_length=64_000)
    repo_url: str | None = Field(default=None, alias="repoUrl", max_length=2048)
    repo_ref: str | None = Field(default=None, alias="repoRef", max_length=255)
    callback_url: str | None = Field(default=None, alias="callbackUrl", max_length=2048)

    model_config = {"populate_by_name": True}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.store = JobStore(settings.db_path)
    app.state.runner = JobRunner(settings, app.state.store)
    await app.state.runner.start()
    log.info(
        "dispatcher ready: backend=%s image=%s concurrency=%d timeout=%ds",
        settings.agent_backend,
        settings.agent_image,
        settings.agent_max_concurrency,
        settings.agent_job_timeout_seconds,
    )
    try:
        yield
    finally:
        await app.state.runner.stop()


app = FastAPI(title="docker-agent-sandbox dispatcher", lifespan=lifespan)


def require_token(request: Request) -> None:
    """Bearer auth on everything but /healthz.

    The dispatcher is never exposed directly to the internet (docs/PLAN.md §3);
    this is the second lock, not the only one.
    """
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    expected = request.app.state.settings.agent_api_token
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_job_or_404(request: Request, job_id: str) -> dict[str, Any]:
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return job


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    """The job as callers see it. Logs are a separate endpoint, not a field."""
    return {
        "jobId": job["id"],
        "status": job["status"],
        "createdAt": job["created_at"],
        "startedAt": job["started_at"],
        "finishedAt": job["finished_at"],
        "exitCode": job["exit_code"],
        "result": job["result"],
        "error": job["error"],
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_token)])
async def submit_job(request: Request, body: JobRequest) -> JSONResponse:
    error = validate_repo(body.repo_url, body.repo_ref)
    if error:
        raise HTTPException(status_code=422, detail=error)

    job_id = uuid.uuid4().hex
    request.app.state.store.create(
        job_id, body.task, body.repo_url, body.repo_ref, body.callback_url
    )
    await request.app.state.runner.submit(job_id)
    log.info("job %s queued", job_id)
    return JSONResponse(status_code=202, content={"jobId": job_id, "status": "queued"})


@app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
async def get_job(request: Request, job_id: str) -> dict[str, Any]:
    return public_job(get_job_or_404(request, job_id))


@app.get("/jobs/{job_id}/logs", dependencies=[Depends(require_token)])
async def get_logs(request: Request, job_id: str) -> PlainTextResponse:
    job = get_job_or_404(request, job_id)
    return PlainTextResponse(job["logs"] or "")


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_token)])
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    job = get_job_or_404(request, job_id)
    if job["status"] in DONE_STATES:
        # Already finished. Saying so is more useful than a 409, because a
        # caller polling and cancelling at once should not have to care who won.
        return public_job(job)
    await request.app.state.runner.cancel(job_id)
    log.info("job %s cancellation requested", job_id)
    return public_job(get_job_or_404(request, job_id))
