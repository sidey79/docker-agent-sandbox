"""Runs jobs as agent containers on the daemon behind the socket proxy.

The container spec is built here and only here, from configuration. A job
contributes its task text and an optional repository, never a mount, an image,
an environment variable or a capability — docs/PLAN.md §3.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any

import docker
import httpx
from docker.errors import DockerException, NotFound

from .config import Settings
from .store import JobStore

log = logging.getLogger("dispatcher.runner")

# The agent labels every devcontainer it creates with JOB_LABEL and removes
# everything carrying it on the way out. So the agent container itself must NOT
# carry that label, or it deletes itself mid-run. It gets AGENT_LABEL instead,
# and the dispatcher sweeps both.
JOB_LABEL = "agent-sandbox.job"
AGENT_LABEL = "agent-sandbox.agent"
RESULT_MARKER = "::agent-result::"
_POLL_INTERVAL_SECONDS = 2.0

# Only plain http(s) remotes. Without this a ref like `--upload-pack=…` would
# reach `git clone` as an option rather than as a URL.
_REPO_URL_RE = re.compile(r"^https?://[^\s]+$")
_REPO_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate_repo(url: str | None, ref: str | None) -> str | None:
    """Return an error message, or None when the pair is acceptable."""
    if url is not None and not _REPO_URL_RE.match(url):
        return "repoUrl must be a plain http:// or https:// URL"
    if ref is not None:
        if not _REPO_REF_RE.match(ref) or ".." in ref:
            return "repoRef must be a plain branch or tag name"
        if url is None:
            return "repoRef is meaningless without repoUrl"
    return None


def extract_result(logs: str) -> dict[str, Any] | None:
    """Pull the agent's result line out of its output.

    The last marker wins: a job that somehow emits two has failed after
    reporting, and the later line is the one that knows about it.
    """
    found = None
    for line in logs.splitlines():
        stripped = line.strip()
        if stripped.startswith(RESULT_MARKER):
            try:
                found = json.loads(stripped[len(RESULT_MARKER) :])
            except json.JSONDecodeError:
                found = {"raw": stripped[len(RESULT_MARKER) :]}
    return found


class JobRunner:
    def __init__(self, settings: Settings, store: JobStore) -> None:
        self._settings = settings
        self._store = store
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        # Jobs asked to stop. A worker consults this instead of guessing why a
        # container disappeared underneath it.
        self._cancelled: set[str] = set()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        orphaned = self._store.fail_orphans("dispatcher restarted while this job was running")
        if orphaned:
            log.warning("marked %d orphaned job(s) as failed", orphaned)
        for job_id in self._store.queued_ids():
            self._queue.put_nowait(job_id)
        self._workers = [
            asyncio.create_task(self._worker(index))
            for index in range(self._settings.agent_max_concurrency)
        ]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit(self, job_id: str) -> None:
        await self._queue.put(job_id)

    async def cancel(self, job_id: str) -> None:
        """Ask a job to stop. Safe to call whether it is queued or running.

        Only the flag is set. Removing the container here instead would race the
        polling loop, which would then see it vanish and report a plain failure
        rather than a cancellation — and the logs would be gone with it. The
        loop notices within one poll interval and tears down in order.
        """
        self._cancelled.add(job_id)

    # --- worker ------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker %d: job %s failed unexpectedly", index, job_id)
                self._store.update(
                    job_id, status="failed", error="dispatcher error", finished_at=time.time()
                )
            finally:
                self._queue.task_done()

    async def _run(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None or job["status"] != "queued":
            return
        if job_id in self._cancelled:
            self._cancelled.discard(job_id)
            self._store.update(job_id, status="cancelled", finished_at=time.time())
            return

        self._store.update(job_id, status="running", started_at=time.time())
        outcome = await asyncio.to_thread(self._execute, job)
        self._cancelled.discard(job_id)
        self._store.update(job_id, finished_at=time.time(), **outcome)

        if job["callback_url"]:
            await self._post_callback(job_id, job["callback_url"])

    # --- docker ------------------------------------------------------------

    def _execute(self, job: dict[str, Any]) -> dict[str, Any]:
        """Create, run, reap. Runs in a worker thread; docker-py is blocking."""
        job_id = job["id"]
        volume_name = f"agentjob-{job_id}"
        client = docker.DockerClient(base_url=self._settings.docker_host)
        container = None
        try:
            client.volumes.create(name=volume_name)
            self._store.update(job_id, volume_name=volume_name)

            container = client.containers.create(**self._container_spec(job, volume_name))
            self._store.update(job_id, container_id=container.id)
            container.start()

            exit_code, outcome = self._wait(container, job_id)
            # Before the sweep, not after: a container that has been removed has
            # taken its logs with it, and the timeout case is exactly the one
            # where those logs matter most.
            logs = _safe_logs(container)
            result = extract_result(logs)

            if outcome == "cancelled":
                return {"status": "cancelled", "logs": logs, "result": result}
            if outcome == "timeout":
                return {
                    "status": "timeout",
                    "logs": logs,
                    "result": result,
                    "error": f"killed after {self._settings.agent_job_timeout_seconds}s",
                }
            if outcome == "gone":
                return {
                    "status": "failed",
                    "logs": logs,
                    "result": result,
                    "error": "the agent container disappeared before it finished",
                }
            return {
                "status": "succeeded" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "logs": logs,
                "result": result,
            }
        except DockerException as exc:
            log.exception("job %s: docker error", job_id)
            return {"status": "failed", "error": f"docker error: {exc}"}
        finally:
            self._sweep_job_containers(job_id, client=client)
            _remove_volume(client, volume_name)
            client.close()

    def _wait(self, container: Any, job_id: str) -> tuple[int | None, str]:
        """Poll until the container stops, we are cancelled, or time runs out.

        Polling rather than `container.wait(timeout=…)` because the latter
        signals a deadline by raising a transport error, which is
        indistinguishable from the proxy having gone away.

        This never removes the container — the caller still needs its logs. A
        container that is still running when this returns is killed by the sweep
        that follows, which uses `remove(force=True)` because the proxy's
        allowlist deliberately withholds stop and kill.
        """
        deadline = time.monotonic() + self._settings.agent_job_timeout_seconds
        while True:
            try:
                container.reload()
            except NotFound:
                # Removed underneath us — a cancel, or a sweep from elsewhere.
                return None, "gone"
            state = container.attrs.get("State", {})
            if state.get("Status") not in ("running", "created", "restarting"):
                return state.get("ExitCode"), "finished"
            if job_id in self._cancelled:
                return None, "cancelled"
            if time.monotonic() >= deadline:
                return None, "timeout"
            time.sleep(_POLL_INTERVAL_SECONDS)

    def _container_spec(self, job: dict[str, Any], volume_name: str) -> dict[str, Any]:
        settings = self._settings
        environment = {
            "JOB_ID": job["id"],
            "TASK": job["task"],
            "WORKSPACE_VOLUME": volume_name,
        }
        if job["repo_url"]:
            environment["REPO_URL"] = job["repo_url"]
        if job["repo_ref"]:
            environment["REPO_REF"] = job["repo_ref"]
        if settings.agent_cmd:
            environment["AGENT_CMD"] = settings.agent_cmd
        if settings.agent_devcontainer_image:
            environment["AGENT_DEVCONTAINER_IMAGE"] = settings.agent_devcontainer_image
        environment.update(_credentials(settings))

        spec: dict[str, Any] = {
            "image": settings.agent_image,
            "environment": environment,
            "volumes": {volume_name: {"bind": "/workspace", "mode": "rw"}},
            "labels": {AGENT_LABEL: job["id"]},
            "detach": True,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
            "mem_limit": settings.agent_mem_limit,
            "nano_cpus": int(settings.agent_cpus * 1_000_000_000),
            "pids_limit": settings.agent_pids_limit,
        }

        if settings.agent_backend == "dind":
            # Inside the dind daemon the proxy's alias does not resolve, so the
            # daemon's own socket is handed over instead — docs/PLAN.md §7.
            spec["environment"]["DOCKER_HOST"] = "unix:///var/run/docker.sock"
            spec["volumes"][settings.agent_dind_socket] = {
                "bind": "/var/run/docker.sock",
                "mode": "rw",
            }
            spec["group_add"] = [str(settings.agent_dind_docker_gid)]
        else:
            spec["environment"]["DOCKER_HOST"] = settings.docker_host
            spec["network"] = settings.agent_network

        return spec

    def _sweep_job_containers(self, job_id: str, client: Any | None = None) -> None:
        """Remove this job's agent container and any devcontainer it created.

        The agent removes the devcontainers itself on the way out, but a killed
        agent never runs its trap, so the dispatcher repeats the sweep rather
        than trusting it.
        """
        owned = client is None
        if client is None:
            try:
                client = docker.DockerClient(base_url=self._settings.docker_host)
            except DockerException:
                log.exception("job %s: cannot reach docker to sweep", job_id)
                return
        try:
            for label in (JOB_LABEL, AGENT_LABEL):
                for container in client.containers.list(
                    all=True, filters={"label": f"{label}={job_id}"}
                ):
                    _force_remove(container)
        except DockerException:
            log.exception("job %s: sweep failed", job_id)
        finally:
            if owned:
                client.close()

    # --- callback ----------------------------------------------------------

    async def _post_callback(self, job_id: str, url: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            return
        payload = {
            "jobId": job_id,
            "status": job["status"],
            "exitCode": job["exit_code"],
            "result": job["result"],
        }
        try:
            async with httpx.AsyncClient(timeout=self._settings.callback_timeout_seconds) as http:
                await http.post(url, json=payload)
        except httpx.HTTPError as exc:
            # The job itself is finished and its status is stored; a callback
            # that cannot be delivered must not change that.
            log.warning("job %s: callback to %s failed: %s", job_id, url, exc)


def _credentials(settings: Settings) -> dict[str, str]:
    import os

    return {
        name: os.environ[name] for name in settings.credential_names if os.environ.get(name)
    }


def _safe_logs(container: Any) -> str:
    try:
        return container.logs(stdout=True, stderr=True).decode("utf-8", "replace")
    except DockerException:
        log.warning("could not read logs of container %s", getattr(container, "id", "?"))
        return ""


def _force_remove(container: Any) -> None:
    try:
        container.remove(force=True)
    except NotFound:
        pass
    except DockerException:
        log.exception("could not remove container %s", getattr(container, "id", "?"))


def _remove_volume(client: Any, name: str) -> None:
    try:
        client.volumes.get(name).remove(force=True)
    except NotFound:
        pass
    except DockerException:
        log.exception("could not remove volume %s", name)
