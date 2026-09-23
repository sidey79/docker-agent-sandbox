"""Dispatcher configuration.

Every value here comes from the environment. None of it can be influenced by a
job payload — see docs/PLAN.md §3, which is the reason the container spec is
assembled from this module and nothing else.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # --- API ---------------------------------------------------------------
    agent_api_token: str
    db_path: str = "/data/jobs.sqlite3"

    # --- Docker backend ----------------------------------------------------
    # Which profile the stack is running. It decides how an agent container
    # reaches the daemon, which is the one thing that genuinely differs between
    # the two — see docs/PLAN.md §7.
    agent_backend: Literal["host", "dind"] = "dind"
    docker_host: str = "tcp://docker-proxy:2375"

    # Where the dind daemon's socket lives *inside the dind container*, which is
    # the filesystem that daemon resolves bind mounts against.
    agent_dind_socket: str = "/home/rootless/docker.sock"
    # gid of the `docker` group in the docker:dind image. Without it the
    # unprivileged agent user cannot open the socket it just got handed.
    agent_dind_docker_gid: int = 2375

    # Network for agent containers under the `host` backend. It reaches the
    # proxy and has egress, so a job can clone a repository.
    agent_network: str = "agentsandbox_agent_run_net"

    # --- Agent container spec ---------------------------------------------
    agent_image: str = "docker-agent-sandbox/agent:local"
    agent_cmd: str = ""
    agent_devcontainer_image: str = ""

    agent_max_concurrency: int = 2
    agent_job_timeout_seconds: int = 1800
    agent_mem_limit: str = "2g"
    agent_cpus: float = 2.0
    agent_pids_limit: int = 512

    # Credentials handed to agent containers. Named here, valued from this
    # process's own environment; a request can never add to this list.
    agent_credential_env: str = "ANTHROPIC_API_KEY"

    # --- Callbacks ---------------------------------------------------------
    callback_timeout_seconds: float = 15.0

    @property
    def credential_names(self) -> list[str]:
        return [name.strip() for name in self.agent_credential_env.split(",") if name.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
