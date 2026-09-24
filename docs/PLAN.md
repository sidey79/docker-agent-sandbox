# Plan: docker-agent-sandbox

Design and phase plan for running AI coding agents in disposable Docker containers, driven by n8n
over HTTP. All six phases are implemented; what remains is the open points in §6.

## 1. Goal

Run AI coding agents on demand, with three properties that pull against each other:

- **Reachable from n8n** without giving n8n any Docker privileges.
- **Disposable** — one fresh container per job, no state carried between jobs.
- **Contained** — a misbehaving or prompt-injected agent must not be able to take the host with it.

A small long-running dispatcher resolves the tension. It is the only component with Docker access.
n8n submits jobs over HTTP; the dispatcher creates and reaps the agent containers.

## 2. Architecture

```
      n8n  ──HTTP (bearer token)──▶  agent-api   [long-running, small]
 (network_backend_net)                   │
                                         │ DOCKER_HOST=tcp://docker-proxy:2375
                                         ▼
                                   docker-proxy  [agent_net, internal: true]
                                         │
                  ┌──────────────────────┴──────────────────────┐
            profile "dind"                                profile "host"
                  ▼                                              ▼
         rootless dind daemon                       /var/run/docker.sock
                  │                                              │
                  └────────▶   agent container   ◀───────────────┘
                              [one per job, --rm]
```

`agent-api` sits on two networks: `network_backend_net` so n8n (and optionally Caddy/Authelia) can
reach it, and the internal `agent_net` to reach the proxy. The proxy gets no port mapping at all —
it is reachable only from inside `agent_net`.

### Profiles

`COMPOSE_PROFILES` in `.env` selects the backend, mirroring the pattern already used in
`docker-llm`:

| Profile | Backend | Use when |
| --- | --- | --- |
| `dind` | dedicated rootless `docker:dind-rootless` daemon | default; n8n-triggered jobs |
| `host` | host `/var/run/docker.sock` | you deliberately want agents to reach the existing stacks |

Both profiles ship their own proxy service (`docker-proxy-dind` / `docker-proxy-host`) sharing the
network alias `docker-proxy`, so the dispatcher's `DOCKER_HOST` never changes. Only one profile is
ever active, so the alias cannot collide.

For the dind profile, the daemon's unix socket is shared with its proxy through a volume rather than
exposing TCP, which keeps the proxy image working unmodified. Two implementation details make that
work:

- The service runs an explicit `dockerd --host=unix://…` command. That suppresses the entrypoint's
  default `--host=tcp://0.0.0.0:2375`, so the daemon has no TCP listener at all and containers
  started *inside* it have no network path back to it.
- The socket lives at the root of a volume mounted on `/home/rootless`, because rootlesskit does
  `--copy-up=/run`: anything mounted under `/run` is shadowed by a tmpfs and never reaches the
  proxy. The daemon's data root is a second, nested volume, so the shared volume stays empty apart
  from the socket.

## 3. Security boundary

The socket proxy is worth having, but it is important to be precise about what it buys.

Granting `CONTAINERS=1`, `POST=1`, `BUILD=1`, `VOLUMES=1` and `EXEC=1` is close to full root on the
target daemon: whoever may create a container may create a privileged one, or one that bind-mounts
`/`. Against the `host` daemon that means the host. So the proxy protects against *accidents* — an
agent tearing down the FHEM stack because it misread an instruction — but not against a
compromised or prompt-injected agent.

The `dind` profile is where the boundary actually holds: the blast radius ends at the dedicated
daemon. Rootless dind is chosen over rootful because the daemon then runs unprivileged inside a user
namespace, which makes escaping it substantially harder. It is still not a hypervisor; a hard
boundary would need a VM. That is a deliberate, documented trade-off, not an oversight.

Rules that follow from this and must hold in the implementation:

- The job payload never influences the container spec — no image, mounts, env or capabilities from
  the request. The dispatcher builds the spec from its own configuration and an image allowlist.
- Agent credentials come from the dispatcher environment, never from a request.
- The dispatcher requires a bearer token and is never exposed directly to the internet.
- Agent containers run with `no-new-privileges`, dropped capabilities, and memory, CPU and PID limits.

## 4. Job contract

| Endpoint | Purpose |
| --- | --- |
| `POST /jobs` | submit a job → `202` + `jobId`. Body: task text, optional repo/branch, optional callback URL |
| `GET /jobs/{id}` | status (`queued`/`running`/`succeeded`/`failed`/`timeout`), exit code, result |
| `GET /jobs/{id}/logs` | collected container logs |
| `DELETE /jobs/{id}` | cancel a running job |
| `GET /healthz` | liveness |

n8n can either poll `GET /jobs/{id}` or pass a callback URL and wait on a webhook. For long agent
runs the callback is the more robust of the two.

Each job gets its own Docker volume, created and removed through the Docker API. That works
identically under both profiles — unlike a bind mount, whose path would mean different things to the
host daemon and to the dind daemon. This is the detail most likely to cause a confusing bug, so it
is settled by design rather than by configuration.

The agent reports its result on stdout using a marker line; the dispatcher stores full logs plus the
extracted result. This avoids having to read a volume back out, which would itself require starting
a container.

## 5. Phases

**Phase 1 — repository scaffolding.** *(done)*
House conventions from the other `docker-*` repos: `renovate.json`, `.dclintrc`, dclint workflow,
GPL-3.0 license, README. Plus `.gitignore` and `.env.example`, which the sibling repos do not need —
this repo is public and its `.env` will hold API tokens. Renovate picks the repo up automatically
through `autodiscover: true`.

**Phase 2 — proxy and dind.** *(done)*
Both profiles, proxy hardened (`read_only`, `no-new-privileges`; `privileged` is not needed for the
proxy itself). *Acceptance:* `docker version` from a throwaway container against
`tcp://docker-proxy:2375` succeeds under both profiles, and the host daemon is unreachable under
`dind`.

Rootless dind turned out to need three things the design did not anticipate. Two are settings on the
service, one is a prerequisite on the host, and all three were found by running it rather than by
reading about it:

- **`/dev/net/tun`.** The rootless daemon gets its network from slirp4netns, which needs to create a
  tap interface. Without the device it fails with a bare `open: No such file or directory`.
- **`systempaths=unconfined`.** Docker masks parts of `/proc` in every container. Inside a user
  namespace, procfs may only be mounted if nothing masked would be revealed, so the daemon's own
  containers die at `error mounting "proc" to rootfs`. Unmasking is the narrow fix; `privileged:
  true` would do it too, by also granting everything else. What it costs is small here: reading the
  paths it unmasks needs capabilities the unprivileged `rootless` user does not hold.
- **An AppArmor exception on the host.** Ubuntu 23.10 and newer set
  `kernel.apparmor_restrict_unprivileged_userns=1`, which stops rootlesskit from creating its user
  namespace at all. `privileged: true` does not lift this one either, because the restriction
  applies to the unprivileged `rootless` user inside the container rather than to the container. The
  README documents the profile that grants `userns` to `rootlesskit` alone.

Rootless was kept rather than falling back to rootful dind, because the user namespace is the whole
reason §3 can claim a boundary at all.

One operational wrinkle: `docker compose down` only removes services of the currently selected
profile. Switching `COMPOSE_PROFILES` therefore leaves the previous profile's proxy running, and
both claim the `docker-proxy` alias. Take the stack down before switching, or the dispatcher may end
up talking to the daemon it was moved away from.

**Phase 3 — agent image.** *(done)*
Generic Node-based image with `@devcontainers/cli`, a non-root user and an entrypoint contract
(`TASK`, `JOB_ID`, result marker), written down in [`AGENT_CONTRACT.md`](AGENT_CONTRACT.md).
Deliberately no specific agent CLI baked in; that is a later choice. *Acceptance:* a manually started
agent container can bring up a devcontainer through the proxy.

One thing the design did not spell out: because the agent talks to a *remote* daemon, the workspace
cannot be handed to `devcontainer up` as a path — the daemon would resolve it on its own filesystem.
The entrypoint therefore rewrites the effective `devcontainer.json` to mount the job volume by name.
Same reasoning as §4, one layer further in.

**Phase 4 — dispatcher.** *(done)*
FastAPI service implementing the contract above: bearer auth, SQLite job store, concurrency limit,
hard timeout, fixed container spec, callback webhook. Python needs no Node tooling here — it only
talks to the Docker API; all agent tooling lives in the agent image. The HTTP surface is written
down in [`API.md`](API.md). *Acceptance:* `curl` submits a job, the container appears and is
removed, logs and status are retrievable, a timeout kills the container.

Three things the design did not anticipate, all of them consequences of decisions made earlier:

- **The agent container must not carry the job label.** The agent removes everything labelled
  `agent-sandbox.job=<id>` on its way out, which is how devcontainers get reaped. Give the agent
  container that same label and it deletes itself mid-run — which is exactly what happened the first
  time the dispatcher created one. The agent container gets `agent-sandbox.agent=<id>` instead and
  the dispatcher sweeps both.
- **Logs must be collected before the container is removed.** Obvious in hindsight, and the timeout
  path is where it bites: the run that most needs its output is the one that gets torn down.
- **Cancelling must not remove the container itself.** Doing so races the worker's polling loop,
  which then sees the container vanish and reports a plain failure instead of a cancellation, taking
  the logs with it. Cancel sets a flag; the loop tears down in order.

One thing that follows from the proxy's allowlist: `ALLOW_STOP` and `ALLOW_RESTARTS` stay `0`, so the
dispatcher cannot stop or kill a container. It ends one with `remove(force=True)`, which the
allowlist does permit, and which is all a disposable agent ever needs.

**Phase 5 — n8n integration.** *(done)*
Example workflows in [`examples/`](../examples), reachability over `network_backend_net` verified end
to end. Both shapes §4 offers are covered: a callback workflow that returns a `jobId` at once and
reacts to the completion webhook, and a polling workflow that answers synchronously.

Both carry an HTTP Header Auth credential rather than the token itself, so the files can be
committed. The callback URL is the one value that cannot be guessed for someone else's install — it
has to name this n8n as the *dispatcher* sees it — so it sits in a `Config` node at the front rather
than being buried in an HTTP node.

**Phase 6 — documentation and Renovate.** *(done)*
Operating instructions and the first Renovate PR as proof the update path works.

The instructions outgrew the README and became [`OPERATIONS.md`](OPERATIONS.md): prerequisites,
first run, a full configuration reference, day-to-day commands, what to watch, maintenance, and a
troubleshooting section built from the failures this build actually hit. The README keeps the
quickstart and points at it.

Writing the configuration reference turned up a gap worth noting: several settings existed in
`config.py` but were never passed through the compose file, so they were only reachable by editing
`docker-compose.yml`. Documenting them honestly would have meant writing that down as a limitation;
wiring them up was the smaller change, and now every documented variable can be set from `.env`.

Renovate needed no proof in the end — it had already opened and merged #5 on its own while phase 4
was being built.

## 6. Open points

- **Egress filtering.** Agents currently reach the internet unrestricted. An allowlisting HTTP proxy
  in front of them is a possible later stage; deliberately out of scope for the first round.
- **Which agent CLI.** Partly settled: the image has a `claude` build target with Claude Code, and
  the base target stays generic. Codex or Gemini CLI would be further targets on the same pattern.
  Adding one surfaced a choice the design had not made — an agent CLI that brings its own tooling has
  no use for a devcontainer — so `AGENT_CMD_LOCATION` now decides whether the command runs inside the
  devcontainer or in the agent container itself.
- **Job store durability.** SQLite in a volume is the plan. If job history turns out not to matter,
  in-memory would be simpler.
- **Egress from agent containers.** Settled structurally in phase 4 but not restricted: under `host`
  agents run on `agent_run_net`, which carries the `docker-proxy` alias and, unlike `agent_net`, is
  not internal; under `dind` they run inside that daemon and use its egress. A second network per
  container was avoided because connecting one needs `NETWORKS=1` on the proxy, which would also let
  the dispatcher delete networks. What those agents may *reach* is still the open question above.

## 7. Decisions taken after the first draft

**How the agent reaches the daemon.** Under `host` the agent gets `DOCKER_HOST=tcp://docker-proxy:2375`
over `agent_net`, which is what phase 3 was verified against. Under `dind` that alias does not
resolve, because the agent container runs *inside* the dind daemon rather than next to it. The
dispatcher will therefore bind-mount the daemon's own socket into the agent:

```
host:  DOCKER_HOST=tcp://docker-proxy:2375
dind:  DOCKER_HOST=unix:///var/run/docker.sock
       -v /home/rootless/docker.sock:/var/run/docker.sock
       --group-add 2375
```

The `--group-add` is not decoration. Seen from inside the agent container the socket is
`srw-rw---- root docker`, so the unprivileged `node` user gets `permission denied` and the
devcontainer CLI fails on its very first `docker ps`. 2375 is the gid of the `docker` group baked
into the `docker:dind` image. Running the agent as root would also work, but there is no reason to
give up the non-root user to solve a group-membership problem.

This hands the agent unrestricted access to the dind daemon. That is deliberate: it is exactly the
blast radius §3 already accepts, and the alternatives are worse. Running a second socket proxy
*inside* dind adds a service with its own lifecycle for a boundary that only separates the agent
from a daemon it is already meant to own; moving the devcontainer CLI into the dispatcher would
contradict §5 phase 4 and discard most of the agent image.

The asymmetry is the point. Under `host` the proxy is doing real work, because the daemon on the
other side is the host's. Under `dind` there is nothing left to protect that the daemon boundary
does not already protect.

Both shapes have been run by hand: a job under `host` through the proxy, and a job under `dind` with
the socket mounted in. In both, the agent clones a repository, brings up a devcontainer, runs its
command inside it and reports a result. Phase 4 is wiring, not discovery.
