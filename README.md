# docker-agent-sandbox

A Docker stack that runs AI coding agents in disposable containers, reachable over an HTTP API so
that automation such as n8n can submit jobs without ever touching a Docker socket itself.

> **Status: scaffolding only.** Phase 1 of [`docs/PLAN.md`](docs/PLAN.md) is done — the repository
> skeleton, house conventions and the design. The compose stack, the agent image and the dispatcher
> are not implemented yet.

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

## Security boundary

Read [`docs/PLAN.md`](docs/PLAN.md#security-boundary) before running this with the `host` profile.
In short: anything allowed to create containers on the host daemon is effectively root on the host,
socket proxy or not. The `dind` profile exists so that the blast radius stays inside a dedicated
daemon.

## License

GPL-3.0 — see [LICENSE](LICENSE).
