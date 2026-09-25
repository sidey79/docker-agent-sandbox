# Agent container contract

What the dispatcher puts into an agent container, and what it gets back out. The image itself lives
in [`agent/`](../agent); this file is the interface between it and phase 4.

The contract is deliberately one-directional: everything the agent needs arrives as environment
variables and a volume, and the only structured thing that comes back is a single line on stdout.
Nothing is read back out of the volume, because that would mean starting yet another container just
to look at a filesystem.

## Input

| Variable | Required | Meaning |
| --- | --- | --- |
| `JOB_ID` | yes | Identifies the job. Also becomes the `agent-sandbox.job=<id>` label on every container the agent creates, which is how they get cleaned up. |
| `TASK` | yes | The task text. Written to `AGENT_TASK.md` in the workspace so an agent CLI can read it from a file instead of a command line. |
| `REPO_URL` | no | Cloned with `--depth 1`. Without it the job starts from an empty workspace. |
| `REPO_REF` | no | Branch or tag to clone. Ignored without `REPO_URL`. |
| `WORKSPACE_VOLUME` | no | Name of the job volume. See [Why the volume name is needed](#why-the-volume-name-is-needed). |
| `AGENT_CMD` | no | Shell command run once per job. Without it the container only brings the devcontainer up, which is a useful smoke test on its own. |
| `AGENT_CMD_LOCATION` | no | `devcontainer` (default) or `agent` — see [Where the command runs](#where-the-command-runs). |
| `AGENT_DEVCONTAINER_IMAGE` | no | Image used when the repository ships no `devcontainer.json`. Defaults to `mcr.microsoft.com/devcontainers/base:bookworm`. |
| `DOCKER_HOST` | yes in practice | Where the agent creates the devcontainer. In the stack this is `tcp://docker-proxy:2375`. |
| `WORKSPACE` | no | Mount point of the job volume. Defaults to `/workspace`. |
| `REPO_DIR_NAME` | no | Directory inside the workspace to work in. Defaults to `repo`; with a mounted workspace the dispatcher sets it from the job's `workspaceDir`. |
| `WORKSPACE_MOUNTED` | no | `1` when `/workspace` is a host directory rather than a job volume. Cloning is refused and the task file goes to `/tmp` instead of into the working tree. |
| `TASK_FILE` | — | *Set by the entrypoint*, not by the dispatcher: the path the task text was written to. Prefer it over hardcoding `AGENT_TASK.md`, which moves when the workspace is mounted. |

The job volume is mounted at `$WORKSPACE`. The image creates that directory owned by `node`, so a
fresh named volume inherits that ownership and the unprivileged user can write to it without anyone
having to run `chown` against a volume.

## Where the command runs

`AGENT_CMD_LOCATION` decides this, and it decides whether a devcontainer is
started at all.

`devcontainer` (the default) brings one up and runs the command inside it with
`devcontainer exec`. The command sees the repository's own toolchain, which is
the whole reason the devcontainer is there.

`agent` runs the command in the agent container itself, with the repository as
the working directory, and starts no devcontainer. This is for an image with an
agent CLI baked in — the `claude` build target — where the CLI brings its own
tooling and a devcontainer would be a container started for nothing. With this
setting `AGENT_CMD` is required: there is nothing else for the job to do.

The trade-off is real and worth stating: under `agent` the command does **not**
get the repository's toolchain. A job that needs the project's exact Node or
Python version wants `devcontainer`.

It also decides whether the container gets Docker access at all. Only a job that
starts a devcontainer needs to reach the daemon, so only that job is given the
means: `DOCKER_HOST`, and under `dind` the daemon's socket and the `docker`
group. Under `agent` none of it is set, and the container runs on a network that
does not carry the `docker-proxy` alias — withholding `DOCKER_HOST` while leaving
the proxy one resolvable name away would be a lock with the key beside it. The
agent runs model-directed code, so it does not get a handle on the daemon just in
case.

## Output

Exactly one line on stdout, always, including when the job fails:

```
::agent-result::{"jobId":"smoke-2","status":"succeeded","exitCode":0,"summary":"the agent command finished successfully"}
```

`status` is `succeeded` or `failed`. The line is emitted from an `EXIT` trap, so a job that dies
halfway through still produces one — with `status: failed` and a summary saying the agent never
reported. The dispatcher stores the full log and extracts this line; the container's own exit code
mirrors `exitCode`.

## Why the volume name is needed

This is the part most likely to look redundant and is not.

The agent container talks to a *remote* daemon through the socket proxy. When the devcontainer CLI
mounts a workspace, it passes a path, and that path is resolved on the daemon's side — where
`/workspace` either does not exist or, worse, is something else entirely. A named volume is the only
handle both sides agree on.

So the entrypoint does not hand `devcontainer up` the repository's own config. It builds an
effective config with [`devcontainer-config.mjs`](../agent/devcontainer-config.mjs), which copies the
repository's `devcontainer.json` (parsed as JSONC, comments and all) and adds:

```jsonc
"workspaceMount": "source=<WORKSPACE_VOLUME>,target=/workspaces,type=volume",
"workspaceFolder": "/workspaces/repo"
```

The result is written next to the repository's own config, so relative paths in it — a
`build.dockerfile`, a `context` — keep resolving the way the repository meant them to.

Without `WORKSPACE_VOLUME` the workspace mount is left alone, which only works when the agent and
the daemon share a filesystem.

**Not supported:** a `devcontainer.json` that delegates to `dockerComposeFile`. Its mounts come from
a compose file the repository owns, and injecting the job volume would mean rewriting that file. The
entrypoint fails with an explicit message rather than starting a devcontainer whose workspace is
silently empty.

## Cleanup

The devcontainer runs on the remote daemon and would outlive the agent container. The entrypoint
removes everything labelled `agent-sandbox.job=$JOB_ID` on exit. The dispatcher treats that as best
effort and repeats the sweep when it reaps a job, since a killed agent container never runs its trap.

**The agent container must not carry `agent-sandbox.job` itself.** The sweep does not exclude the
caller, so an agent labelled with its own job id deletes itself mid-run. The dispatcher labels it
`agent-sandbox.agent=<job id>` instead and sweeps both labels.

## Trying it by hand

With the `host` profile up:

```sh
docker build -t docker-agent-sandbox/agent:local agent/
docker volume create agentjob-demo

docker run --rm \
  --network agentsandbox_agent_net \
  -e DOCKER_HOST=tcp://docker-proxy:2375 \
  -e JOB_ID=demo \
  -e TASK="Say hello." \
  -e WORKSPACE_VOLUME=agentjob-demo \
  -e AGENT_CMD='whoami; pwd; cat AGENT_TASK.md' \
  -v agentjob-demo:/workspace \
  docker-agent-sandbox/agent:local
```

`agent_net` is `internal`, so this has no egress and `REPO_URL` will fail. Attaching a second,
non-internal network is what makes cloning work — which is why who attaches that network is listed
as an open point in [`PLAN.md`](PLAN.md#6-open-points).

Under the `dind` profile the same job runs *inside* the dind daemon, where `docker-proxy` does not
resolve. There the daemon's own socket is mounted in instead, and the agent needs the `docker` group
to use it ([`PLAN.md` §7](PLAN.md#7-decisions-taken-after-the-first-draft)):

```sh
docker run --rm --group-add 2375 \
  -e DOCKER_HOST=unix:///var/run/docker.sock \
  -v /home/rootless/docker.sock:/var/run/docker.sock \
  ...
```

The dind daemon has egress of its own, so `REPO_URL` works there without a second network.
