# n8n example workflows

Two ways to run a job from n8n, matching the two shapes `PLAN.md` §4 describes. Import whichever
fits, or read them as documentation and build your own.

| File | Shape | Use when |
| --- | --- | --- |
| [`agent-job-callback.json`](agent-job-callback.json) | submit, return immediately, react to a webhook | the normal case — agent runs are long |
| [`agent-job-polling.json`](agent-job-polling.json) | submit, then poll until done, answer synchronously | the caller wants one request and one answer |

## Before importing

Both workflows expect an **HTTP Header Auth** credential named
`docker-agent-sandbox dispatcher`, holding:

| Field | Value |
| --- | --- |
| Name | `Authorization` |
| Value | `Bearer <your AGENT_API_TOKEN>` |

n8n asks for it on import. The token is not in these files on purpose — they are meant to be
committed.

n8n must also share `network_backend_net` with the dispatcher, which is how `http://agent-api:8080`
resolves at all. The dispatcher publishes no port, so this network is the only way in.

## The callback workflow

```
POST /webhook/agent-job  →  Config  →  Submit job  →  Respond with jobId
                                                            (returns at once)

POST /webhook/agent-job-done  →  Fetch logs  →  Succeeded?  →  Handle success
                                                           └→  Handle failure
```

The **Config** node holds the two values worth changing in one place: `dispatcherUrl` and
`callbackUrl`. The callback URL has to be *this* n8n as the dispatcher sees it — the default
`http://n8n:5678/webhook/agent-job-done` assumes the service is called `n8n` on the shared network
and serves plain HTTP. Two ways this goes wrong:

- **n8n terminates TLS itself** (`N8N_PROTOCOL=https`). Then the URL needs `https://`, and the
  dispatcher has to trust the certificate. If it comes from a private CA, set
  `EXTRA_CA_BUNDLE_SOURCE` / `EXTRA_CA_BUNDLE` (see `docs/OPERATIONS.md`) — plain `http://`
  against a TLS port fails with an empty-looking `ConnectError`.
- **The public URL is not an option** when a reverse proxy in front of n8n demands a client
  certificate. The dispatcher has none. Use the internal name.

Submitting returns a `jobId` immediately; the second trigger fires when the job reaches a final
state. That separation is the point: an agent run can take many minutes, and nothing in between has
to hold a connection open.

**Where the answer is.** The callback carries status, not output: `result.summary` is the
entrypoint saying *the command finished*, not what the agent said. The agent's actual answer is in
the job's logs, so the workflow fetches them right after the callback, for success and failure
alike. Open the `Fetch logs` node in the execution to read it; `Handle success` and
`Handle failure` are placeholders for whatever should happen next — a commit, a message, another
workflow.

**Test mode does not serve the callback.** *Execute workflow* in the editor registers only the
trigger you are waiting on, for one call. The dispatcher's callback then hits a webhook that does
not exist. Activate the workflow and trigger it through its production URL.

## The polling workflow

```
POST /webhook/agent-job-sync  →  Submit job  →  Wait 15s  →  Get status  →  Still running? ──┐
                                                    ▲                              │         │
                                                    └──────────────────────────────┘         │
                                                                                             ▼
                                                                              Respond with result
```

One request in, one answer out, at the price of holding the caller's connection open for the whole
run. Fine for short jobs and for testing; for real agent work prefer the callback. Mind n8n's own
execution timeout if you keep this.

## Verified against

n8n 2.39.6. Both files import through `n8n import:workflow` and were run end to end against the
stack: the callback workflow submitted a job, answered with its `jobId`, and its second trigger fired
on completion and took the success branch; the polling workflow returned the finished job as its HTTP
response.
