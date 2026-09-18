# docker-agent-sandbox

A Docker stack that runs AI coding agents in disposable containers, reachable over an HTTP API so
that automation such as n8n can submit jobs without ever touching a Docker socket itself.

> **Status: no dispatcher yet.** Phases 1–3 of [`docs/PLAN.md`](docs/PLAN.md) are done — the
> repository skeleton, the compose stack with both Docker backends, and the agent image. The
> dispatcher is not implemented, so jobs have to be started by hand for now; see
> [`docs/AGENT_CONTRACT.md`](docs/AGENT_CONTRACT.md).

## Idea in one picture

```
      n8n  ──HTTP (bearer token)──▶  agent-api   [long-running, small]
                                         │
                                         │ DOCKER_HOST=tcp://docker-proxy:2375
                                         ▼
                                   docker-proxy  [internal network only]
                                         │
                  ┌──────────────────────┴──────────────────────┐
            profile "dind"                                profile "host"
                  ▼                                              ▼
         rootless dind daemon                       /var/run/docker.sock
                  │                                              │
                  └────────▶   agent container   ◀───────────────┘
                              [one per job, removed afterwards]
```

The dispatcher is the only component with Docker access. n8n speaks plain HTTP, so it needs no
privileges of its own, and agents stay ephemeral: one container per job, removed when the job ends.

## Why a dispatcher instead of SSH

Connecting n8n over SSH into a long-lived agent container was the obvious first idea, but it leaves
state shared between jobs, adds key management, and offers no job lifecycle — no status, no logs,
no timeout, no cancellation. The dispatcher provides all of those and keeps the agents disposable.

## Running the backend

```sh
cp .env.example .env     # pick COMPOSE_PROFILES, fill in the tokens later
docker compose up -d
```

The profile comes from `COMPOSE_PROFILES` in `.env`; the compose file needs no flags of its own.
Both profiles publish the socket proxy under the network alias `docker-proxy` on the internal
`agent_net`, so the dispatcher's `DOCKER_HOST=tcp://docker-proxy:2375` is the same either way.

Check that the backend answers:

```sh
docker run --rm --network agentsandbox_agent_net \
  -e DOCKER_HOST=tcp://docker-proxy:2375 docker:cli docker version
```

### Host requirement for the `dind` profile

The `dind` profile runs `docker:dind-rootless`, whose `rootlesskit` needs to create an unprivileged
user namespace. Ubuntu 23.10 and newer block that by default
(`kernel.apparmor_restrict_unprivileged_userns=1`), and the container then dies at startup with:

```
[rootlesskit:parent] error: failed to start the child: fork/exec /proc/self/exe: operation not permitted
```

`privileged: true` does **not** help here, because the restriction applies to the unprivileged
`rootless` user inside the container rather than to the container itself. Grant the exception to
`rootlesskit` alone, once per host:

```sh
cat <<'EOT' | sudo tee /etc/apparmor.d/usr.local.bin.rootlesskit
abi <abi/4.0>,
include <tunables/global>

/usr/local/bin/rootlesskit flags=(unconfined) {
  userns,

  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/usr.local.bin.rootlesskit>
}
EOT
sudo systemctl restart apparmor.service
```

Turning the restriction off globally (`sysctl kernel.apparmor_restrict_unprivileged_userns=0`) also
works but lifts it for every unprivileged process on the host, so the profile above is preferred.
The `host` profile needs none of this.

The two settings this needs *inside* the container — `/dev/net/tun` for slirp4netns and
`systempaths=unconfined` so the daemon's own containers can mount `/proc` — are already in the
compose file and need nothing from you.

### Switching profiles

`docker compose down` only removes services belonging to the profile currently selected, so changing
`COMPOSE_PROFILES` in a running stack leaves the old profile's proxy behind — and both answer to the
`docker-proxy` alias. Take the stack down before you switch:

```sh
docker compose down          # with the OLD COMPOSE_PROFILES still set
# ...edit .env...
docker compose up -d
```

## The agent image

```sh
docker build -t docker-agent-sandbox/agent:local agent/
```

A generic image: Node, git, the Docker CLI and `@devcontainers/cli`, running as a non-root user. No
agent CLI is baked in yet — which one to use is still open (`docs/PLAN.md` §6), and the image is
structured so that adding one is a build target rather than a rewrite.

Its interface with the dispatcher — the environment it expects and the single result line it prints
— is [`docs/AGENT_CONTRACT.md`](docs/AGENT_CONTRACT.md), which also shows how to run a job by hand
while the dispatcher does not exist.

## Security boundary

Read [`docs/PLAN.md`](docs/PLAN.md#security-boundary) before running this with the `host` profile.
In short: anything allowed to create containers on the host daemon is effectively root on the host,
socket proxy or not. The `dind` profile exists so that the blast radius stays inside a dedicated
daemon.

## License

GPL-3.0 — see [LICENSE](LICENSE).
