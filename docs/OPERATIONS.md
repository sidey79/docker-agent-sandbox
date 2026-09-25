# Operating the stack

Everything needed to install, run, watch and repair this. The README is the tour; this is the manual.

- [Prerequisites](#prerequisites)
- [First run](#first-run)
- [Configuration reference](#configuration-reference)
- [Day-to-day](#day-to-day)
- [Watching what it does](#watching-what-it-does)
- [Maintenance](#maintenance)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- Docker with Compose v2. Developed against Docker 25.0.5 and Compose 2.33.1.
- The external network `network_backend_net`. It belongs to another stack — the dispatcher joins it
  so that n8n can reach it by name, and creates nothing itself.
- For the `dind` profile only: a host that lets `rootlesskit` create a user namespace. See
  [AppArmor on Ubuntu 23.10+](#apparmor-on-ubuntu-2310) below.

### AppArmor on Ubuntu 23.10+

The `dind` profile runs `docker:dind-rootless`, whose `rootlesskit` needs an unprivileged user
namespace. Ubuntu 23.10 and newer block that by default
(`kernel.apparmor_restrict_unprivileged_userns=1`) and the container dies at startup with:

```
[rootlesskit:parent] error: failed to start the child: fork/exec /proc/self/exe: operation not permitted
```

`privileged: true` does **not** help, because the restriction applies to the unprivileged `rootless`
user *inside* the container rather than to the container itself. Grant the exception to `rootlesskit`
alone, once per host:

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

Turning the restriction off globally (`sysctl kernel.apparmor_restrict_unprivileged_userns=0`) works
too, but lifts it for every unprivileged process on the host, so the profile above is preferred.

The two settings this also needs *inside* the container — `/dev/net/tun` for slirp4netns and
`systempaths=unconfined` so the daemon's own containers can mount `/proc` — are already in the
compose file and need nothing from you. The `host` profile needs none of this.

## First run

```sh
cp .env.example .env
openssl rand -hex 32          # put this in AGENT_API_TOKEN
$EDITOR .env
```

Build the agent image. It is not on any registry, so it has to exist before the first job:

```sh
docker build -t docker-agent-sandbox/agent:local agent/
docker compose up -d          # builds the dispatcher too
```

### Under `dind`, load the agent image into the daemon

This is the step most likely to be missed. Under the `dind` profile, agent containers are created by
the *dind* daemon, which has its own image store and cannot see the host's. A locally built image
must be copied across:

```sh
docker save docker-agent-sandbox/agent:local \
  | docker run --rm -i --network agentsandbox_agent_net \
      -e DOCKER_HOST=tcp://docker-proxy:2375 docker:cli docker load
```

Repeat after every rebuild of the agent image. Under `host` this does not apply — the dispatcher and
the daemon share an image store.

If `AGENT_IMAGE` names something on a registry instead, the dispatcher pulls it itself and this step
disappears. That is the better arrangement once the image stops changing every day.

### Smoke test

```sh
docker run --rm --network network_backend_net curlimages/curl -s \
  -X POST http://agent-api:8080/jobs \
  -H "Authorization: Bearer $AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"hello"}'
```

A `202` with a `jobId` means the API is up. Poll `GET /jobs/{id}` until it leaves `running`; with no
`AGENT_CMD` configured a job only brings the devcontainer up, which is exactly the check you want
here. The full HTTP contract is [`API.md`](API.md).

## Configuration reference

Everything is read from the environment. `.env` feeds the compose file, which passes a subset to the
dispatcher; the rest of the table is settable but rarely worth changing.

### Set these

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMPOSE_PROFILES` | `dind` | `dind` or `host`. Exactly one — the dispatcher reads it as `AGENT_BACKEND`, and a list of two leaves it undecided. |
| `AGENT_API_TOKEN` | *(required)* | Bearer token. The stack refuses to start without it. |
| `AGENT_IMAGE` | `docker-agent-sandbox/agent:local` | Image used for every job. |
| `AGENT_CMD` | *(empty)* | Command run once per job. This is where an agent CLI goes. Empty means "bring the devcontainer up and stop", which is a useful smoke test. |
| `AGENT_DOCKER_ACCESS` | `auto` | `auto` gives Docker access only to jobs that start a devcontainer; `always` also without one; `never` not at all. |
| `AGENT_WORKSPACE_MOUNT` | *(empty)* | Host directory used as the workspace instead of a per-job volume. See [Working on a host directory](#working-on-a-host-directory). |
| `AGENT_WORKSPACE_MOUNT_MODE` | `rw` | `rw` or `ro` for that mount. |
| `AGENT_CMD_LOCATION` | `devcontainer` | `devcontainer` runs `AGENT_CMD` inside the devcontainer; `agent` runs it in the agent container and starts no devcontainer. See [Running Claude Code as the agent](#running-claude-code-as-the-agent). |
| `ANTHROPIC_API_KEY` | *(empty)* | Handed to agent containers. See [Credentials](#credentials). |

### Limits

Enforced by the dispatcher on every container, never negotiable by a job.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_MAX_CONCURRENCY` | `2` | Jobs running at once. The rest stay `queued`. |
| `AGENT_JOB_TIMEOUT_SECONDS` | `1800` | Hard wall. The container is removed and the job ends as `timeout`. |
| `AGENT_MEM_LIMIT` | `2g` | Per agent container. |
| `AGENT_CPUS` | `2` | Per agent container. |
| `AGENT_PIDS_LIMIT` | `512` | Per agent container. |

### Rarely changed

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_DEVCONTAINER_IMAGE` | `mcr.microsoft.com/devcontainers/base:bookworm` | Used when a repository ships no `devcontainer.json`. |
| `AGENT_NETWORK` | `agentsandbox_agent_run_net` | Network for agent containers under `host` that start a devcontainer. Reaches the proxy. Only needs changing if the compose project name is not `Agent Sandbox`. |
| `AGENT_EGRESS_NETWORK` | `agentsandbox_agent_egress_net` | Network for agent containers under `host` that get no Docker access. Egress only — the proxy is deliberately not on it. |
| `AGENT_DIND_SOCKET` | `/home/rootless/docker.sock` | The dind daemon's socket, as a path inside the dind container. |
| `AGENT_DIND_DOCKER_GID` | `2375` | gid of the `docker` group in the `docker:dind` image. Without it the agent cannot open the socket it was handed. |
| `AGENT_CREDENTIAL_ENV` | `ANTHROPIC_API_KEY` | Comma-separated names of variables forwarded to agents. |
| `CALLBACK_TIMEOUT_SECONDS` | `15` | How long a completion webhook may take. |
| `DB_PATH` | `/data/jobs.sqlite3` | Job store inside the dispatcher container. |

### Working on a host directory

By default every job gets a fresh volume and clones into it, which is what keeps
jobs disposable. Setting `AGENT_WORKSPACE_MOUNT` replaces that with a directory
from the host:

```sh
COMPOSE_PROFILES=host                       # required — see below
AGENT_WORKSPACE_MOUNT=/home/you/git_repos
AGENT_WORKSPACE_MOUNT_MODE=rw
```

A job then picks which subdirectory to work in:

```sh
curl -X POST http://agent-api:8080/jobs \
  -H "Authorization: Bearer $AGENT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"task": "...", "workspaceDir": "my-project"}'
```

The mount path is configuration; a job only chooses a directory inside it.
`workspaceDir` must be a single plain name — a slash, a `..` or an absolute path
is refused with `422` rather than normalised, because normalising is where path
checks usually go wrong.

**`host` only.** The dind daemon resolves bind-mount paths against its own
filesystem, where a host path does not exist. Under `dind` the mount silently
means something else, so do not use it there.

**`repoUrl` is refused while a mount is set**, on both sides. The clone path
deletes its target before cloning; pointed at a mounted repository it would
delete the very thing the job was given. The dispatcher answers `422` and the
entrypoint refuses too, in case a job reaches it another way.

The task text does **not** land in the working tree when the workspace is
mounted — writing `AGENT_TASK.md` into somebody's repository would leave a stray
file behind. It goes to `/tmp/AGENT_TASK.md`, and `TASK_FILE` in the container
always holds the right path. Prefer `"$TASK_FILE"` over a hardcoded name.

What this costs is worth stating plainly: jobs stop being disposable. The agent
writes into a real working tree, uncommitted changes included, and a job that
goes wrong goes wrong in your repository rather than in a volume that gets thrown
away. Committing first, or pointing the mount at a git worktree, limits the
damage a bad run can do.

### A `$` in AGENT_CMD disappears

Compose interpolates values from `.env` against its own environment before the
dispatcher ever sees them, so `$TASK_FILE` in `AGENT_CMD` arrives as an empty
string — with only a warning on `docker compose up` to hint at it. Write `$$VAR`
to pass a shell variable through:

```sh
AGENT_CMD=claude -p "$(cat $$TASK_FILE)" --permission-mode acceptEdits
```

`$(...)` is left alone, so command substitution needs no escaping.

### Running Claude Code as the agent

The agent image has a second build target with Claude Code baked in:

```sh
docker build --target claude -t docker-agent-sandbox/agent:claude agent/
```

Claude Code brings its own tooling, so it runs in the agent container rather than
in a devcontainer — and because it starts no devcontainer, the dispatcher gives
it no Docker access at all: no `DOCKER_HOST`, no socket, and a network without
the `docker-proxy` alias. Three settings in `.env`:

```sh
AGENT_IMAGE=docker-agent-sandbox/agent:claude
AGENT_CMD_LOCATION=agent
AGENT_CMD=claude -p "$(cat AGENT_TASK.md)" --permission-mode acceptEdits
```

`--permission-mode acceptEdits` is not optional for a job that changes files, and
leaving it off fails in the worst possible way — see
[Claude Code reports success without doing anything](#claude-code-reports-success-without-doing-anything).

Authenticate it one of two ways. **An API key** from the Console, billed by
usage — set `ANTHROPIC_API_KEY` and leave `AGENT_CREDENTIAL_ENV` alone. **Or a
Claude subscription**, which needs a one-time browser login to mint a token:

```sh
claude setup-token          # prints a token; it is not saved anywhere
```

Put it in `CLAUDE_CODE_OAUTH_TOKEN` and name that variable in
`AGENT_CREDENTIAL_ENV`, so the dispatcher forwards it:

```sh
AGENT_CREDENTIAL_ENV=CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN=<the token>
```

The token lasts a year, authenticates against your subscription's quota, and can
only make model requests — no Remote Control, no claude.ai connectors.

Two things to weigh before pointing a subscription at this. Every agent shares
one quota, so concurrent jobs compete with each other and with your own use of
Claude Code. And an agent that clones a repository is reading text written by
someone else: a prompt injection in that repository runs with whatever credential
you handed the container. That is the risk `PLAN.md` §3 describes for agent
credentials generally, and a subscription token is a credential like any other.

Under `dind`, remember to load the image into the daemon after building it — see
[First run](#under-dind-load-the-agent-image-into-the-daemon).

### Credentials

`AGENT_CREDENTIAL_ENV` names variables; their values come from the dispatcher's own environment and
are copied into agent containers. A job request can never add to that list — that is the rule from
`PLAN.md` §3, and it is why the list is configuration rather than payload.

To add a second credential:

```sh
# .env
AGENT_CREDENTIAL_ENV=ANTHROPIC_API_KEY,GITHUB_TOKEN
GITHUB_TOKEN=ghp_…
```

and add `GITHUB_TOKEN=${GITHUB_TOKEN:-}` to the `agent-api` service's `environment:` block, so
compose actually passes it through. Every variable in the tables above is already wired up that way;
only new ones need the extra line.

## Day-to-day

```sh
docker compose ps
docker compose logs -f agent-api
docker compose restart agent-api      # queued jobs survive; running ones do not, see below
docker compose down
```

A dispatcher restart marks every `running` job as `failed` with
`dispatcher restarted while this job was running`. Their containers outlived the process that was
watching them, so there is nothing left to wait on; saying so beats leaving a job that never
finishes. Jobs still `queued` are picked up again.

**Their containers keep running.** The dispatcher does not sweep them at startup, so an agent
orphaned this way carries on past the job it belonged to, unwatched and with no timeout, and its
`agentjob-<id>` volume stays behind. After restarting with jobs in flight, check and clean up:

```sh
docker ps --filter label=agent-sandbox.agent          # or via the proxy under dind
docker rm -f $(docker ps -q --filter label=agent-sandbox.agent)
```

Cross-check against `GET /jobs/{id}` before removing anything — a container belonging to a job that
is genuinely still `running` is one a live worker is waiting on.

### Switching profiles

`docker compose down` only removes services belonging to the profile currently selected. Changing
`COMPOSE_PROFILES` in a running stack therefore leaves the old profile's proxy behind — and both
answer to the `docker-proxy` alias, so the dispatcher may keep talking to the daemon you moved away
from. Take the stack down first:

```sh
docker compose down          # with the OLD COMPOSE_PROFILES still set
$EDITOR .env
docker compose up -d
```

## Watching what it does

### Is the backend reachable?

```sh
docker run --rm --network agentsandbox_agent_net \
  -e DOCKER_HOST=tcp://docker-proxy:2375 docker:cli docker version
```

The reported **server** version tells you which daemon you reached. Under `dind` it is the daemon's
own version, which differs from the host's — that difference is the quickest proof the isolation
holds.

### What is running right now

Agent containers carry `agent-sandbox.agent=<job id>`; devcontainers they create carry
`agent-sandbox.job=<job id>`.

```sh
# host profile
docker ps --filter label=agent-sandbox.agent

# dind profile — ask the daemon behind the proxy
docker run --rm --network agentsandbox_agent_net -e DOCKER_HOST=tcp://docker-proxy:2375 \
  docker:cli docker ps --filter label=agent-sandbox.agent
```

### Job history

Jobs, their status and their full logs live in SQLite inside the `api_data` volume. `GET /jobs/{id}`
and `GET /jobs/{id}/logs` are the supported way in; for a look across all of them:

```sh
docker compose exec agent-api python -c "
import sqlite3
db = sqlite3.connect('/data/jobs.sqlite3')
for row in db.execute('SELECT id, status, exit_code, created_at FROM jobs ORDER BY created_at DESC LIMIT 20'):
    print(row)
"
```

## Maintenance

### Backing up the job store

The store is WAL-mode SQLite. Copying `jobs.sqlite3` alone gives you a stale database — recent
writes sit in the `-wal` file until a checkpoint. Either take the whole set, or let SQLite do it:

```sh
docker compose exec agent-api python -c "
import sqlite3
src = sqlite3.connect('/data/jobs.sqlite3')
dst = sqlite3.connect('/data/backup.sqlite3')
src.backup(dst)
" && docker compose cp agent-api:/data/backup.sqlite3 ./jobs-backup.sqlite3
docker compose exec agent-api rm /data/backup.sqlite3
```

Whether it is worth backing up at all is a real question — `PLAN.md` §6 keeps it open. If job
history does not matter to you, losing the volume costs nothing.

### The store grows without bound

Every job keeps its full container log, and nothing prunes them. There is no retention setting yet.
Until there is, trim it by hand:

```sh
docker compose exec agent-api python -c "
import sqlite3, time
db = sqlite3.connect('/data/jobs.sqlite3')
cutoff = time.time() - 30*24*3600
n = db.execute('DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?', (cutoff,)).rowcount
db.commit(); db.execute('VACUUM')
print(f'{n} jobs removed')
"
```

### Leftover job volumes

Each job gets `agentjob-<id>`, removed when the job ends. A dispatcher killed mid-job leaves one
behind. They are safe to delete once no job is running:

```sh
docker volume ls --filter name=agentjob- -q | xargs -r docker volume rm
```

Under `dind`, run that against the daemon behind the proxy instead, the same way as `docker ps`
above.

### Updating images

Renovate watches this repository and opens PRs for every pinned digest — the compose images, the
agent image's bases, the dispatcher's Python base. Merge the PR, then:

```sh
git pull
docker compose up -d --build
docker build -t docker-agent-sandbox/agent:local agent/
# under dind: re-load the agent image, see "First run"
```

Python dependencies in `api/requirements.txt` are pinned and updated the same way.

## Troubleshooting

Each of these was hit while building the stack; the cause is the useful part.

### `rootlesskit … fork/exec /proc/self/exe: operation not permitted`

The host blocks unprivileged user namespaces. See
[AppArmor on Ubuntu 23.10+](#apparmor-on-ubuntu-2310). `privileged: true` does not fix it.

### dind is healthy, but every container it starts dies

```
error mounting "proc" to rootfs … operation not permitted
```

Docker masks parts of `/proc`, and inside a user namespace procfs may only be mounted if nothing
masked would be revealed. `systempaths=unconfined` on the `dind` service is the fix and is already in
the compose file — if you see this, something removed it.

### `open: No such file or directory` from rootlesskit, before anything starts

slirp4netns cannot create its tap interface. `/dev/net/tun` is missing from the `dind` service's
`devices:`, or absent on the host.

### Jobs fail with `No such image: docker-agent-sandbox/agent:local`

Under `dind`, the daemon has its own image store. Load the image across — see
[First run](#under-dind-load-the-agent-image-into-the-daemon).

### Jobs fail immediately with a 404 on the container the dispatcher just started

The agent container is carrying `agent-sandbox.job=<id>`. The agent removes everything with that
label on its way out, so it deletes itself mid-run. The dispatcher labels agent containers
`agent-sandbox.agent=<id>` precisely to avoid this; if you changed that, change it back.

### `permission denied` opening `/var/run/docker.sock`, under `dind`

The agent runs unprivileged and the socket is `srw-rw---- root:docker`. It needs
`--group-add 2375` — `AGENT_DIND_DOCKER_GID`. Check that the gid still matches the `docker` group in
whatever `docker:dind` version you are on.

### The dispatcher cannot reach `docker-proxy`

Both profiles' proxies claim that alias. If you switched `COMPOSE_PROFILES` without taking the stack
down, two containers answer to it. `docker compose ps -a` shows the stray one.

### Agents cannot clone anything

Under `host`, agent containers run on `agent_run_net`, which is deliberately not internal. If jobs
fail at `git clone` with a DNS error, check that `AGENT_NETWORK` still names an existing, non-internal
network — `agent_net` is internal and will fail exactly this way.

### An MCP server from the repository is not used

`claude mcp list` inside the job shows it as `⏸ Pending approval (run \`claude\`
to approve)`. A `.mcp.json` in a repository is project scope, and project-scope
servers need a human to approve them — which nobody can do in `-p` mode. That is
a feature here: a cloned repository is text somebody else wrote, and it should
not be able to start processes just by being cloned.

To use one deliberately, load it explicitly with `--mcp-config`, which makes it a
server *you* supplied rather than one the repository proposed:

```sh
AGENT_CMD=claude -p "$(cat $$TASK_FILE)" --mcp-config .mcp.json --permission-mode acceptEdits
```

Read the file before you do this, especially for a repository you do not own. An
MCP server is a command the agent will run. Note too that a server which itself
runs `docker run` needs `AGENT_DOCKER_ACCESS=always`, and under `host` that
daemon is the host's.

### Claude Code reports success without doing anything

A job finishes in seconds, `status` is `succeeded`, `exitCode` is `0`, and the log
ends with:

```
Waiting on permission to edit README.md — please approve to continue.
```

Claude Code asks before editing files. In `-p` mode there is nobody to approve, so
it stops — and exits `0` anyway, which the dispatcher can only read as success.
Add `--permission-mode acceptEdits` to `AGENT_CMD` for jobs that change files.

Worth knowing because the failure is silent: nothing in the status tells you the
job did nothing. If jobs come back suspiciously fast, read the log.

### A job says `failed` but something is still running

The dispatcher was restarted while the job was in flight. The job row was closed out, the container
was not. See [Day-to-day](#day-to-day) for how to find and remove it.

### A job hangs in `queued`

All worker slots are busy. `AGENT_MAX_CONCURRENCY` is the limit; `docker ps --filter
label=agent-sandbox.agent` shows what is holding them.
