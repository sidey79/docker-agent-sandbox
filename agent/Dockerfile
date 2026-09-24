# The agent image, in two targets.
#
#   base    the generic agent: brings up a devcontainer, reports a result, and
#           bakes in no agent CLI at all. This is the default target.
#   claude  base plus Claude Code, the first of the "separate build targets"
#           docs/PLAN.md §6 left room for.
#
# Build the generic one with `docker build agent/`, the other with
# `docker build --target claude -t docker-agent-sandbox/agent:claude agent/`.
#
# The Docker CLI is copied in rather than installed from a repository, so that
# Renovate tracks it as the same image the rest of the stack pins.
FROM docker:29.8.1-cli@sha256:6602978e2be3c20e530e33773b8cadcef5fe998a71a534ee24516f1176973cdf AS docker-cli

FROM node:24.21.0-bookworm-slim@sha256:5cbc7caba8c2c0f0bca675d1b61b9f2857e1cf1853c6164ee9dd409501a936e7 AS base

# git to fetch the repository under test, jq for the entrypoint, tini so that a
# cancelled job does not leave zombies behind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        jq \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# jsonc-parser is used by the entrypoint to read a repository's devcontainer.json,
# which is JSON with comments and therefore not jq's business.
RUN npm install -g @devcontainers/cli@0.89.0 jsonc-parser@3.3.1 \
    && npm cache clean --force
ENV NODE_PATH=/usr/local/lib/node_modules

# The job volume is mounted here. Creating the directory with the right owner
# matters: Docker copies the image's ownership onto a fresh named volume, which
# is how the unprivileged `node` user ends up able to write to it.
RUN mkdir -p /workspace && chown node:node /workspace

COPY --chmod=0755 entrypoint.sh /usr/local/bin/agent-entrypoint
COPY --chmod=0755 devcontainer-config.mjs /usr/local/lib/agent/devcontainer-config.mjs

USER node
WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agent-entrypoint"]

# --- claude target -----------------------------------------------------------
# Claude Code runs in the agent container itself, not in the devcontainer, so
# jobs using this image set AGENT_CMD_LOCATION=agent. See docs/AGENT_CONTRACT.md.
FROM base AS claude

USER root
RUN npm install -g @anthropic-ai/claude-code@2.1.281 \
    && npm cache clean --force
USER node

# --- default target ----------------------------------------------------------
# `docker build` with no --target builds the LAST stage, so this one has to stay
# at the bottom of the file. Without it, `docker build agent/` would quietly
# produce the Claude Code image under the generic image's name.
FROM base AS generic
