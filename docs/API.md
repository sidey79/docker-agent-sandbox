# Dispatcher API

The HTTP contract n8n talks to. Every endpoint but `/healthz` needs
`Authorization: Bearer <AGENT_API_TOKEN>`.

The dispatcher has no published port. It is reachable by name on
`network_backend_net`, which is what keeps it off the internet — the bearer token
is the second lock, not the only one (`PLAN.md` §3).

## Submit a job

```sh
curl -X POST http://agent-api:8080/jobs \
  -H 'Authorization: Bearer '"$AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "task": "Update the changelog for the next release.",
        "repoUrl": "https://github.com/example/repo.git",
        "repoRef": "main",
        "callbackUrl": "http://n8n:5678/webhook/agent-done"
      }'
```

```json
{ "jobId": "9b19231fe5f14553bdf7d206a7c0e289", "status": "queued" }
```

`202 Accepted`. Only `task` is required. `repoUrl` must be a plain `http://` or
`https://` URL and `repoRef` a plain branch or tag name — a `422` comes back
otherwise, because both end up as arguments to `git clone` and a value starting
with `--` would be read there as an option rather than as a remote.

Note what is *not* in the body: no image, no mounts, no environment, no command.
The container spec is built from the dispatcher's own configuration, so a job
can never widen what it is allowed to do (`PLAN.md` §3).

## Poll a job

```sh
curl http://agent-api:8080/jobs/$JOB_ID -H "Authorization: Bearer $AGENT_API_TOKEN"
```

```json
{
  "jobId": "9b19231fe5f14553bdf7d206a7c0e289",
  "status": "succeeded",
  "createdAt": 1790108414.865419,
  "startedAt": 1790108414.8727694,
  "finishedAt": 1790108477.4120781,
  "exitCode": 0,
  "result": { "jobId": "…", "status": "succeeded", "exitCode": 0, "summary": "…" },
  "error": null
}
```

| Status | Meaning |
| --- | --- |
| `queued` | accepted, waiting for a free worker slot |
| `running` | the agent container exists and is running |
| `succeeded` | the agent exited 0 |
| `failed` | the agent exited non-zero, or the dispatcher could not run it (see `error`) |
| `timeout` | killed after `AGENT_JOB_TIMEOUT_SECONDS` |
| `cancelled` | stopped through `DELETE /jobs/{id}` |

`result` is the agent's own result line, parsed
([`AGENT_CONTRACT.md`](AGENT_CONTRACT.md)). It can be `null` when a job died
before reporting — `status` and `error` are the dispatcher's account of what
happened, `result` is the agent's.

## Logs

```sh
curl http://agent-api:8080/jobs/$JOB_ID/logs -H "Authorization: Bearer $AGENT_API_TOKEN"
```

Plain text, the container's full output, collected before the container is
removed. Available for as long as the job row exists, including after a timeout.

## Cancel

```sh
curl -X DELETE http://agent-api:8080/jobs/$JOB_ID -H "Authorization: Bearer $AGENT_API_TOKEN"
```

Returns the job as it stands at that moment, which is usually still `running`:
cancelling sets a flag that the worker picks up within a poll interval, then
tears the container down in order so the logs survive. Poll once more, or wait
for the callback, to see the final `cancelled`. Cancelling a job that has already
finished is not an error — it returns the finished job unchanged, so a caller
that polls and cancels at the same time does not have to care which won.

## Callback

With `callbackUrl` set, the dispatcher POSTs once when the job reaches a final
state:

```json
{ "jobId": "…", "status": "succeeded", "exitCode": 0, "result": { "…": "…" } }
```

A callback that cannot be delivered is logged and dropped. The job is finished
and its status is stored either way; a webhook nobody answers must not change
that. For long agent runs the callback is more robust than polling, but the poll
endpoint remains the source of truth.

## Health

```sh
curl http://agent-api:8080/healthz
```

`{"status":"ok"}`, no token. Liveness only — it says the process is up, not that
the Docker backend is reachable.
