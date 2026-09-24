# docker-agent-sandbox

A Docker stack that runs AI coding agents in disposable containers, reachable over an HTTP API so
that automation such as n8n can submit jobs without ever touching a Docker socket itself.

> **Status: complete.** All six phases of [`docs/PLAN.md`](docs/PLAN.md) are done — the compose
> stack with both Docker backends, the agent image, the dispatcher, n8n example workflows and the
> operating documentation. What remains is the open points in §6, egress filtering chief among them.

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

Running it for real takes a few things this quickstart skips: the host prerequisite for `dind`,
switching profiles without leaving a stray proxy behind, loading the agent image into the dind
daemon, backups, pruning, and what each error message actually means.
[`docs/OPERATIONS.md`](docs/OPERATIONS.md) is the manual.

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

## The dispatcher

Built and started with the rest of the stack. It listens on port 8080 with no published port, so it
is reachable by name from `network_backend_net` and nowhere else.

```sh
curl -X POST http://agent-api:8080/jobs \
  -H "Authorization: Bearer $AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task": "Say hello.", "repoUrl": "https://github.com/example/repo.git"}'
```

Submit, poll, fetch logs, cancel, or hand it a `callbackUrl` and wait for the webhook — the full
contract is [`docs/API.md`](docs/API.md).

Two things about it are deliberate. It is the only component with Docker access, which is what lets
n8n stay unprivileged. And it builds every container spec from its own configuration: a job supplies
task text and at most a repository, never an image, a mount, an environment variable or a command.
`AGENT_CMD` — the command that actually runs inside the devcontainer — comes from `.env`, because a
request that could choose it would be remote code execution with extra steps.

## Driving it from n8n

Two ready-made workflows live in [`examples/`](examples/): one that submits a job and reacts to the
completion webhook, and one that polls until the job is done. Import the one that fits and give it an
HTTP Header Auth credential holding the bearer token — [`examples/README.md`](examples/README.md) has
the details.

n8n needs nothing but `network_backend_net` in common with the dispatcher. No Docker socket, no
mount, no privileges of its own — that is the entire reason this stack is shaped the way it is.

## Security boundary

Read [`docs/PLAN.md`](docs/PLAN.md#security-boundary) before running this with the `host` profile.
In short: anything allowed to create containers on the host daemon is effectively root on the host,
socket proxy or not. The `dind` profile exists so that the blast radius stays inside a dedicated
daemon.

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Install, configure, run, watch, maintain, repair. The manual. |
| [`docs/API.md`](docs/API.md) | The HTTP contract n8n talks to. |
| [`docs/AGENT_CONTRACT.md`](docs/AGENT_CONTRACT.md) | What goes into an agent container and what comes back out. |
| [`docs/PLAN.md`](docs/PLAN.md) | The design, why it is shaped this way, and what is still open. |
| [`examples/README.md`](examples/README.md) | The two n8n workflows and how to import them. |

## License

GPL-3.0 — see [LICENSE](LICENSE).
